"""The Codex computer-use tools for Hermes: Open Computer Use's native tools, exposed to the model verbatim
(names, descriptions, input schemas, server instructions) and backed by `OpenComputerUse mcp`.

Around each call the plugin adds only what Hermes needs: the human-takeover lease and the viewer's Stop are refused
before anything is sent, an interrupted turn aborts an in-flight call, results become Hermes tool results (text, plus
the screenshot when the main model takes images), and the live viewer learns which window was used and where the
native cursor went.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import live, ocu, stream, trace, windows

logger = logging.getLogger(__name__)

_DEFS = json.loads((Path(__file__).parent / "codex_tools.json").read_text(encoding="utf-8"))
TOOL_NAMES: Tuple[str, ...] = tuple(t["name"] for t in _DEFS["tools"])
SERVER_INSTRUCTIONS: str = _DEFS["instructions"]

_EMOJI = {"list_apps": "🗂️", "get_app_state": "👀", "click": "🖱️", "perform_secondary_action": "🧰", "scroll": "🖱️",
          "drag": "✋", "type_text": "⌨️", "press_key": "⌨️", "set_value": "✏️", "query": "🔎", "select_text": "🔤"}

# What the patched fork adds, said once in the system prompt rather than learnt by failing.
FORK_GUIDANCE = """
Working fast and precisely with these tools:
- Find a control with `query` (text and/or role) instead of reading a whole tree when you know what you are looking for; its indexes work with click, set_value, scroll and perform_secondary_action.
- Action results show only the tree lines that changed ("tree diff"); indexes on unchanged lines stay valid. A full tree comes back when the window changed or many lines moved.
- Multi-window apps (mail compose, several browser windows): the `Windows:` header line lists window ids. Pin the one you are working in with get_app_state window_id (or window_title) so actions and results cannot drift to another window.
- set_value and type_text read the field back. A set_value error or a type_text WARNING means nothing was written: do not assume success and do not retype blindly; check state first.
- Long or multi-line text (email bodies): type_text method="paste". If type_text says no focused field was found, click the field first.
- Before saving, sending or submitting, verify from the app itself (for mail: reopen the draft and check recipients, subject and body)."""


def schema(name: str) -> Dict[str, Any]:
    t = next(t for t in _DEFS["tools"] if t["name"] == name)
    return {"name": name, "description": t["description"], "parameters": t["inputSchema"]}


def emoji(name: str) -> str:
    return _EMOJI.get(name, "🖥️")


def instructions() -> str:
    """The server's instructions, with the two host-specific sentences made true for Hermes."""
    text = SERVER_INSTRUCTIONS.replace(
        "Codex will automatically stop the session after each assistant turn",
        "Hermes ends the computer use session after each assistant turn")
    text = re.sub(r" If any of these are not available in your environment, use tool_search[^.]*\.", "", text)
    return text + "\n" + FORK_GUIDANCE if ocu.is_fork() else text


# ── per-session state (element lines for viewer labels, screenshot sizes for coordinates) ──

class _State:
    __slots__ = ("app", "bundle", "pid", "window_id", "title", "elements", "shot_size", "cursor_file")

    def __init__(self, cursor_file: str):
        self.app = self.bundle = self.title = ""
        self.pid = self.window_id = None
        self.elements: Dict[str, str] = {}
        self.shot_size: Optional[Tuple[int, int]] = None
        self.cursor_file = cursor_file


_states: Dict[str, _State] = {}
_states_lock = threading.Lock()


def _session_key(session_id: str) -> str:
    profile = live._profile_key() or ""
    return f"{profile}|{session_id}" if profile else session_id


def _cursor_dir() -> Path:
    d = Path(tempfile.gettempdir()) / f"hermes-ocu-{os.getuid()}"
    d.mkdir(mode=0o700, exist_ok=True)
    return d


def _state(key: str) -> _State:
    with _states_lock:
        st = _states.get(key)
        if st is None:
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)[-80:] or "default"
            st = _states[key] = _State(str(_cursor_dir() / f"cursor-{safe}.json"))
        return st


