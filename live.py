"""Live view for Hermes Desktop: what each session is doing, which apps/windows it used, where its cursor is, and
pictures of those windows. stream.py serves this on localhost to the Desktop viewer.

Recording state costs a few dict writes per action. Pictures are produced only while a viewer holds a stream or
asks for a frame, never inside an action. Each picture is fenced by the session's Bot Desktop lease: none is
captured while a person holds the desktop, and one captured across a takeover (the lease epoch moved) is dropped.
Capture never talks to the Open Computer Use server, so the model's element indices survive it.

The primary path is macOS's own window capture (`screencapture -l <window>`), which reads one window even when it
is covered. When that fails for a window, the screenshot from the agent's latest `get_app_state` / action result for
that window is shown instead (labelled `snapshot`); without one there is no picture (never the whole desktop).
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

PLUGIN_ID = "computer-use-plus"
STOP_HOLD = 15.0            # a Stop refuses new actions this long (the Desktop also interrupts the whole turn)
SESSION_TTL = 1800.0        # forget a session this long after its last action
VIEW_TTL = 180.0            # a session's windows can be watched this long after its last action
ENDED_GRACE = 5.0           # ... and this long after the session ended
VIEWER_TTL = 10.0           # a viewer that has not polled for this long is gone
TAKEOVER_TTL = 30.0         # a takeover whose viewer is gone this long is released
MAX_APPS = 6                # windows remembered per session (the viewer shows the newest few)

_enabled = True
_lock = threading.Lock()
_sizes: Dict[Tuple[Any, Any], Tuple[int, int]] = {}      # (pid, window_id) -> screenshot pixel size
_bounds: Dict[Tuple[Any, Any], Tuple[float, float, float, float]] = {}  # (pid, window_id) -> screen points x,y,w,h
_titles: Dict[Tuple[Any, Any], str] = {}
_sessions: Dict[str, Dict[str, Any]] = {}                 # tool session id -> viewer-facing state
_private: Dict[str, Dict[str, Any]] = {}                  # tool session id -> profile / current target (never served)
_stops: Dict[str, float] = {}                             # tool session id -> time Stop was pressed
_viewers: Dict[str, float] = {}                           # viewer id -> last poll (monotonic)
_takeovers: Dict[str, Optional[str]] = {}                 # viewer id -> profile key of the lease it holds
_window_health: Dict[Any, Dict[str, Any]] = {}            # window id -> {method, error, at, failed_until}
_snapshots: Dict[Any, Tuple[bytes, float]] = {}           # window id -> (PNG of the agent's latest state, time)
_last_poll = [0.0]


def configure(enabled: bool) -> None:
    global _enabled
    _enabled = bool(enabled)


def enabled() -> bool:
    return _enabled


def active() -> bool:
    return _enabled


# ── Window geometry (from observations and core captures) ───────────────────

def remember_window(pid: Any, window_id: Any, *, width: Optional[int] = None, height: Optional[int] = None,
                    title: Optional[str] = None, bounds: Any = None) -> None:
    if not pid or not window_id:
        return
    with _lock:
        if width and height and width > 0 and height > 0:
            _sizes[(pid, window_id)] = (int(width), int(height))
        if title:
            _titles[(pid, window_id)] = title
        if rect := _rect(bounds):
            _bounds[(pid, window_id)] = rect


def _rect(b: Any) -> Optional[Tuple[float, float, float, float]]:
    """x, y, w, h in screen points from the driver's {x, y, width, height} (or {x, y, w, h}, or a list)."""
    try:
        if isinstance(b, dict):
            r = (float(b["x"]), float(b["y"]), float(b.get("width", b.get("w"))), float(b.get("height", b.get("h"))))
        elif isinstance(b, (list, tuple)) and len(b) == 4:
            r = tuple(float(v) for v in b)  # type: ignore[assignment]
        else:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return r if r[2] > 0 and r[3] > 0 else None  # type: ignore[return-value]


