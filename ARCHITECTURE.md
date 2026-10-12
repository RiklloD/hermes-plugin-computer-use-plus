# Computer Use Plus

Codex computer use for Hermes, plus a live screen share in Hermes Desktop.

Since 0.5.0 the backend is [Open Computer Use](https://github.com/iFurySt/open-codex-computer-use) (`OpenComputerUse
mcp`, installed with npm under `~/.hermes/tools/open-computer-use`). The model sees exactly what Codex's computer-use
plugin gives it:

| Tool | What it does |
|---|---|
| `list_apps` | Running apps and apps used in the last 14 days |
| `get_app_state` | Start an app session, return the key window's accessibility tree (sequential element indices) and a screenshot. Call once per turn before acting. `text_limit`, `max_tree_nodes`, `max_tree_depth` for long content |
| `click` | By `element_index` or screenshot pixel `x`/`y`; `click_count`, `mouse_button`, `click_method` (auto, accessibility, app_post, sky_click, global) |
| `perform_secondary_action` | A named accessibility action an element lists under "Secondary Actions" |
| `scroll` | An element, by pages, in a direction |
| `drag` | Pixel coordinates (app-posted events; window-server drags need the global gate) |
| `type_text` / `press_key` | Literal text / xdotool-style keys (`super+c`, `Return`) |
| `set_value` | Set a settable accessibility element's value |

Names, descriptions and input schemas are the server's own, copied verbatim into `codex_tools.json` (a test checks
them against the installed server). The server's instructions go into the system prompt (`computer-use` section) for
sessions with the `computer_use` toolset, with its two Codex-specific sentences made true for Hermes. Action results
are the server's: the refreshed tree plus a screenshot (text only for models that don't take images in tool
results). The native Codex-style cursor overlay is drawn on screen by Open Computer Use.app.

How it plugs in:
- The nine tools are registered in the `computer_use` toolset and counted as core for tool_search, so they are listed
  up front like Codex's instead of hiding behind `tool_search`.
- With `plugins.entries.computer-use-plus.allow_tool_override: true` (and the `tools.override` grant), Hermes's
  cua-driver `computer_use` is overridden by a hidden stub, so the model has one way to drive apps. Without the grant
  both are visible.
- One `OpenComputerUse mcp` process per Hermes session (element indices live there); closed at session end, reaped
  after 15 idle minutes. At the end of each turn the server gets Codex's `notifications/turn-ended` (cursor put away).
- Before a call: refused while the user holds the desktop (Take over) or pressed Stop. During a call: a turn
  interrupt or Stop aborts it (the process is killed and restarted next call; the outcome is reported unknown).
- The raw `mcp_servers.open-computer-use` entry in config.yaml is disabled; the plugin serves the same server.

## Screen share (Hermes Desktop)

Unchanged from 0.4: `desktop/plugin.js` shows a fanned stack of cards, one per app window the agent used, newest in
front. The front card is a live capture (~8 fps), the others refresh every 3 s. Under it: status, **■ Stop**,
**Take over / Hand back** and **×**. A **Computer** chip in the status bar shows and hides it.

What feeds it now:
- The target window comes from each result's header (`App=<bundle> (pid N)`, `Window: "<title>"`), matched to a
  CGWindowID through CoreGraphics (`windows.py`, ctypes).
- Labels name the element from the latest tree ("Clicking “button Save”").
- The cursor: coordinate clicks/drags are placed from the screenshot pixels before the call; element actions follow
  the native cursor, which Open Computer Use reports to a per-session observation file
  (`OPEN_COMPUTER_USE_VISUAL_CURSOR_OBSERVATION_FILE`, Cocoa screen points) while the action runs.
- Pictures: `screencapture -l <window>` (covered windows OK); when that fails, the agent's latest screenshot of that
  window (`snapshot`). Never the whole desktop. Every picture is fenced by the Bot Desktop lease.
- `stream.py` serves it on `127.0.0.1:47831-47840` behind a capability token (`/computer-view token`, `/v1/pair`).

`/computer-view` reports readiness: tool ready, server port, viewer connected, fresh pictures, capture problems,
who holds the desktop.

## Files

`__init__.py` registration · `codex.py` the nine tools · `ocu.py` stdio MCP client · `codex_tools.json` verbatim
definitions · `windows.py` CGWindowList · `live.py` viewer state, Stop, Take over, capture · `stream.py` localhost
server · `commands.py` `/computer-view` · `trace.py` one line per call in `logs/computer-use-plus.jsonl` (no content)
· `desktop/plugin.js` viewer.

## Tests

    HERMES_HOME=$(mktemp -d) PYTHONPATH=~/.hermes/hermes-agent <hermes test python> -m pytest -q -p no:xdist plugins/computer-use-plus/tests
    HERMES_HOME=$(mktemp -d) <hermes test python> -I plugins/computer-use-plus/evals/run.py   # live, Calculator

This distribution contains only the current Open Computer Use MCP integration. The retired cua-driver implementation is not included.
