"""One JSON line per computer-use tool call in $HERMES_HOME/logs/computer-use-plus.jsonl: tool, ok, error code and
duration. Never arguments, typed text, values or screen content. Rotates at 5 MB."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

_MAX_BYTES = 5 * 1024 * 1024
_lock = threading.Lock()


def _path() -> Optional[Path]:
    try:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home())
    except Exception:
        home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return home / "logs" / "computer-use-plus.jsonl"


def record(session_id: str, tool: str, ok: bool, seconds: float, *, code: Optional[str] = None) -> None:
    path = _path()
    if path is None:
        return
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "session": session_id[:40], "tool": tool, "ok": bool(ok),
             "s": round(seconds, 2)}
    if code:
        entry["code"] = code
    try:
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > _MAX_BYTES:
                path.replace(path.with_suffix(".jsonl.1"))
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass
