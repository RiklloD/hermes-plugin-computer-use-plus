"""The screen share: token, CORS, streams, lease fencing, Stop and Take over (ported from the 0.4 review tests)."""

from __future__ import annotations

import json
import time

import pytest

from cup_plugin import commands, live
from tests.test_codex import fake, tool  # noqa: F401  (fixtures)


@pytest.fixture
def share(fake, monkeypatch, tmp_path):  # noqa: F811
    """The screen-share server on a free port, with a fake window capture and its own token file."""
    from cup_plugin import stream
    shots = {"n": 0}

    def fake_capture(sid, pid, win, w):
        shots["n"] += 1
        return (b"\xff\xd8jpeg-%d" % (shots["n"] // 3), 640, 480, "window")
    monkeypatch.setattr(live, "capture_window", fake_capture)
    monkeypatch.setattr(stream, "PORTS", range(47900, 47950))
    monkeypatch.setattr(stream, "_token_path", lambda: tmp_path / "share-token")
    stream._token_cache.clear()
    stream.stop()
    port = stream.start()
    yield {"port": port, "shots": shots, "token": stream.token()}
    stream.stop()
    stream._token_cache.clear()
    live._sessions.clear()


def observe():
    return tool("get_app_state", {"app": "Notes"})

def _get(share, path, origin="null", token=True, headers=None):
    import urllib.request
    h = {**({"Origin": origin} if origin is not None else {}), **(headers or {})}
    if token:
        h["Authorization"] = f"Bearer {share['token']}"
    req = urllib.request.Request(f"http://127.0.0.1:{share['port']}{path}", headers=h)
    with urllib.request.urlopen(req, timeout=3) as r:
        return r.status, dict(r.headers), r.read()


def _post(share, path, header=True, token=True):
    import urllib.error
    import urllib.request
    h = {"X-Computer-Use-Plus": "1"} if header else {}
    if token:
        h["Authorization"] = f"Bearer {share['token']}"
    req = urllib.request.Request(f"http://127.0.0.1:{share['port']}{path}", data=b"", method="POST", headers=h)
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _wait(pred, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _state(share, viewer="v1"):
    return json.loads(_get(share, f"/v1/state?viewer={viewer}")[2])


def _status(share, path, **kw):
    import urllib.error
    try:
        return _get(share, path, **kw)[0]
    except urllib.error.HTTPError as e:
        return e.code


def _open_stream(share, path):
    import socket
    sock = socket.create_connection(("127.0.0.1", share["port"]), timeout=3)
    sock.sendall(f"GET {path}&t={share['token']} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
    return sock



def test_every_route_needs_the_token_and_a_loopback_host(share):
    import urllib.error
    assert _status(share, "/v1/state", token=False) == 401
    assert _status(share, "/v1/state", headers={"Host": "evil.example:47900"}) == 403
    assert _post(share, "/v1/stop?session=t1", token=False)[0] == 401
    assert not live.stop_requested("t1")
    with pytest.raises(urllib.error.HTTPError):
        _get(share, "/v1/pair", token=False)  # a plain client is not the Desktop renderer
    ua = {"User-Agent": "Mozilla/5.0 Hermes/1.0 Chrome/138 Electron/37.0.0"}
    assert _status(share, "/v1/pair", token=False, origin="https://evil.example", headers=ua) == 403
    body = json.loads(_get(share, "/v1/pair", token=False, origin="null", headers=ua)[2])
    assert body["token"] == share["token"]
    # Electron 40 sends no Origin from the file:// page, only Sec-Fetch-Mode: cors.
    assert _status(share, "/v1/pair", token=False, origin=None, headers=ua) == 403
    cors = {**ua, "Sec-Fetch-Mode": "cors"}
    assert json.loads(_get(share, "/v1/pair", token=False, origin=None, headers=cors)[2])["token"] == share["token"]
    assert json.loads(commands.handle("token"))["token"] == share["token"]


def test_cors_only_for_the_desktop_origin(share):
    _, headers, _ = _get(share, "/v1/state", origin="null")
    assert headers.get("Access-Control-Allow-Origin") == "null"
    _, headers, _ = _get(share, "/v1/state", origin="https://evil.example")
    assert "Access-Control-Allow-Origin" not in headers


def test_stream_sends_frames_and_ends_when_the_client_leaves_even_without_frames(share, monkeypatch):
    from cup_plugin import stream
    assert _status(share, "/v1/stream?window=7") == 404  # nobody used window 7 yet
    observe()
    live.activity("t1", "observe", "Looking", phase="end", status="confirmed", ok=True)
    sock = _open_stream(share, "/v1/stream?window=7&fps=10")
    buf, end = b"", time.monotonic() + 3
    while buf.count(b"--cupframe") < 2 and time.monotonic() < end:
        buf += sock.recv(65536)
    assert b"multipart/x-mixed-replace" in buf and buf.count(b"Content-Type: image/jpeg") >= 2
    assert _wait(lambda: stream.open_streams() == 1)
    sock.close()
    assert _wait(lambda: stream.open_streams() == 0)
    # Capture unavailable: nothing is ever written, yet a closed client still ends the stream.
    monkeypatch.setattr(live, "capture_window", lambda *a: None)
    sock = _open_stream(share, "/v1/stream?window=7&fps=10")
    assert _wait(lambda: stream.open_streams() == 1)
    sock.close()
    assert _wait(lambda: stream.open_streams() == 0)


def test_streams_close_when_the_server_stops_and_when_the_session_ends(share):
    from cup_plugin import stream
    observe()
    live.activity("t1", "observe", "Looking", phase="end")
    sock = _open_stream(share, "/v1/stream?window=7&fps=10")
    assert _wait(lambda: stream.open_streams() == 1)
    live.ended("t1")
    real = time.time
    live_time = live.time
    try:
        live_time.time = lambda: real() + live.ENDED_GRACE + 1
        assert _wait(lambda: stream.open_streams() == 0, timeout=4)
        assert live.window_info(7) is None
        assert _status(share, "/v1/frame?window=7") == 404  # an ended session's windows are not served
    finally:
        live_time.time = real
    sock.close()


def test_one_capture_producer_is_shared_by_streams_of_a_window(share):
    from cup_plugin import stream
    observe()
    live.activity("t1", "observe", "Looking", phase="end")
    a = _open_stream(share, "/v1/stream?window=7&fps=10&width=640")
    b = _open_stream(share, "/v1/stream?window=7&fps=10&width=640")
    assert _wait(lambda: stream.open_streams() == 2)
    assert len(stream._producers) == 1
    a.close()
    b.close()
    assert _wait(lambda: stream.open_streams() == 0)


def test_no_pictures_while_a_human_holds_the_desktop(share, monkeypatch):
    observe()
    live.activity("t1", "observe", "Looking", phase="end")
    monkeypatch.setattr(live, "lease_state", lambda profile=None: (True, 3))
    before = share["shots"]["n"]
    assert _status(share, "/v1/frame?window=7") == 423
    assert share["shots"]["n"] == before


def test_a_frame_captured_across_a_takeover_is_dropped(share, monkeypatch):
    observe()
    live.activity("t1", "observe", "Looking", phase="end")
    state = {"held": False, "epoch": 1}
    monkeypatch.setattr(live, "lease_state", lambda profile=None: (state["held"], state["epoch"]))

    def racy(*a):
        state.update(held=True, epoch=2)  # the person takes over while the window is being captured
        return (b"\xff\xd8secret", 1, 1, "window")
    monkeypatch.setattr(live, "capture_window", racy)
    assert _status(share, "/v1/frame?window=7") == 423


def test_unreadable_lease_fails_closed(monkeypatch):
    import tools.bot_desktop.lease as lease
    monkeypatch.setattr(lease, "get", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    assert live.lease_state() == (True, -1)


def test_nothing_is_captured_by_actions(share):
    observe()
    tool("click", {"app": "Notes", "element_index": "3"})
    assert share["shots"]["n"] == 0


def test_readiness_separates_viewer_and_fresh_pictures(share):
    r = json.loads(commands.handle("status"))
    assert r["screen_share_port"] == share["port"]
    assert r["viewer_connected"] is False and r["ready_to_watch"] is False
    assert any("Desktop half" in n for n in r["notes"])
    _state(share)
    r = json.loads(commands.handle("status"))
    assert r["viewer_connected"] is True and r["ready_to_watch"] is False  # no fresh frame yet
    observe()
    live.activity("t1", "observe", "Looking", phase="end")
    assert _status(share, "/v1/frame?window=7") == 200
    assert json.loads(commands.handle("status"))["ready_to_watch"] is True


def test_posts_need_the_custom_header(share):
    assert _post(share, "/v1/stop?session=t1", header=False)[0] == 403
    assert not live.stop_requested("t1")


def test_take_over_pauses_the_agent_until_hand_back(share):
    observe()
    live.activity("t1", "observe", "Looking", phase="end")
    assert _post(share, "/v1/takeover?viewer=v9&session=t1")[0] == 200
    try:
        assert json.loads(observe())["code"] == "human_has_control"
        assert _state(share)["sessions"][0]["human_has_control"] is True
    finally:
        assert _post(share, "/v1/handback?viewer=v9")[0] == 200
    assert observe()["_multimodal"]


def test_a_vanished_viewer_releases_its_takeover_even_if_others_poll(share, monkeypatch):
    from tools.bot_desktop import lease
    _state(share, viewer="v7")
    _post(share, "/v1/takeover?viewer=v7")
    assert lease.human_holds()
    live.release_abandoned_takeover()
    assert lease.human_holds()  # its viewer is still polling
    real = time.monotonic
    monkeypatch.setattr(live.time, "monotonic", lambda: real() + live.TAKEOVER_TTL + 1)
    live.seen_viewer("other")  # another viewer polling does not keep v7's hold alive
    live.release_abandoned_takeover()
    assert not lease.human_holds()


def test_live_module_has_no_removed_viewer_protocol():
    assert not hasattr(live, "subscribe") and not hasattr(live, "unsubscribe")


def test_cached_frame_is_not_resent_while_a_person_holds_the_desktop(share, monkeypatch):
    from cup_plugin import stream
    observe()
    live.activity("t1", "observe", "Looking", phase="end")
    lease = {"held": False, "epoch": 1}
    monkeypatch.setattr(live, "lease_state", lambda profile=None: (lease["held"], lease["epoch"]))
    monkeypatch.setattr(live, "capture_window", lambda *a: (b"\xff\xd8OLD-FRAME" + b"x" * 50, 1, 1, "window"))
    sock = _open_stream(share, "/v1/stream?window=7&fps=10")
    buf, end = b"", time.monotonic() + 3
    while b"OLD-FRAME" not in buf and time.monotonic() < end:
        buf += sock.recv(65536)
    assert b"OLD-FRAME" in buf
    lease.update(held=True, epoch=2)
    sock.settimeout(0.3)
    later, end = b"", time.monotonic() + 3.0  # past the 2 s keep-alive resend
    while time.monotonic() < end:
        try:
            later += sock.recv(65536)
        except OSError:
            pass
    assert b"OLD-FRAME" not in later
    sock.close()
    assert _wait(lambda: stream.open_streams() == 0)
