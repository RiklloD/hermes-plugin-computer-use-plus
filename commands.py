"""`/computer-view`: readiness check for the screen share, the viewer's capability, manual stop / take over.

    /computer-view                     readiness, as separate facts: tool ready, server port, viewer connected,
                                       windows with a fresh frame, capture error, who holds the desktop
    /computer-view token               the screen-share capability for the Hermes Desktop viewer
    /computer-view stop <session>      stop that session's computer use (runs halt between steps)
    /computer-view takeover <viewer>   pause the agent: a person holds the desktop until hand back
    /computer-view handback <viewer>
"""

from __future__ import annotations

import json
from typing import Any, Dict

from . import live


def readiness() -> Dict[str, Any]:
    h = live.health()
    notes = []
    if not h["tool_ready"]:
        notes.append("live_preview is off in this plugin's settings.")
    if not h["screen_share_port"]:
        notes.append("The screen-share server is not running in this process (it starts with the plugin, on "
                     "127.0.0.1:47831-47840). Each Hermes process that runs computer use has its own.")
    if not h["viewer_connected"]:
        notes.append("No Hermes Desktop viewer has polled in the last 10 s. In Hermes Desktop open Capabilities → "
                     "Plugins and check the Desktop half of Computer Use Plus is on.")
    if h.get("capture_error"):
        notes.append(f"Last capture problem: {h['capture_error']}")
    if h.get("human_has_control"):
        notes.append("A person holds the desktop (Take over); the agent's screen actions are refused until hand back.")
    # "ready" means a person can actually watch: a viewer is connected and at least one window has a fresh frame.
    watching = bool(h["tool_ready"] and h["screen_share_port"] and h["viewer_connected"])
    return {**h, "ready_to_watch": watching and bool(h["fresh_frame_windows"]), "viewer_ready": watching,
            "notes": notes}


def handle(raw: str = "") -> str:
    parts = str(raw or "").split()
    cmd = parts[0].lower() if parts else "status"
    try:
        if cmd in ("status", "doctor", "check"):
            return json.dumps(readiness(), ensure_ascii=False, indent=1)
        if cmd == "token":
            from . import stream
            return json.dumps({"token": stream.token(), "ports": [stream.PORTS[0], stream.PORTS[-1]]})
        if cmd == "stop" and len(parts) >= 2:
            return json.dumps(live.request_stop(parts[1]))
        if cmd == "takeover" and len(parts) >= 2:
            return json.dumps(live.take_over(parts[1]))
        if cmd == "handback" and len(parts) >= 2:
            return json.dumps(live.hand_back(parts[1]))
    except Exception as e:
        return json.dumps({"error": str(e)[:300]})
    return "usage: /computer-view [status | token | stop <session> | takeover <viewer> | handback <viewer>]"
