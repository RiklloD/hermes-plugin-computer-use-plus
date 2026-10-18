"""stdio MCP client for Open Computer Use (`OpenComputerUse mcp`), the native Codex-style computer-use runtime.

One server process per Hermes session (element indices from `get_app_state` live in that process), started on first
use, reaped when idle or when the session ends. Each process proxies to the hidden Open Computer Use.app, which owns
the Accessibility / Screen Recording permissions and draws the native cursor overlay.
"""

from __future__ import annotations

import json
import logging
import os
import select
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-03-26"
CALL_TIMEOUT = 90.0
START_TIMEOUT = 20.0
IDLE_REAP = 900.0

_ENV_INSTALL = "HERMES_OCU_HOME"
_DIST = ("node_modules", "open-computer-use", "dist")
_EXE = ("Contents", "MacOS", "OpenComputerUse")
# The patched engine (scripts/build-engine.py, Dev bundle identity) wins over the stock npm app.
_APP_NAMES = ("Open Computer Use (Dev).app", "Open Computer Use.app")
_APP_BINARY = (*_DIST, _APP_NAMES[1], *_EXE)
# The patched engine's own app-agent socket, so it never talks to (or replaces) the stock app's agent.
FORK_SOCKET_NAMESPACE = "hermes"


def _app_binary(root: Path) -> Optional[Path]:
    """The engine under `root`: a build output (`root/<App>`) or an npm-style install (`root/node_modules/...`)."""
    for name in _APP_NAMES:
        for path in (root.joinpath(name, *_EXE), root.joinpath(*_DIST, name, *_EXE)):
            if path.is_file():
                return path
    return None


def install_dir() -> Path:
    """Where the engine is. In order: the patched engine built into this plugin (engine/dist), a shared patched
    build under the Hermes root's tools/, the plugin-local npm backend, then a shared stock npm install."""
    if os.environ.get(_ENV_INSTALL):
        return Path(os.environ[_ENV_INSTALL]).expanduser()
    here = Path(__file__).resolve().parent
    roots = [Path(__file__).resolve().parents[2]]
    try:
        from hermes_constants import get_default_hermes_root
        roots.append(Path(get_default_hermes_root()))
    except Exception:
        pass
    candidates = [here / "engine" / "dist", *(r / "tools" / "open-computer-use-hermes" for r in roots),
                  here / "backend", *(r / "tools" / "open-computer-use" for r in roots)]
    return next((c for c in candidates if _app_binary(c) is not None), here / "backend")


def binary() -> Optional[Path]:
    path = _app_binary(install_dir())
    return path if path is not None and os.access(path, os.X_OK) else None


def is_fork() -> bool:
    """Whether the patched engine (its extra tools and OPEN_COMPUTER_USE_ACTION_* switches) is the one in use."""
    path = binary()
    return path is not None and "(Dev).app" in str(path)


def available() -> bool:
    import sys
    return sys.platform == "darwin" and binary() is not None


class OCUError(RuntimeError):
    pass


class Aborted(RuntimeError):
    """The turn was interrupted or the user pressed Stop while a call was in flight."""


class Client:
    """One `OpenComputerUse mcp` process. Calls are serialised; a dead or wedged process is restarted next call."""

    def __init__(self, env: Optional[Dict[str, str]] = None):
        self._env = dict(env or {})
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._next_id = 0
        self._buf = b""
        self.instructions = ""
        self.tools: List[Dict[str, Any]] = []
        self.last_used = time.monotonic()

    # ── process ──
    def _start(self) -> None:
        exe = binary()
        if exe is None:
            raise OCUError(f"Open Computer Use is not installed (expected under {install_dir()}).")
        env = {**os.environ, **self._env}
        if is_fork():
            env.setdefault("OPEN_COMPUTER_USE_AGENT_SOCKET_NAMESPACE", FORK_SOCKET_NAMESPACE)
        self._proc = subprocess.Popen([str(exe), "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, env=env, close_fds=True)
        self._buf = b""
        init = self._request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                            "clientInfo": {"name": "hermes-computer-use-plus", "version": "0.5"}},
                             timeout=START_TIMEOUT)
        self.instructions = str((init or {}).get("instructions") or "")
        self._notify("notifications/initialized")
        self.tools = list(((self._request("tools/list", {}, timeout=START_TIMEOUT)) or {}).get("tools") or [])

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def close(self) -> None:
        with self._lock:
            self._kill()

    def _kill(self) -> None:
        p, self._proc = self._proc, None
        if p is None:
            return
        try:
            p.stdin.close()  # type: ignore[union-attr]
        except Exception:
            pass
        try:
            p.terminate()
            p.wait(timeout=2)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    # ── JSON-RPC ──
    def _send(self, msg: Dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write((json.dumps(msg, separators=(",", ":")) + "\n").encode())
        self._proc.stdin.flush()

    def _notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})})

    def _readline(self, deadline: float, abort: Optional[Callable[[], bool]] = None) -> bytes:
        assert self._proc is not None and self._proc.stdout is not None
        fd = self._proc.stdout.fileno()
        while b"\n" not in self._buf:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            if abort is not None and abort():
                raise Aborted
            ready, _, _ = select.select([fd], [], [], min(left, 0.2))
            if not ready:
                if self._proc.poll() is not None:
                    raise OCUError("the Open Computer Use server exited")
                continue
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                raise OCUError("the Open Computer Use server closed its output")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line

    def _request(self, method: str, params: Dict[str, Any], *, timeout: float,
                 abort: Optional[Callable[[], bool]] = None) -> Any:
        self._next_id += 1
        rid = self._next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            line = self._readline(deadline, abort).strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") != rid:
                continue  # notifications or stale replies
            if "error" in msg:
                raise OCUError(str((msg["error"] or {}).get("message") or msg["error"]))
            return msg.get("result")

    # ── public ──
    def ensure_started(self) -> None:
        with self._lock:
            if not self.alive():
                self._kill()
                self._start()

    def call(self, name: str, arguments: Dict[str, Any], *, timeout: float = CALL_TIMEOUT,
             abort: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
        """tools/call → the MCP result ({content: [...], isError}). Raises OCUError / TimeoutError / Aborted;
        each of those kills the process, so a late reply can never be read as the next call's answer."""
        with self._lock:
            self.last_used = time.monotonic()
            if not self.alive():
                self._kill()
                self._start()
            try:
                return self._request("tools/call", {"name": name, "arguments": arguments}, timeout=timeout,
                                     abort=abort) or {}
            except (TimeoutError, OCUError, Aborted, OSError):
                self._kill()
                raise
            finally:
                self.last_used = time.monotonic()

    def turn_ended(self) -> None:
        """Codex's `notifications/turn-ended`: the native cursor overlay is cleared."""
        if not self._lock.acquire(timeout=0.2):
            return
        try:
            if self.alive():
                self._notify("notifications/turn-ended")
        except Exception:
            pass
        finally:
            self._lock.release()


# ── per-session clients ──
_clients: Dict[str, Client] = {}
_clients_lock = threading.Lock()


def client(session_key: str, env: Optional[Dict[str, str]] = None) -> Client:
    with _clients_lock:
        _reap_locked()
        c = _clients.get(session_key)
        if c is None:
            c = _clients[session_key] = Client(env)
        return c


def existing(session_key: str) -> Optional[Client]:
    with _clients_lock:
        return _clients.get(session_key)


def close(session_key: str) -> None:
    with _clients_lock:
        c = _clients.pop(session_key, None)
    if c is not None:
        c.close()


def close_all() -> None:
    with _clients_lock:
        items = list(_clients.values())
        _clients.clear()
    for c in items:
        c.close()


def _reap_locked() -> None:
    now = time.monotonic()
    for key in [k for k, c in _clients.items() if now - c.last_used > IDLE_REAP and not c._lock.locked()]:
        c = _clients.pop(key)
        threading.Thread(target=c.close, daemon=True).start()
