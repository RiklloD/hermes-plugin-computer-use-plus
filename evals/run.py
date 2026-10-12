"""Live check for computer-use-plus 0.5: the real Open Computer Use server, real Calculator, the plugin's handlers.

No model is involved: it calls the tools the way a model would and checks the app's answer, the viewer feed (window,
label, cursor) and the window capture. Calculator is quit afterwards if this script launched it.

    <hermes test python> -I ~/.hermes/plugins/computer-use-plus/evals/run.py

Needs an unlocked screen, with Calculator on the current Space. Results append to evals/history.jsonl.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
checkout = os.environ.get("HERMES_AGENT_DIR")
if checkout:
    sys.path.insert(0, checkout)
spec = importlib.util.spec_from_file_location("cup_plugin", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
cup = importlib.util.module_from_spec(spec)
sys.modules["cup_plugin"] = cup
spec.loader.exec_module(cup)
from cup_plugin import codex, live  # noqa: E402

SID = "eval-calculator"


def text_of(out) -> str:
    return out["text_summary"] if isinstance(out, dict) else str(out)


def index_of(state: str, element_id: str) -> str:
    m = re.search(rf"^\t*(\d+) button .*ID: {element_id}\b", state, re.M)
    if not m:
        raise AssertionError(f"no button {element_id} in the state")
    return m.group(1)


def main() -> int:
    was_running = subprocess.run(["pgrep", "-x", "Calculator"], capture_output=True).returncode == 0
    tool = {n: codex.handler_for(n) for n in codex.TOOL_NAMES}
    checks, started = {}, time.monotonic()
    try:
        state = text_of(tool["get_app_state"]({"app": "Calculator"}, session_id=SID))
        checks["state"] = state.startswith("App=com.apple.calculator")
        for button in ("AllClear", "One", "Two", "Multiply", "Three", "Equals"):
            state = text_of(tool["click"]({"app": "Calculator", "element_index": index_of(state, button)},
                                          session_id=SID))
        checks["answer_36"] = bool(re.search(r"StandardInputView;value:36\b", state))
        s = next(x for x in live.snapshot()["sessions"] if x["session_id"] == SID)
        checks["viewer_window"] = bool(s["apps"]) and s["apps"][0]["app"] == "Calculator"
        checks["viewer_label"] = s["label"] == "Clicking “button Equals”"
        p = s.get("pointer") or {}
        checks["viewer_cursor_on_equals"] = p.get("x", 0) > 0.6 and p.get("y", 0) > 0.75
        app = s["apps"][0] if s["apps"] else {}
        shot = live.capture_window(SID, app.get("pid"), app.get("window_id"), 400) if app else None
        checks["capture"] = shot is not None
    finally:
        codex.forget(SID)
        if not was_running:
            subprocess.run(["pkill", "-x", "Calculator"], capture_output=True)
    ok = all(checks.values())
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "version": "0.5.0", "ok": ok,
           "s": round(time.monotonic() - started, 1), "checks": checks}
    with open(ROOT / "evals" / "history.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    print(json.dumps(row, indent=1, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