def _client(key: str, st: _State) -> ocu.Client:
    # Every OPEN_COMPUTER_USE_* variable is forwarded per request to the app agent that draws the cursor, so this
    # session's cursor positions land in its own file.
    env = {"OPEN_COMPUTER_USE_VISUAL_CURSOR_OBSERVATION_FILE": st.cursor_file}
    if ocu.is_fork():
        # Action results: changed tree lines only, and no screenshot capture/encoding for a model that never sees
        # it (Hermes drops images for those models anyway).
        env["OPEN_COMPUTER_USE_ACTION_RESULT"] = "diff"
        env["OPEN_COMPUTER_USE_ACTION_SCREENSHOT"] = "1" if _images_native() else "0"
    return ocu.client(key, env)


def _prepare_args(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """A model that cannot see images gets no get_app_state screenshot unless it asks for one: capture and encoding
    are the slowest part of a state read. Kept while the viewer is open, which uses it as its fallback picture."""
    if (name == "get_app_state" and ocu.is_fork() and "include_screenshot" not in args
            and not _images_native() and not live.active()):
        return {**args, "include_screenshot": False}
    return args


def forget(session_id: str) -> None:
    key = _session_key(session_id)
    ocu.close(key)
    with _states_lock:
        st = _states.pop(key, None)
    if st is not None:
        try:
            os.unlink(st.cursor_file)
        except OSError:
            pass


def turn_ended(session_id: str) -> None:
    c = ocu.existing(_session_key(session_id))
    if c is not None:
        c.turn_ended()


# ── result parsing ──

_HEADER_APP = re.compile(r"^App=(\S+) \(pid (\d+)\)", re.M)
_HEADER_WINDOW = re.compile(r'^Window: "(.*)", App: (.*?)\.$', re.M)
_ELEMENT = re.compile(r"^\t*(\d+) (.+)$", re.M)
_DIFF_MARK = re.compile(r"^\((?:tree diff:|no accessibility tree changes)", re.M)
_DIFF_ADDED = re.compile(r"^\+ (\d+) (.+)$", re.M)
_TRIM = re.compile(r"( ID: | Help: |, Secondary Actions: | Secondary Actions: | Description: ).*$")


def _element_label(line: str) -> str:
    short = _TRIM.sub("", line).strip()
    if not short or short in ("button", "text", "row", "cell", "container", "group"):
        desc = re.search(r"Description: ([^,]+)", line)
        short = f"{short} {desc.group(1)}".strip() if desc else short
    return short[:60]


def _png_size(data: bytes) -> Optional[Tuple[int, int]]:
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return None


def _absorb(st: _State, session_id: str, text: str, png: Optional[bytes]) -> None:
    """Learn the target window from a state result; tell the viewer about it."""
    m = _HEADER_APP.search(text)
    if not m:
        return
    st.bundle, st.pid = m.group(1), int(m.group(2))
    w = _HEADER_WINDOW.search(text)
    st.title, st.app = (w.group(1), w.group(2)) if w else ("", st.bundle)
    if _DIFF_MARK.search(text):
        # A tree diff lists changed lines only: unchanged elements keep their labels.
        st.elements.update({i: _element_label(rest) for i, rest in _DIFF_ADDED.findall(text)})
    else:
        st.elements = {i: _element_label(rest) for i, rest in _ELEMENT.findall(text)}
    size = _png_size(png) if png else None
    if size:
        st.shot_size = size
    win = windows.find_window(st.pid, st.title or None)
    if win is None:
        return
    st.window_id = win["window_id"]
    live.set_target(session_id, st.pid, st.window_id, st.app, title=st.title or win.get("title") or None,
                    bounds=win["bounds"], width=size[0] if size else None, height=size[1] if size else None)
    if png:
        live.remember_snapshot(st.window_id, png)


def _same_app(st: _State, app: Any) -> bool:
    a = str(app or "").strip().lower()
    return bool(a) and a in (st.app.lower(), st.bundle.lower(), st.app.lower().lstrip("‎"))


# ── the viewer's cursor ──

_POINTER_KIND = {"click": "click", "perform_secondary_action": "click", "scroll": "scroll", "drag": "drag",
                 "set_value": "focus", "type_text": "focus", "press_key": "focus", "select_text": "focus"}


def _click_kind(args: Dict[str, Any]) -> str:
    if str(args.get("mouse_button") or "") == "right":
        return "right_click"
    try:
        return "double_click" if int(args.get("click_count") or 1) >= 2 else "click"
    except (TypeError, ValueError):
        return "click"


def _shot_fraction(st: _State, x: Any, y: Any) -> Optional[Tuple[float, float]]:
    if st.shot_size and isinstance(x, (int, float)) and isinstance(y, (int, float)):
        return (float(x) / st.shot_size[0], float(y) / st.shot_size[1])
    return None


def _pointer_before(session_id: str, st: _State, name: str, args: Dict[str, Any]) -> None:
    """Coordinate input lands where the model said: publish it before the call."""
    if not _same_app(st, args.get("app")) or st.window_id is None:
        return
    if name == "click":
        at = _shot_fraction(st, args.get("x"), args.get("y"))
        if at:
            live.pointer(session_id, _click_kind(args), at, pid=st.pid, window_id=st.window_id)
    elif name == "drag":
        start = _shot_fraction(st, args.get("from_x"), args.get("from_y"))
        if start:
            live.pointer(session_id, "drag", start, to=_shot_fraction(st, args.get("to_x"), args.get("to_y")),
                         pid=st.pid, window_id=st.window_id)


def _read_cursor(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _cursor_fraction(st: _State, point: Any) -> Optional[Tuple[float, float]]:
    """The native cursor's tip (Cocoa screen points, bottom-left origin) → fractions of the target window."""
    if not isinstance(point, dict) or st.pid is None or st.window_id is None:
        return None
    rect = live.window_bounds(st.pid, st.window_id)
    height = windows.main_display_height()
    if not rect or not height:
        return None
    try:
        x, y = float(point["x"]), height - float(point["y"])
    except (KeyError, TypeError, ValueError):
        return None
    fx, fy = (x - rect[0]) / rect[2], (y - rect[1]) / rect[3]
    return (fx, fy) if -0.05 <= fx <= 1.05 and -0.05 <= fy <= 1.05 else None


class _CursorWatch:
    """While an element action runs, follow the native cursor's destination so the viewer's cursor moves with it."""

    def __init__(self, session_id: str, st: _State, kind: str):
        self.session_id, self.st, self.kind = session_id, st, kind
        self.before = (_read_cursor(st.cursor_file) or {}).get("timestamp")
        self.last: Optional[Tuple[float, float]] = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="cup-cursor")
        self._thread.start()

    def _publish(self, final: bool) -> None:
        obs = _read_cursor(self.st.cursor_file) or {}
        if obs.get("timestamp") == self.before:
            return
        at = _cursor_fraction(self.st, obs.get("restingTipPosition") or obs.get("tipPosition"))
        if at is None or (at == self.last and not final):
            return
        self.last = at
        live.pointer(self.session_id, self.kind if final else "move", at, pid=self.st.pid, window_id=self.st.window_id)

    def _run(self) -> None:
        while not self._stop.wait(0.12):
            try:
                self._publish(False)
            except Exception:
                logger.debug("computer-use-plus: cursor follow failed", exc_info=True)

    def finish(self) -> None:
        self._stop.set()
        self._thread.join(timeout=0.5)
        try:
            self._publish(True)
        except Exception:
            logger.debug("computer-use-plus: cursor follow failed", exc_info=True)


# ── results ──

def _images_native() -> bool:
    """Whether the main model takes images in tool results (the same gate core's computer_use uses)."""
    try:
        from agent.auxiliary_client import _read_main_model, _read_main_provider
        from hermes_cli.config import load_config_readonly
        from tools.vision_tools import _native_tool_result_images
        return bool(_native_tool_result_images(_read_main_provider() or "", _read_main_model() or "",
                                               load_config_readonly()))
    except Exception:
        return True


def _to_hermes(result: Dict[str, Any]) -> Tuple[Any, str, Optional[bytes]]:
    """MCP result → (Hermes tool result, its text, the PNG if any)."""
    texts: List[str] = []
    png: Optional[bytes] = None
    png_b64 = ""
    for item in result.get("content") or []:
        if item.get("type") == "text":
            texts.append(str(item.get("text") or ""))
        elif item.get("type") == "image" and item.get("data") and png is None:
            png_b64 = str(item["data"])
            try:
                png = base64.b64decode(png_b64)
            except Exception:
                png, png_b64 = None, ""
    text = "\n".join(t for t in texts if t)
    if result.get("isError"):
        text = text or "The action failed."
        return text, text, png
    if png_b64 and _images_native():
        mime = str(next((i.get("mimeType") for i in result.get("content") or [] if i.get("type") == "image"),
                        "image/png") or "image/png")
        return {"_multimodal": True,
                "content": [{"type": "text", "text": text},
                            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{png_b64}"}}],
                "text_summary": text}, text, png
    return text or "OK", text, png


# ── the handler ──

def _refusal(code: str, message: str) -> str:
    return json.dumps({"ok": False, "code": code, "error": message})


def _guard(name: str, session_id: str) -> Optional[str]:
    if name not in live.QUIET and live.stop_requested(session_id):
        return _refusal("cancelled", "The user pressed Stop in the computer-use viewer. Stop driving the desktop and "
                                     "tell them where you got to.")
    try:
        from tools.bot_desktop import lease
        try:
            lease.assert_agent_may_act()
        except lease.HumanHasControl as e:
            return _refusal("human_has_control", str(e))
    except ImportError:
        pass
    return None


def _aborted(session_id: str) -> Callable[[], bool]:
    def check() -> bool:
        if live.stop_requested(session_id):
            return True
        try:
            from tools.interrupt import is_interrupted
            return bool(is_interrupted())
        except Exception:
            return False
    return check


def handle(name: str, args: Optional[Dict[str, Any]], **kwargs: Any) -> Any:
    args = {k: v for k, v in dict(args or {}).items() if v is not None}
    session_id = str(kwargs.get("session_id") or "")
    if (refused := _guard(name, session_id)) is not None:
        trace.record(session_id, name, False, 0.0, code=json.loads(refused)["code"])
        return refused
    live.release_abandoned_takeover()
    key = _session_key(session_id)
    st = _state(key)
    watching = live.active() and name not in live.QUIET
    element = st.elements.get(str(args.get("element_index", ""))) if _same_app(st, args.get("app")) else None
    label = live.describe(name, args, element or "")
    if watching:
        stream.start()
        live.activity(session_id, name, label, phase="start")
        _pointer_before(session_id, st, name, args)
    watch = None
    if watching and name in _POINTER_KIND and args.get("element_index") not in (None, ""):
        watch = _CursorWatch(session_id, st, _click_kind(args) if name == "click" else _POINTER_KIND[name])
    started = time.monotonic()
    ok, code, out = False, None, None
    try:
        result = _client(key, st).call(name, _prepare_args(name, args), abort=_aborted(session_id))
        out, text, png = _to_hermes(result)
        ok = not result.get("isError")
        if name != "list_apps":
            try:
                _absorb(st, session_id, text, png)
            except Exception:
                logger.debug("computer-use-plus: reading the target window failed", exc_info=True)
        return out
    except ocu.Aborted:
        code = "cancelled"
        out = _refusal("cancelled", "Stopped: the turn was interrupted or the user pressed Stop while this action "
                                    "was running. Whether it took effect is unknown; call get_app_state to check.")
        return out
    except TimeoutError:
        code = "timeout"
        out = _refusal("timeout", "Open Computer Use did not answer in time; the outcome is unknown. Call "
                                  "get_app_state to check before retrying.")
        return out
    except Exception as e:
        code = "unavailable"
        out = _refusal("unavailable", f"Computer Use is unavailable: {e}"[:500])
        return out
    finally:
        if watch is not None:
            watch.finish()
        trace.record(session_id, name, ok, time.monotonic() - started, code=code)
        if watching:
            try:
                live.activity(session_id, name, label, phase="end",
                              status="ok" if ok else ("unknown" if code in ("cancelled", "timeout") else "failed"),
                              ok=ok, code=code)
            except Exception:
                logger.debug("computer-use-plus: live update failed", exc_info=True)


def handler_for(name: str) -> Callable[..., Any]:
    def _handler(args: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Any:
        return handle(name, args, **kwargs)
    _handler.__name__ = f"handle_{name}"
    return _handler