def window_bounds(pid: Any, window_id: Any) -> Optional[Tuple[float, float, float, float]]:
    with _lock:
        return _bounds.get((pid, window_id))


def window_size(pid: Any, window_id: Any) -> Optional[Tuple[int, int]]:
    with _lock:
        return _sizes.get((pid, window_id))


# ── Ownership: which profile and window a session belongs to ───────────────

def _profile_key() -> Optional[str]:
    """The HERMES_HOME of the profile serving the current turn (None outside a multiplexed profile scope)."""
    try:
        from hermes_constants import get_hermes_home_override
        home = get_hermes_home_override()
        return str(home) if home is not None else None
    except Exception:
        return None


def _bind(session_id: str) -> None:
    """Remember, at action time (inside the served turn), which profile's lease fences this session, for the
    screen-share server's own threads, which run outside any profile scope."""
    profile = _profile_key()
    with _lock:
        _private.setdefault(session_id, {})["profile"] = profile


def _profile_of(session_id: str) -> Optional[str]:
    with _lock:
        return (_private.get(session_id) or {}).get("profile")


def set_target(session_id: str, pid: Any, window_id: Any, app: str = "", *, title: Optional[str] = None,
               bounds: Any = None, width: Optional[int] = None, height: Optional[int] = None) -> None:
    """The window the session's latest Open Computer Use call acted on (from its state header + CGWindowList)."""
    if not pid or not window_id:
        return
    remember_window(pid, window_id, width=width, height=height, title=title, bounds=bounds)
    with _lock:
        _private.setdefault(session_id, {})["target"] = (pid, window_id, app or "")
        s = _sessions.get(session_id)
        if s is not None:
            _touch_app(s, pid, window_id, app or "")


def remember_snapshot(window_id: Any, png: bytes) -> None:
    """The screenshot from the agent's latest state of this window: the viewer's fallback picture."""
    if _enabled and window_id and png:
        with _lock:
            _snapshots[window_id] = (png, time.time())


# ── Lease (Take over) ───────────────────────────────────────────────────────

def lease_state(profile: Optional[str] = None) -> Tuple[bool, int]:
    """(a person holds the desktop, lease epoch). Fails closed: an unreadable lease counts as held."""
    try:
        from tools.bot_desktop import lease
        cur = lease.get(profile)
        return cur.holder == lease.HUMAN, int(cur.epoch)
    except Exception:
        logger.debug("computer-use-plus: reading the desktop lease failed", exc_info=True)
        return True, -1


def human_holds(profile: Optional[str] = None) -> bool:
    return lease_state(profile)[0]


_human_holds = human_holds


def _lease_viewer(viewer_id: str) -> str:
    return f"computer-view:{viewer_id}"


def take_over(viewer_id: str, session_id: Optional[str] = None) -> Dict[str, Any]:
    """The viewer's person takes the desktop of ``session_id``'s profile. Hand back goes to the same lease."""
    from tools.bot_desktop import lease
    profile = _profile_of(session_id) if session_id else None
    held = lease.acquire(_lease_viewer(viewer_id), profile_key=profile,
                         reason="The user took over from the computer-use viewer.")
    with _lock:
        _takeovers[viewer_id] = profile
        _viewers[viewer_id] = time.monotonic()
    return {"holder": held.holder, "since": held.since}


def hand_back(viewer_id: str) -> Dict[str, Any]:
    from tools.bot_desktop import lease
    with _lock:
        profile = _takeovers.pop(viewer_id, None)
    held = lease.release(_lease_viewer(viewer_id), profile_key=profile)
    return {"holder": held.holder}


