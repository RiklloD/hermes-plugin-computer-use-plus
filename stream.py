"""Screen share for Hermes Desktop: a localhost server in the agent's own process.

    GET  /v1/pair                                 the viewer capability (Hermes Desktop's renderer only, see below)
    GET  /v1/state?viewer=<id>                    sessions, the apps/windows each used (newest first), cursor, status
    GET  /v1/stream?window=<id>&session=<sid>&fps=8&width=640&t=<token>   multipart MJPEG of one window
    GET  /v1/frame?window=<id>&session=<sid>&width=400&t=<token>          one JPEG
    POST /v1/stop?session=<sid>                   stop that session's computer use (runs halt between steps)
    POST /v1/takeover?viewer=<id>&session=<sid>   pause the agent: a person holds that profile's desktop
    POST /v1/handback?viewer=<id>                 release exactly the lease that viewer took

Every route but /v1/pair needs the capability: ``Authorization: Bearer <token>`` (fetch) or ``t=<token>`` (<img>),
a random secret kept in a 0600 file under the Hermes home and shared by every Hermes process on this account. The
viewer gets it from ``/computer-view token`` (gateway command), or from /v1/pair, which answers only a request
carrying the Desktop renderer's own identity (Origin "null" from its file:// page and an Electron user agent; web
pages cannot set either). Host must be the loopback address (DNS-rebinding guard). CORS headers are a convenience
for that renderer, not the authentication.

Pictures come from one producer thread per window, shared by all its streams; it stops when nobody watches. A
stream ends when its client disconnects (checked every 0.5s even when no frame is sent), when the session ends or
expires, or when the server stops.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from . import live

logger = logging.getLogger(__name__)

PORTS = range(47831, 47841)
ALLOWED_ORIGINS = ("null", "file://")
MAX_FPS = 10.0
MAX_STREAMS = 6          # per process; the viewer uses one live stream at a time
PRODUCER_IDLE = 1.0      # a producer with no reader for this long stops (hide/close stops capture fast)

_server: Optional[ThreadingHTTPServer] = None
_port: Optional[int] = None
_generation = [0]        # bumped by stop(): open streams notice and close
_start_lock = threading.Lock()
_streams = [0]
_streams_lock = threading.Lock()


# ── Capability ──────────────────────────────────────────────────────────────

def _token_path() -> Path:
    from hermes_constants import get_default_hermes_root
    return Path(get_default_hermes_root()) / "plugin-data" / "computer-use-plus" / "share-token"


_token_cache: List[str] = []


def token() -> str:
    if _token_cache:
        return _token_cache[0]
    path = _token_path()
    try:
        value = path.read_text().strip()
    except OSError:
        value = ""
    if len(value) < 32:
        path.parent.mkdir(parents=True, exist_ok=True)
        value = secrets.token_urlsafe(32)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(value)
    _token_cache.append(value)
    return value


def port() -> Optional[int]:
    return _port


def open_streams() -> int:
    with _streams_lock:
        return _streams[0]


# ── Server lifecycle ────────────────────────────────────────────────────────

def start() -> Optional[int]:
    """Start the server once per process. Returns the port, or None when no port in range is free."""
    global _server, _port
    with _start_lock:
        if _server is not None:
            return _port
        token()
        for p in PORTS:
            try:
                srv = ThreadingHTTPServer(("127.0.0.1", p), _Handler)
            except OSError:
                continue
            srv.daemon_threads = True
            threading.Thread(target=srv.serve_forever, name="cup-screen-share", daemon=True).start()
            _server, _port = srv, p
            logger.info("computer-use-plus: screen share on 127.0.0.1:%s", p)
            return p
        logger.warning("computer-use-plus: no free port for the screen share in %s-%s", PORTS[0], PORTS[-1])
        return None


def stop() -> None:
    global _server, _port
    with _start_lock:
        _generation[0] += 1
        if _server is not None:
            _server.shutdown()
            _server.server_close()
        _server, _port = None, None
        with _producers_lock:
            for prod in list(_producers.values()):
                prod.stop()
            _producers.clear()


# ── Shared producers ────────────────────────────────────────────────────────

class _Producer:
    """Captures one window for every stream that watches it, at the fastest rate any of them asked for."""

    def __init__(self, info: Dict[str, Any], width: int) -> None:
        self.info = info
        self.width = width
        self.cond = threading.Condition()
        self.frame: Optional[bytes] = None
        self.seq = 0
        self.epoch: Optional[int] = None      # lease epoch the current frame was admitted under
        self.refusal: Optional[str] = None
        self.readers: Dict[int, float] = {}   # reader id -> requested fps
        self.last_reader = time.monotonic()
        self.alive = True
        self.thread = threading.Thread(target=self._loop, name=f"cup-capture:{info['window_id']}", daemon=True)
        self.thread.start()

    def join(self, reader: int, fps: float) -> None:
        with self.cond:
            self.readers[reader] = fps

    def leave(self, reader: int) -> None:
        with self.cond:
            self.readers.pop(reader, None)
            self.last_reader = time.monotonic()

    def stop(self) -> None:
        with self.cond:
            self.alive = False
            self.cond.notify_all()

    def _loop(self) -> None:
        try:
            while True:
                with self.cond:
                    if not self.alive:
                        return
                    if not self.readers and time.monotonic() - self.last_reader > PRODUCER_IDLE:
                        self.alive = False
                        return
                    fps = max(self.readers.values(), default=1.0)
                started = time.monotonic()
                _, epoch = live.lease_state(self.info.get("profile"))
                shot, refusal = live.capture_fenced(self.info, self.width)
                with self.cond:
                    self.refusal = refusal
                    if refusal == "human_has_control":
                        self.frame = None  # never keep (or show) what was on screen around a takeover
                    if shot is not None and shot[0] != self.frame:
                        self.frame, self.seq, self.epoch = shot[0], self.seq + 1, epoch
                    self.cond.notify_all()
                    if refusal == "ended":
                        self.alive = False
                        return
                time.sleep(max(0.02, 1.0 / fps - (time.monotonic() - started)))
        except Exception:
            logger.debug("computer-use-plus: capture producer crashed", exc_info=True)
        finally:
            with self.cond:
                self.alive = False
                self.cond.notify_all()
            with _producers_lock:
                if _producers.get(self.key) is self:
                    _producers.pop(self.key, None)

    @property
    def key(self) -> Tuple[Any, Any, int]:
        return (self.info["session_id"], self.info["window_id"], self.width)


_producers: Dict[Tuple[Any, Any, int], _Producer] = {}
_producers_lock = threading.Lock()


def _producer(info: Dict[str, Any], width: int) -> _Producer:
    key = (info["session_id"], info["window_id"], width)
    with _producers_lock:
        prod = _producers.get(key)
        if prod is None or not prod.alive:
            prod = _producers[key] = _Producer(info, width)
        return prod


def _client_gone(conn: socket.socket) -> bool:
    """A closed (or reset) client, detected without sending anything."""
    try:
        readable, _, errored = select.select([conn], [], [conn], 0)
        if errored:
            return True
        if readable:
            return conn.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True
    return False


# ── HTTP ────────────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_: Any) -> None:  # keep the agent's logs quiet
        pass

    # ── helpers ──

    def _cors(self) -> None:
        origin = self.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _json(self, code: int, body: Dict[str, Any]) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _q(self) -> Dict[str, str]:
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in ("127.0.0.1", "localhost", "::1")

    def _authorized(self) -> bool:
        got = ""
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer "):
            got = auth[7:].strip()
        got = got or self._q().get("t", "")
        return bool(got) and hmac.compare_digest(got.encode(), token().encode())

    def _is_desktop_renderer(self) -> bool:
        ua = self.headers.get("User-Agent") or ""
        # Electron 40 sends no Origin at all from the file:// page (older builds sent "null"). A web page
        # can't get here: browsers always send its real Origin on a cross-site CORS fetch.
        origin = self.headers.get("Origin")
        cors_without_origin = origin is None and self.headers.get("Sec-Fetch-Mode") == "cors"
        return (origin == "null" or cors_without_origin) and "Electron/" in ua

    # ── routes ──

    def do_OPTIONS(self) -> None:
        if self.headers.get("Origin") not in ALLOWED_ORIGINS or not self._host_ok():
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST")
        self.send_header("Access-Control-Allow-Headers", "Authorization, X-Computer-Use-Plus")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        route = urlparse(self.path).path
        if not self._host_ok():
            return self._json(403, {"error": "bad host"})
        if route == "/v1/pair":
            if not self._is_desktop_renderer():
                return self._json(403, {"error": "pairing is only for the Hermes Desktop viewer"})
            return self._json(200, {"token": token()})
        if not self._authorized():
            return self._json(401, {"error": "missing or wrong viewer token"})
        try:
            if route == "/v1/state":
                q = self._q()
                return self._json(200, {**live.snapshot(q.get("viewer")), "pid": os.getpid(), "port": _port})
            if route == "/v1/stream":
                return self._stream()
            if route == "/v1/frame":
                return self._frame()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:
            logger.debug("computer-use-plus: screen share %s failed", route, exc_info=True)
            try:
                return self._json(500, {"error": str(e)[:200]})
            except Exception:
                return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        if not self._host_ok():
            return self._json(403, {"error": "bad host"})
        if not self._authorized():
            return self._json(401, {"error": "missing or wrong viewer token"})
        if self.headers.get("X-Computer-Use-Plus") != "1":
            return self._json(403, {"error": "missing header"})
        q = self._q()
        try:
            if route == "/v1/stop" and q.get("session"):
                return self._json(200, live.request_stop(q["session"]))
            if route == "/v1/takeover" and q.get("viewer"):
                return self._json(200, live.take_over(q["viewer"], q.get("session")))
            if route == "/v1/handback" and q.get("viewer"):
                return self._json(200, live.hand_back(q["viewer"]))
        except Exception as e:
            return self._json(500, {"error": str(e)[:200]})
        self._json(404, {"error": "not found"})

    def _window(self) -> Optional[Dict[str, Any]]:
        q = self._q()
        try:
            wid = int(q.get("window", ""))
        except ValueError:
            return None
        return live.window_info(wid, q.get("session") or None)

    def _frame(self) -> None:
        info = self._window()
        if info is None:
            return self._json(404, {"error": "unknown, ended or expired window"})
        shot, refusal = live.capture_fenced(info, _width(self._q()))
        if shot is None:
            code = 423 if refusal == "human_has_control" else 410 if refusal == "ended" else 503
            return self._json(code, {"error": refusal, "detail": live.capture_error()})
        data = shot[0]
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _stream(self) -> None:
        info = self._window()
        if info is None:
            return self._json(404, {"error": "unknown, ended or expired window"})
        q = self._q()
        try:
            fps = min(MAX_FPS, max(0.2, float(q.get("fps", "8"))))
        except ValueError:
            fps = 8.0
        with _streams_lock:
            if _streams[0] >= MAX_STREAMS:
                busy = True
            else:
                busy = False
                _streams[0] += 1
        if busy:
            return self._json(429, {"error": "too many open streams"})
        generation = _generation[0]
        reader = id(self)
        prod = _producer(info, _width(q))
        prod.join(reader, fps)
        try:
            boundary = "cupframe"
            self.send_response(200)
            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={boundary}")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            sent_seq, last_sent, frame_epoch = -1, 0.0, None
            while generation == _generation[0] and prod.alive:
                with prod.cond:
                    prod.cond.wait_for(lambda: prod.seq != sent_seq or not prod.alive, timeout=0.5)
                    frame, seq, frame_epoch = prod.frame, prod.seq, prod.epoch
                if _client_gone(self.connection):
                    return
                # Publication fence: nothing (not even a frame captured earlier) goes out while a person holds the
                # desktop or after the lease changed since that frame was admitted.
                held, epoch = live.lease_state(info.get("profile"))
                if held or (frame is not None and epoch != frame_epoch):
                    frame = None
                now = time.monotonic()
                if frame is not None and (seq != sent_seq or now - last_sent > 2.0):
                    self.wfile.write(f"--{boundary}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(frame)}"
                                     "\r\n\r\n".encode() + frame + b"\r\n")
                    self.wfile.flush()
                    sent_seq, last_sent = seq, now
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            prod.leave(reader)
            with _streams_lock:
                _streams[0] = max(0, _streams[0] - 1)


def _width(q: Dict[str, str]) -> int:
    try:
        return min(1600, max(160, int(q.get("width", "640"))))
    except ValueError:
        return 640
