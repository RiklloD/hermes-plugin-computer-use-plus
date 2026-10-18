"""The Codex tools: registration, verbatim schemas, results, guards, abort, viewer feed (target window, cursor)."""

from __future__ import annotations

import base64
import json
import os
import struct
import sys
import textwrap
import time
import zlib

import pytest

import cup_plugin
from cup_plugin import codex, live, ocu, windows

STATE_TEXT = textwrap.dedent("""\
    App=com.apple.Notes (pid 5)
    Window: "Untitled", App: Notes.
    0 standard window Untitled ID: main, Secondary Actions: Raise
    \t1 button Save Help: Save the note ID: save
    \t2 text field Description: Name, Value: Ada
    \t3 button Description: Delete

    The focused UI element is 2 text field.""")


def png(w: int, h: int) -> bytes:
    raw = b"".join(b"\x00" + bytes([200, 30, 30]) * w for _ in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))  # noqa: E731
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


SHOT = png(800, 600)


class FakeClient:
    def __init__(self):
        self.calls = []
        self.error = None
        self.on_call = None

    def call(self, name, args, *, timeout=None, abort=None):
        self.calls.append((name, dict(args)))
        if self.on_call:
            self.on_call(name, args, abort)
        if self.error:
            return {"content": [{"type": "text", "text": self.error}], "isError": True}
        if name == "list_apps":
            return {"content": [{"type": "text", "text": "Notes — com.apple.Notes [running]"}], "isError": False}
        return {"content": [{"type": "text", "text": STATE_TEXT},
                            {"type": "image", "data": base64.b64encode(SHOT).decode(), "mimeType": "image/png"}],
                "isError": False}

    def turn_ended(self):
        self.calls.append(("turn-ended", {}))


@pytest.fixture
def fake(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    client = FakeClient()
    monkeypatch.setattr(codex, "_client", lambda key, st: client)
    monkeypatch.setattr(ocu, "existing", lambda key: client)
    monkeypatch.setattr(windows, "find_window", lambda pid, title=None: {
        "window_id": 7, "pid": pid, "title": title or "", "layer": 0, "bounds": (100.0, 100.0, 400.0, 300.0)})
    monkeypatch.setattr(windows, "main_display_height", lambda: 1000.0)
    monkeypatch.setattr(codex, "_images_native", lambda: True)
    monkeypatch.setattr(codex, "_cursor_dir", lambda: tmp_path)
    for d in (live._sessions, live._private, live._stops, live._noted, live._viewers, live._takeovers,
              live._window_health, live._snapshots):
        d.clear()
    codex._states.clear()
    live.configure(True)
    from tools.bot_desktop import lease
    lease._reset_for_tests()
    yield client
    codex._states.clear()


def tool(name, args, sid="t1"):
    return codex.handler_for(name)(args, session_id=sid)


# ── the model-facing surface is Open Computer Use's, verbatim ──

def test_codex_tools_with_verbatim_schemas():
    assert set(codex.TOOL_NAMES) == {"list_apps", "get_app_state", "query", "click", "perform_secondary_action",
                                     "scroll", "drag", "type_text", "press_key", "set_value", "select_text"}
    assert {"window_id", "window_title"} <= set(codex.schema("get_app_state")["parameters"]["properties"])
    assert codex.schema("type_text")["parameters"]["properties"]["method"]["enum"] == ["auto", "keys", "paste"]
    click = codex.schema("click")
    assert click["description"].startswith("Click an element by index or pixel coordinates from screenshot.")
    assert click["parameters"]["required"] == ["app"]
    assert set(click["parameters"]["properties"]) == {"app", "element_index", "x", "y", "click_count",
                                                      "mouse_button", "click_method"}
    assert codex.schema("get_app_state")["parameters"]["properties"]["text_limit"]["anyOf"]


@pytest.mark.skipif(not ocu.available(), reason="Open Computer Use is not installed")
def test_schemas_match_the_installed_server():
    c = ocu.Client()
    try:
        c.ensure_started()
        live_defs = {t["name"]: t for t in c.tools}
    finally:
        c.close()
    for name in codex.TOOL_NAMES:
        assert codex.schema(name)["parameters"] == live_defs[name]["inputSchema"], name
        assert codex.schema(name)["description"] == live_defs[name]["description"], name


def test_instructions_are_the_servers_made_true_for_hermes():
    text = codex.instructions()
    assert text.startswith("Computer Use tools let you interact with macOS apps")
    assert "Hermes ends the computer use session after each assistant turn" in text
    assert "Codex will" not in text and "tool_search" not in text
    assert "Ask the user before taking destructive or externally visible actions" in text


class Ctx:
    def __init__(self, override=True):
        self.tools, self.hooks, self.sections, self.commands = {}, {}, {}, {}
        self.override = override

    def register_tool(self, **kw):
        self.tools[kw["name"]] = kw

    def has_capability(self, cap):
        return self.override and cap == "tools.override"

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_system_prompt_section(self, id, content, **kw):
        self.sections[id] = content

    def register_command(self, name, fn, **kw):
        self.commands[name] = fn

    def get_config(self, key, default=None):
        return False  # no screen-share server in unit tests


@pytest.fixture(autouse=True)
def _live_on():
    yield
    live.configure(True)  # register() reads live_preview; a test Ctx turns it off


def test_register_hides_core_computer_use_and_keeps_the_tools_eager(monkeypatch):
    monkeypatch.setattr(ocu, "available", lambda: True)
    from tools import tool_search
    monkeypatch.setattr(tool_search, "_core_tool_names", tool_search._core_tool_names)
    ctx = Ctx()
    cup_plugin.register(ctx)
    assert set(codex.TOOL_NAMES) <= set(ctx.tools)
    assert all(ctx.tools[n]["toolset"] == "computer_use" for n in codex.TOOL_NAMES)
    hidden = ctx.tools["computer_use"]
    assert hidden["override"] is True and hidden["check_fn"]() is False
    assert set(codex.TOOL_NAMES) <= tool_search._core_tool_names()
    assert "computer-use" in ctx.sections and {"on_session_end", "post_llm_call"} <= set(ctx.hooks)
    cup_plugin._keep_eager()  # idempotent
    assert tool_search._core_tool_names._cup_eager


def test_register_without_grant_leaves_core_computer_use(monkeypatch):
    monkeypatch.setattr(ocu, "available", lambda: True)
    from tools import tool_search
    monkeypatch.setattr(tool_search, "_core_tool_names", tool_search._core_tool_names)
    ctx = Ctx(override=False)
    cup_plugin.register(ctx)
    assert "computer_use" not in ctx.tools and "click" in ctx.tools


def test_not_installed_registers_nothing(monkeypatch):
    monkeypatch.setattr(ocu, "available", lambda: False)
    ctx = Ctx()
    cup_plugin.register(ctx)
    assert ctx.tools == {}


def test_prompt_section_only_where_the_toolset_is_on(monkeypatch):
    monkeypatch.setattr(ocu, "available", lambda: True)
    import hermes_cli.tools_config as tc
    monkeypatch.setattr(tc, "_get_platform_tools", lambda cfg, p, **k: {"computer_use"} if p == "cli" else {"web"})
    assert cup_plugin._prompt_section({"platform": "desktop"}).startswith("Computer Use tools")
    assert cup_plugin._prompt_section({"platform": "telegram"}) == ""


# ── results ──

def test_state_comes_back_as_text_plus_screenshot(fake):
    out = tool("get_app_state", {"app": "Notes"})
    assert out["_multimodal"] and out["text_summary"] == STATE_TEXT
    assert out["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert fake.calls == [("get_app_state", {"app": "Notes"})]


def test_text_only_models_get_the_tree(fake, monkeypatch):
    monkeypatch.setattr(codex, "_images_native", lambda: False)
    assert tool("get_app_state", {"app": "Notes"}) == STATE_TEXT


def test_errors_are_passed_through(fake):
    fake.error = "appNotFound(\"Nope\")"
    assert tool("click", {"app": "Nope", "element_index": "1"}) == "appNotFound(\"Nope\")"


def test_none_arguments_are_not_sent(fake):
    tool("click", {"app": "Notes", "element_index": "1", "x": None})
    assert fake.calls[-1] == ("click", {"app": "Notes", "element_index": "1"})


def test_list_apps(fake):
    assert "com.apple.Notes" in tool("list_apps", {})


# ── guards ──

def test_stop_refuses_new_actions(fake):
    live.request_stop("t1")
    out = json.loads(tool("click", {"app": "Notes", "element_index": "1"}))
    assert out["code"] == "cancelled" and fake.calls == []
    live.clear_stop("t1")


def test_take_over_refuses_every_tool(fake):
    from tools.bot_desktop import lease
    lease.acquire("computer-view:v1", reason="test")
    try:
        assert json.loads(tool("get_app_state", {"app": "Notes"}))["code"] == "human_has_control"
        assert fake.calls == []
    finally:
        lease.release("computer-view:v1")
    assert tool("get_app_state", {"app": "Notes"})["_multimodal"]


def test_stop_during_a_call_aborts_it(fake):
    def slow(name, args, abort):
        live.request_stop("t1")
        assert abort()
        raise ocu.Aborted
    fake.on_call = slow
    out = json.loads(tool("click", {"app": "Notes", "element_index": "1"}))
    assert out["code"] == "cancelled" and "unknown" in out["error"]
    live.clear_stop("t1")


def test_turn_end_and_session_end(fake, monkeypatch):
    closed = []
    monkeypatch.setattr(ocu, "close", lambda key: closed.append(key))
    cup_plugin.on_turn_end(session_id="t1")
    assert fake.calls[-1][0] == "turn-ended"
    cup_plugin.on_session_end(session_id="t1")
    assert closed == ["t1"]


# ── the viewer feed ──

def test_state_tells_the_viewer_the_window_and_labels(fake):
    tool("get_app_state", {"app": "Notes"})
    s = live.snapshot()["sessions"][0]
    assert s["apps"][0]["window_id"] == 7 and s["apps"][0]["app"] == "Notes"
    assert s["apps"][0]["width"] == 800 and s["apps"][0]["title"] == "Untitled"
    tool("click", {"app": "Notes", "element_index": "1"})
    assert live.snapshot()["sessions"][0]["label"] == "Clicking “button Save”"
    tool("click", {"app": "Notes", "element_index": "3"})
    assert live.snapshot()["sessions"][0]["label"] == "Clicking “button Delete”"
    tool("set_value", {"app": "Notes", "element_index": "2", "value": "Bo"})
    s = live.snapshot()["sessions"][0]
    assert s["label"] == "Setting “text field” to “Bo”" and s["typing"]["window_id"] == 7
    assert s["status"] == "ok" and s["ok"] is True


def test_a_tree_diff_keeps_the_labels_of_unchanged_elements(fake):
    tool("get_app_state", {"app": "Notes"})
    st = next(iter(codex._states.values()))
    diff = textwrap.dedent("""\
        App=com.apple.Notes (pid 5)
        Window: "Untitled", App: Notes.
        (tree diff: 1 added, 1 removed, 3 unchanged lines omitted; indexes on unchanged lines are still valid)
        - 3 button Description: Delete
        + 3 button Send Help: Send the message""")
    codex._absorb(st, "t1", diff, None)
    assert st.elements["1"] == "button Save" and st.elements["3"] == "button Send"


def test_text_only_models_skip_state_screenshots_on_the_fork(fake, monkeypatch):
    monkeypatch.setattr(ocu, "is_fork", lambda: True)
    monkeypatch.setattr(codex, "_images_native", lambda: False)
    live.configure(False)
    tool("get_app_state", {"app": "Notes"})
    assert fake.calls[-1][1]["include_screenshot"] is False
    tool("get_app_state", {"app": "Notes", "include_screenshot": True})
    assert fake.calls[-1][1]["include_screenshot"] is True
    monkeypatch.setattr(codex, "_images_native", lambda: True)
    tool("get_app_state", {"app": "Notes"})
    assert "include_screenshot" not in fake.calls[-1][1]


def test_fork_guidance_only_with_the_fork(monkeypatch):
    monkeypatch.setattr(ocu, "is_fork", lambda: True)
    assert "window_id" in codex.instructions() and 'method="paste"' in codex.instructions()
    monkeypatch.setattr(ocu, "is_fork", lambda: False)
    assert "Working fast and precisely" not in codex.instructions()


def test_coordinate_click_places_the_cursor_from_screenshot_pixels(fake):
    tool("get_app_state", {"app": "Notes"})
    tool("click", {"app": "Notes", "x": 200, "y": 300, "click_count": 2})
    p = live.snapshot()["sessions"][0]["pointer"]
    assert (p["kind"], p["x"], p["y"], p["window_id"]) == ("double_click", 0.25, 0.5, 7)
    tool("drag", {"app": "Notes", "from_x": 0, "from_y": 0, "to_x": 800, "to_y": 600})
    p = live.snapshot()["sessions"][0]["pointer"]
    assert (p["kind"], p["x"], p["to_x"], p["to_y"]) == ("drag", 0.0, 1.0, 1.0)


def test_element_click_follows_the_native_cursor(fake):
    tool("get_app_state", {"app": "Notes"})
    st = codex._states["t1"]

    def cursor_moves(name, args, abort):
        # The overlay reports Cocoa points (bottom-left origin): (300, 1000-250) is the window's centre.
        with open(st.cursor_file, "w") as fh:
            json.dump({"tipPosition": {"x": 300, "y": 750}, "restingTipPosition": {"x": 300, "y": 750},
                       "timestamp": time.time(), "phase": "idle"}, fh)
    fake.on_call = cursor_moves
    tool("click", {"app": "Notes", "element_index": "1", "mouse_button": "right"})
    p = live.snapshot()["sessions"][0]["pointer"]
    assert (p["kind"], p["x"], p["y"]) == ("right_click", 0.5, 0.5)


def test_cursor_outside_the_window_is_ignored(fake):
    tool("get_app_state", {"app": "Notes"})
    st = codex._states["t1"]
    assert codex._cursor_fraction(st, {"x": 5000, "y": 10}) is None
    assert codex._cursor_fraction(st, {"x": 100, "y": 900}) == (0.0, 0.0)


def test_snapshot_is_the_fallback_picture(fake, monkeypatch):
    tool("get_app_state", {"app": "Notes"})
    monkeypatch.setattr(live, "_screencapture", lambda wid, w: (None, "could not create image"))
    shot = live.capture_window("t1", 5, 7, 400)
    assert shot is not None and shot[1:] == (400, 300, "snapshot") and shot[0][:2] == b"\xff\xd8"
    assert live.capture_window("t1", 5, 99, 400) is None


def test_list_apps_is_quiet(fake):
    tool("list_apps", {})
    assert live.snapshot()["sessions"] == []


def test_trace_has_no_content(fake, tmp_path):
    tool("type_text", {"app": "Notes", "text": "secret words"})
    log = (tmp_path / "logs" / "computer-use-plus.jsonl").read_text()
    assert "secret" not in log and json.loads(log.splitlines()[-1])["tool"] == "type_text"


# ── the stdio client against a scripted server ──

FAKE_SERVER = r'''#!{python}
import json, sys, time
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    m = msg["method"]
    if m == "initialize":
        res = {{"protocolVersion": "2025-03-26", "instructions": "hi", "capabilities": {{}}}}
    elif m == "tools/list":
        res = {{"tools": [{{"name": "click"}}]}}
    else:
        args = msg["params"]["arguments"]
        if args.get("sleep"):
            time.sleep(args["sleep"])
        res = {{"content": [{{"type": "text", "text": "did " + msg["params"]["name"]}}], "isError": False}}
    sys.stdout.write(json.dumps({{"jsonrpc": "2.0", "id": msg["id"], "result": res}}) + "\n")
    sys.stdout.flush()
'''


@pytest.fixture
def server(tmp_path, monkeypatch):
    exe = tmp_path.joinpath(*ocu._APP_BINARY)
    exe.parent.mkdir(parents=True)
    exe.write_text(FAKE_SERVER.format(python=sys.executable))
    exe.chmod(0o755)
    monkeypatch.setenv(ocu._ENV_INSTALL, str(tmp_path))
    c = ocu.Client()
    yield c
    c.close()


def test_client_handshake_and_calls(server):
    assert server.call("click", {"app": "X"})["content"][0]["text"] == "did click"
    assert server.instructions == "hi" and server.tools == [{"name": "click"}]


def test_client_abort_kills_the_process_and_recovers(server):
    server.ensure_started()
    started = time.monotonic()
    with pytest.raises(ocu.Aborted):
        server.call("click", {"sleep": 5}, abort=lambda: time.monotonic() - started > 0.3)
    assert time.monotonic() - started < 2 and not server.alive()
    assert server.call("click", {})["content"][0]["text"] == "did click"  # a fresh process; no stale reply


def test_client_timeout(server):
    with pytest.raises(TimeoutError):
        server.call("click", {"sleep": 3}, timeout=0.5)
    assert not server.alive()