def release_abandoned_takeover() -> None:
    """A Take over whose own viewer stopped polling (window closed, app quit) must not keep the agent paused.
    Judged per viewer: another viewer polling does not keep a lost one's hold alive."""
    now = time.monotonic()
    with _lock:
        gone = [(v, p) for v, p in _takeovers.items() if now - _viewers.get(v, 0.0) > TAKEOVER_TTL]
    for viewer_id, profile in gone:
        try:
            from tools.bot_desktop import lease
            lease.release(_lease_viewer(viewer_id), profile_key=profile)
        except Exception:
            logger.debug("computer-use-plus: releasing an abandoned takeover failed", exc_info=True)
        with _lock:
            _takeovers.pop(viewer_id, None)


# ── Viewers ─────────────────────────────────────────────────────────────────

def seen_viewer(viewer_id: str) -> None:
    with _lock:
        _viewers[str(viewer_id)[:64]] = time.monotonic()
        _last_poll[0] = time.monotonic()


def viewers_connected() -> int:
    """Viewers that polled within VIEWER_TTL (the Desktop viewer polls about once a second)."""
    now = time.monotonic()
    with _lock:
        return sum(1 for t in _viewers.values() if now - t < VIEWER_TTL)


# ── Viewer-facing state ─────────────────────────────────────────────────────

def _session(session_id: str) -> Dict[str, Any]:
    s = _sessions.get(session_id)
    if s is None:
        s = _sessions[session_id] = {"session_id": session_id, "ui_session_id": "", "apps": [], "inflight": 0,
                                     "label": "", "action": "", "status": None, "ok": None, "code": None,
                                     "pointer": None, "typing": None, "last_activity": time.time(), "ended": False,
                                     "ended_at": None, "last_failure": None}
    return s


def _touch_app(s: Dict[str, Any], pid: Any, win: Any, app: str) -> None:
    if not pid or not win:
        return
    apps = [a for a in s["apps"] if a["window_id"] != win]
    prev = next((a for a in s["apps"] if a["window_id"] == win), None)
    title = _titles.get((pid, win), "") or (prev or {}).get("title", "")
    apps.insert(0, {"pid": pid, "window_id": win, "app": app or (prev or {}).get("app", ""), "title": title,
                    "last_used": time.time()})
    s["apps"] = apps[:MAX_APPS]


def _watchable(s: Dict[str, Any], now: float) -> bool:
    if not _enabled:
        return False
    if s["ended"]:
        return now - (s["ended_at"] or now) < ENDED_GRACE
    return now - s["last_activity"] < VIEW_TTL


def window_info(window_id: int, session_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Session, pid and profile of a window a session used and may still be watched (not ended, not expired)."""
    now = time.time()
    with _lock:
        for sid, s in sorted(_sessions.items(), key=lambda kv: -kv[1]["last_activity"]):
            if session_id and sid != session_id:
                continue
            if not _watchable(s, now):
                continue
            for a in s["apps"]:
                if a["window_id"] == window_id:
                    return {"session_id": sid, "pid": a["pid"], "window_id": window_id,
                            "profile": (_private.get(sid) or {}).get("profile")}
    return None


def still_watchable(info: Dict[str, Any]) -> bool:
    return window_info(info["window_id"], info["session_id"]) is not None


def snapshot(viewer_id: Optional[str] = None) -> Dict[str, Any]:
    now = time.time()
    if viewer_id:
        seen_viewer(viewer_id)
    with _lock:
        for sid in [k for k, v in _sessions.items() if now - v["last_activity"] > SESSION_TTL]:
            _sessions.pop(sid, None)
            _private.pop(sid, None)
        raw = [(json.loads(json.dumps(s, default=str)), (_private.get(s["session_id"]) or {}).get("profile"))
               for s in _sessions.values()]
        health = {w: dict(h) for w, h in _window_health.items()}
    sessions = []
    for item, profile in raw:
        held, _ = lease_state(profile)
        item["human_has_control"] = held
        item["stopping"] = stop_requested(item["session_id"])
        item["watchable"] = _watchable({**item, "ended_at": item.get("ended_at")}, now)
        for a in item["apps"]:
            size = window_size(a["pid"], a["window_id"])
            if size:
                a["width"], a["height"] = size
            h = health.get(a["window_id"]) or {}
            a["capture"] = {"method": h.get("method"), "error": h.get("error"),
                            "frame_age": round(now - h["at"], 1) if h.get("at") else None}
        sessions.append(item)
    sessions.sort(key=lambda x: -x["last_activity"])
    return {"now": now, "enabled": _enabled, "sessions": sessions}


# ── Stop ────────────────────────────────────────────────────────────────────

def request_stop(session_id: str) -> Dict[str, Any]:
    with _lock:
        _stops[session_id] = time.monotonic()
    return {"stopping": session_id}


def stop_requested(session_id: str) -> bool:
    with _lock:
        at = _stops.get(session_id)
        if at is not None and time.monotonic() - at > STOP_HOLD:
            _stops.pop(session_id, None)
            return False
    return at is not None


def clear_stop(session_id: str) -> None:
    with _lock:
        _stops.pop(session_id, None)


# ── Activity and pointer events ─────────────────────────────────────────────

def _ui_session_id() -> str:
    try:
        from gateway.session_context import get_session_env
        return get_session_env("HERMES_UI_SESSION_ID", "") or ""
    except Exception:
        return ""


def _target(session_id: str) -> Tuple[Any, Any, str]:
    with _lock:
        return (_private.get(session_id) or {}).get("target") or (None, None, "")


_TYPING = frozenset({"type_text", "press_key", "set_value"})


def activity(session_id: str, action: str, label: str, *, phase: str = "start", status: Optional[str] = None,
             ok: Optional[bool] = None, code: Optional[str] = None) -> None:
    if not _enabled:
        return
    if phase == "start":
        _bind(session_id)
    ui = _ui_session_id()
    pid, win, app = _target(session_id)
    with _lock:
        s = _session(session_id)
        if ui:
            s["ui_session_id"] = ui
        start = phase == "start"
        s["inflight"] = max(0, s["inflight"] + (1 if start else -1))
        s.update(label=label, action=action, last_activity=time.time(), ended=False, ended_at=None)
        if start:
            s.update(status=None, ok=None, code=None)
        else:
            s.update(status=status, ok=ok, code=code)
            if ok is False:
                s["last_failure"] = {"label": label, "status": status, "code": code, "at": time.time()}
        if start and action in _TYPING:
            # Keyboard input has no pointer: the viewer shows this badge on the window itself.
            s["typing"] = {"text": label, "at": time.time(), "window_id": win}
        _touch_app(s, pid, win, app)


def pointer(session_id: str, kind: str, at: Optional[Tuple[float, float]], *, to: Optional[Tuple[float, float]] = None,
            pid: Any = None, window_id: Any = None) -> None:
    """Where input is about to land, as fractions of the window (0..1, origin top-left)."""
    if not _enabled or at is None:
        return
    if pid is None:
        pid, window_id, _ = _target(session_id)
    if not window_id:
        return
    clamp = lambda v: round(min(max(float(v), 0.0), 1.0), 4)  # noqa: E731
    with _lock:
        s = _session(session_id)
        seq = ((s.get("pointer") or {}).get("seq") or 0) + 1
        p: Dict[str, Any] = {"kind": kind, "x": clamp(at[0]), "y": clamp(at[1]), "pid": pid, "window_id": window_id,
                             "seq": seq, "at": time.time()}
        if to is not None:
            p["to_x"], p["to_y"] = clamp(to[0]), clamp(to[1])
        s["pointer"] = p
        s["last_activity"] = time.time()
        _touch_app(s, pid, window_id, (next((a["app"] for a in s["apps"] if a["window_id"] == window_id), "")))


def ended(session_id: str) -> None:
    with _lock:
        _stops.pop(session_id, None)
        s = _sessions.get(session_id)
        if s is not None:
            s.update(ended=True, ended_at=time.time(), inflight=0)


# ── Capture ─────────────────────────────────────────────────────────────────

def capture_fenced(info: Dict[str, Any], max_width: int) -> Tuple[Optional[Tuple[bytes, int, int, str]], Optional[str]]:
    """(picture, refusal) for a watchable window, fenced by its profile's lease: nothing is captured while a person
    holds the desktop, and a picture taken across a takeover (epoch moved) is dropped."""
    if not still_watchable(info):
        return None, "ended"
    held, epoch = lease_state(info.get("profile"))
    if held:
        return None, "human_has_control"
    shot = capture_window(info["session_id"], info["pid"], info["window_id"], max_width)
    held_after, epoch_after = lease_state(info.get("profile"))
    if held_after or epoch_after != epoch:
        return None, "human_has_control"
    if shot is None:
        return None, "capture_failed"
    with _lock:
        h = _window_health.setdefault(info["window_id"], {"method": None, "error": None, "at": None,
                                                           "failed_until": 0.0})
        h.update(method=shot[3], at=time.time(), error=None)
    return shot, None


def capture_window(session_id: str, pid: Any, window_id: Any, max_width: int) -> Optional[Tuple[bytes, int, int, str]]:
    """(jpeg bytes, width, height, method) of one window, or None. Never raises. Health is tracked per window,
    so one failing window never pushes the others onto the fallback."""
    now = time.monotonic()
    with _lock:
        h = _window_health.setdefault(window_id, {"method": None, "error": None, "at": None, "failed_until": 0.0})
    if sys.platform == "darwin" and now >= h["failed_until"]:
        shot, err = _screencapture(window_id, max_width)
        if shot is not None:
            h.update(method="window", error=None, at=time.time(), failed_until=0.0)
            return (*shot, "window")
        h.update(error=err, failed_until=now + 10.0)
    shot, err = _snapshot_picture(window_id, max_width)
    if shot is not None:
        h.update(method="snapshot", at=time.time())
        return (*shot, "snapshot")
    h["error"] = h.get("error") or err
    return None


def capture_error() -> Optional[str]:
    with _lock:
        errs = [h["error"] for h in _window_health.values() if h.get("error")]
    return errs[-1] if errs else None


def _encode(img: Any, max_width: int) -> Tuple[bytes, int, int]:
    from PIL import Image
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    if img.width > max_width:
        img = img.resize((max_width, max(1, round(img.height * max_width / img.width))), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=72, optimize=False)
    return out.getvalue(), img.width, img.height


def _screencapture(window_id: Any, max_width: int) -> Tuple[Optional[Tuple[bytes, int, int]], Optional[str]]:
    """macOS window capture by CGWindowID: works for covered windows, needs Screen Recording for Hermes."""
    fd, path = tempfile.mkstemp(prefix="cup_live_", suffix=".jpg")
    os.close(fd)
    try:
        # JPEG keeps both screencapture's encode and our decode cheap; draft() decodes at a reduced scale directly.
        done = subprocess.run(["screencapture", "-x", "-o", f"-l{int(window_id)}", "-t", "jpg", path],
                              capture_output=True, text=True, timeout=4)
        if done.returncode != 0 or os.path.getsize(path) < 1000:
            msg = (done.stderr or "").strip()[:200] or "screencapture produced no image"
            if "could not create image" in msg:
                msg += " (window closed, minimized, on another Space, or the screen is locked)"
            return None, msg
        from PIL import Image
        with Image.open(path) as img:
            img.draft("RGB", (max_width, max_width * 4))
            img.load()
            if _looks_blank(img):
                return None, ("the window capture came back blank: Hermes may lack Screen Recording permission, "
                              "or the screen is locked")
            return _encode(img, max_width), None
    except Exception as e:
        return None, f"screencapture failed: {e}"[:200]
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _looks_blank(img: Any) -> bool:
    """A window capture without Screen Recording permission comes back as a single flat colour."""
    small = img.convert("L").resize((16, 16))
    lo, hi = small.getextrema()
    return hi - lo < 3


def _snapshot_picture(window_id: Any, max_width: int) -> Tuple[Optional[Tuple[bytes, int, int]], Optional[str]]:
    """The agent's latest screenshot of this window (what the model saw), when macOS window capture fails."""
    with _lock:
        snap = _snapshots.get(window_id)
    if snap is None:
        return None, "no window capture and no agent screenshot of this window yet"
    try:
        from PIL import Image
        with Image.open(io.BytesIO(snap[0])) as img:
            img.load()
            return _encode(img, max_width), None
    except Exception as e:
        return None, f"agent screenshot unreadable: {e}"[:200]


def health() -> Dict[str, Any]:
    from . import stream
    with _lock:
        sessions = {sid: {"apps": len(s["apps"]), "idle_s": round(time.time() - s["last_activity"], 1),
                          "ended": s["ended"]} for sid, s in _sessions.items()}
        fresh = [w for w, h in _window_health.items() if h.get("at") and time.time() - h["at"] < 5]
    return {"tool_ready": _enabled, "preview_enabled": _enabled, "screen_share_port": stream.port(),
            "viewer_connected": bool(viewers_connected()), "open_streams": stream.open_streams(),
            "fresh_frame_windows": fresh, "sessions": sessions, "capture_error": capture_error(),
            "human_has_control": human_holds()}


# ── Human-readable status lines ─────────────────────────────────────────────

def describe(action: str, args: Dict[str, Any], element: str = "") -> str:
    """One line for the viewer. ``element`` is the element's line from the latest state ("button Save")."""
    tgt = f"“{element}”" if element else (f"#{args['element_index']}" if args.get("element_index") not in (None, "")
                                         else "")
    text = str(args.get("text") or "")
    short = (text[:40] + "…") if len(text) > 40 else text
    app = str(args.get("app") or "")
    clicks = {1: "Clicking", 2: "Double-clicking", 3: "Triple-clicking"}
    button = str(args.get("mouse_button") or "left")
    verb = ("Right-clicking" if button == "right" else "Middle-clicking" if button == "middle"
            else clicks.get(int(args.get("click_count") or 1) if str(args.get("click_count") or 1).isdigit() else 1,
                            "Clicking"))
    table = {
        "list_apps": "Listing apps",
        "get_app_state": f"Looking at {app or 'the app'}",
        "click": f"{verb} {tgt}".strip() if tgt else f"{verb} in {app}".strip(),
        "perform_secondary_action": f"{str(args.get('action') or 'Action')} {tgt}".strip(),
        "scroll": f"Scrolling {args.get('direction', 'down')} {tgt}".strip(),
        "drag": "Dragging",
        "type_text": f"Typing “{short}”",
        "press_key": f"Pressing {args.get('key', '')}",
        "set_value": f"Setting {tgt} to “{str(args.get('value', ''))[:40]}”".replace("  ", " "),
    }
    return table.get(action, action.replace("_", " ").capitalize())


# Calls that never change or show a window: no activity in the viewer.
QUIET = frozenset({"list_apps"})


def desktop_attached() -> bool:
    """A Hermes Desktop (or other websocket client) is connected to this process."""
    srv = sys.modules.get("tui_gateway.server")
    if srv is None:
        return False
    try:
        with srv._live_transports_lock:
            return bool(srv._live_transports)
    except Exception:
        return False


_noted: Dict[str, float] = {}


def viewer_note(session_id: str) -> Optional[str]:
    """Once per session: why the user can't watch, when a Desktop is attached but no viewer is polling."""
    if not _enabled or viewers_connected() or not desktop_attached():
        return None
    with _lock:
        if session_id in _noted:
            return None
        _noted[session_id] = time.time()
    return ("The user cannot watch this: the Computer Use Plus viewer is not running in Hermes Desktop. Tell them to "
            "switch on the Desktop half of Computer Use Plus in Capabilities → Plugins (run /computer-view to check).")
