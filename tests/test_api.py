"""Tests for the RPC client against a local HTTP server that behaves like the display."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncGenerator, Generator
import hashlib
import json
import logging
from typing import Any
from unittest.mock import patch

from aiohttp import ClientSession, ClientTimeout, web
from aiohttp.test_utils import TestServer
import pytest

from custom_components.wall_display_plus.api import (
    WallDisplayAuthError,
    WallDisplayClient,
    WallDisplayError,
    WallDisplayUnsupportedError,
)

REALM = "ShellyWallDisplay-02A1B2C3D4E5"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


HA2 = _sha("dummy_method:dummy_uri")


class DisplayServer:
    """A minimal HTTP twin of the display, with an optional password.

    It behaves like firmware 2.8.5 of a Wall Display XL: a request without a
    valid login gets HTTP 401, a header that announces digest authentication,
    and the error with what a login is computed with as the whole answer. A
    login in that header is rejected; only the "auth" object in the request is
    accepted. Unlike the firmware, the twin also insists on its current nonce,
    so the tests show that the client picks up a new one.
    """

    def __init__(self) -> None:
        self.password: str | None = None
        self.calls: list[dict[str, Any]] = []
        self.answers: dict[str, Any] = {}
        self.shelly: Any = {
            "mac": "02A1B2C3D4E5",
            "model": "SAWD-3A1XE10EU2",
            "ver": "2.8.2",
            "app": "WallDisplayV2",
            "auth_en": False,
            "fs_free": 23992492032,
        }
        self.list_delays = 0
        # The next requests (RPC or /shelly) fail with this HTTP status or hang
        self.failures: list[int | str] = []
        self.nonce: int | str = 1795469290
        self.requests = 0  # all requests to /rpc, rejected ones included
        self.logins: list[dict[str, Any]] = []
        # What a rejected request is answered with; None is the display's own answer
        self.rejection: Any = None

    def _authorized(self, frame: dict[str, Any]) -> bool:
        if self.password is None:
            return True
        auth = frame.get("auth")
        if not isinstance(auth, dict):
            return False
        self.logins.append(auth)
        ha1 = _sha(f"admin:{REALM}:{self.password}")
        expected = _sha(f"{ha1}:{self.nonce}:{auth.get('nc')}:{auth.get('cnonce')}:auth:{HA2}")
        return (
            auth.get("username") == "admin"
            and auth.get("realm") == REALM
            and auth.get("nonce") == self.nonce
            and auth.get("algorithm") == "SHA-256"
            and auth.get("response") == expected
        )

    def _rejected(self) -> web.Response:
        headers = {
            "WWW-Authenticate": (
                f'Digest realm="{REALM}", qop="auth", nonce="3a952fa3", opaque="", '
                "algorithm=SHA-256"
            )
        }
        if isinstance(self.rejection, str):
            return web.Response(status=401, headers=headers, text=self.rejection)
        asked = self.rejection
        if asked is None:
            asked = {
                "auth_type": "digest",
                "nonce": self.nonce,
                "nc": "1",
                "realm": REALM,
                "algorithm": "SHA-256",
            }
        return web.json_response(
            {"code": 401, "message": json.dumps(asked)}, status=401, headers=headers
        )

    async def _fail(self) -> web.Response | None:
        if not self.failures:
            return None
        failure = self.failures.pop(0)
        if failure == "hang":
            await asyncio.sleep(1)
            return web.Response(status=500)
        return web.Response(status=int(failure))

    async def shelly_handler(self, request: web.Request) -> web.Response:
        if (failed := await self._fail()) is not None:
            return failed
        if self.shelly == "broken":
            return web.Response(text="not json", content_type="text/plain")
        if self.shelly == "error":
            return web.Response(status=500)
        return web.json_response(self.shelly)

    async def rpc_handler(self, request: web.Request) -> web.Response:
        self.requests += 1
        frame = await request.json()
        if not self._authorized(frame):
            return self._rejected()
        self.calls.append(frame)
        if (failed := await self._fail()) is not None:
            return failed
        method = frame["method"]
        if method == "Media.List" and self.list_delays:
            self.list_delays -= 1
            return web.json_response({"id": frame["id"], "result": {"list": []}})
        answer = self.answers.get(method, {"result": {}})
        if answer == "not a dict":
            return web.json_response(["unexpected"])
        return web.json_response({"id": frame["id"], **answer})


@pytest.fixture
async def server(socket_enabled: None) -> AsyncGenerator[tuple[DisplayServer, str]]:
    """Run the display twin on localhost."""
    twin = DisplayServer()
    app = web.Application()
    app.router.add_get("/shelly", twin.shelly_handler)
    app.router.add_post("/rpc", twin.rpc_handler)
    test_server = TestServer(app, host="127.0.0.1")
    await test_server.start_server()
    yield twin, f"127.0.0.1:{test_server.port}"
    await test_server.close()


@pytest.fixture(autouse=True)
def no_retry_pause() -> Generator[None]:
    """Retry without the pause between attempts."""
    with patch("custom_components.wall_display_plus.api.RETRY_PAUSE", 0):
        yield


@pytest.fixture
async def session() -> AsyncGenerator[ClientSession]:
    """A plain client session."""
    async with ClientSession() as client_session:
        yield client_session


async def test_device_info(server, session) -> None:
    _twin, host = server
    info = await WallDisplayClient(session, host, None).get_device_info()
    assert info.mac == "02a1b2c3d4e5"
    assert info.firmware == "2.8.2"
    assert info.is_supported
    assert not info.auth_enabled
    assert info.fs_free == 23992492032


@pytest.mark.parametrize("shelly", ["broken", "error"])
async def test_device_info_errors(server, session, shelly: str) -> None:
    twin, host = server
    twin.shelly = shelly
    with pytest.raises(WallDisplayError):
        await WallDisplayClient(session, host, None).get_device_info()


async def test_unreachable(session, socket_enabled: None) -> None:
    client = WallDisplayClient(session, "127.0.0.1:9", None)
    with pytest.raises(WallDisplayError):
        await client.get_device_info()
    with pytest.raises(WallDisplayError):
        await client.rpc("Sys.GetConfig")


async def test_rpc_frame_and_result(server, session) -> None:
    twin, host = server
    twin.answers["Sys.GetConfig"] = {"result": {"device": {"name": "XL"}}}
    result = await WallDisplayClient(session, host, None).rpc("Sys.GetConfig", {"a": 1})
    assert result == {"device": {"name": "XL"}}
    assert twin.calls[0]["method"] == "Sys.GetConfig"
    assert twin.calls[0]["params"] == {"a": 1}
    assert twin.calls[0]["src"] == "wall_display_plus"


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        ({"error": {"code": -114, "message": "Method not found!"}}, WallDisplayUnsupportedError),
        ({"error": {"code": 404, "message": "No handler"}}, WallDisplayUnsupportedError),
        ({"error": {"code": 401, "message": "Unauthorized"}}, WallDisplayAuthError),
        ({"error": {"code": -103, "message": "Bad payload"}}, WallDisplayError),
        ("not a dict", WallDisplayError),
    ],
)
async def test_rpc_errors(server, session, answer: Any, error: type[Exception]) -> None:
    twin, host = server
    twin.answers["X.Y"] = answer
    with pytest.raises(error):
        await WallDisplayClient(session, host, None).rpc("X.Y")


async def test_result_not_a_dict_is_empty(server, session) -> None:
    twin, host = server
    twin.answers["X.Y"] = {"result": [1, 2]}
    assert await WallDisplayClient(session, host, None).rpc("X.Y") == {}


async def test_password(server, session) -> None:
    twin, host = server
    twin.password = "secret"
    twin.answers["Sys.GetConfig"] = {"result": {"ok": True}}
    assert await WallDisplayClient(session, host, "secret").rpc("Sys.GetConfig") == {"ok": True}
    with pytest.raises(WallDisplayAuthError):
        await WallDisplayClient(session, host, "wrong").rpc("Sys.GetConfig")
    with pytest.raises(WallDisplayAuthError):
        await WallDisplayClient(session, host, None).rpc("Sys.GetConfig")
    assert "secret" not in json.dumps(twin.logins)


async def test_password_login_is_kept(server, session) -> None:
    """The display is asked once what to log in with; later requests log in at once."""
    twin, host = server
    twin.password = "secret"
    client = WallDisplayClient(session, host, "secret")
    await client.rpc("Sys.GetConfig")
    assert twin.requests == 2  # rejected, then logged in
    await client.rpc("Media.GetStatus")
    await client.rpc("Media.SetVolume", {"volume": 5})
    assert twin.requests == 4
    assert [login["nc"] for login in twin.logins] == ["00000001", "00000002", "00000003"]
    assert len({login["cnonce"] for login in twin.logins}) == 3
    assert "auth" not in twin.calls[0]["params"]


async def test_password_after_display_restart(server, session) -> None:
    """A display that asks for a login with new values gets one, also for a write."""
    twin, host = server
    twin.password = "secret"
    client = WallDisplayClient(session, host, "secret")
    await client.rpc("Sys.GetConfig")
    twin.nonce = "a-new-nonce"
    await client.play_alert(5)
    assert [c["method"] for c in twin.calls] == ["Sys.GetConfig", "Media.MediaPlayer.PlayAlert"]
    assert twin.logins[-1]["nonce"] == "a-new-nonce"
    assert twin.logins[-1]["nc"] == "00000001"


async def test_wrong_password_is_asked_again_later(server, session) -> None:
    """After a rejected password the client starts over with the next request."""
    twin, host = server
    twin.password = "secret"
    client = WallDisplayClient(session, host, "wrong")
    with pytest.raises(WallDisplayAuthError):
        await client.rpc("Sys.GetConfig")
    assert twin.requests == 2
    twin.password = "wrong"  # the user set this password on the display
    assert await client.rpc("Sys.GetConfig") == {}


@pytest.mark.parametrize(
    "rejection",
    [
        "",  # no answer text at all
        "Unauthorized",  # no JSON
        "[401]",  # JSON of another shape
        json.dumps({"code": 401, "message": "not json inside"}),
        ["digest"],
        {"auth_type": "basic", "realm": REALM, "nonce": 1, "algorithm": "SHA-256"},
        {"auth_type": "digest", "realm": REALM, "nonce": 1, "algorithm": "MD5"},
        {"auth_type": "digest", "realm": "", "nonce": 1, "algorithm": "SHA-256"},
        {"auth_type": "digest", "realm": REALM, "nonce": True, "algorithm": "SHA-256"},
        {"auth_type": "digest", "realm": REALM, "algorithm": "SHA-256"},
    ],
)
async def test_unknown_login_request_is_an_auth_error(server, session, rejection: Any) -> None:
    """A rejection the client cannot answer is reported, and nothing is signed."""
    twin, host = server
    twin.password = "secret"
    twin.rejection = rejection
    with pytest.raises(WallDisplayAuthError):
        await WallDisplayClient(session, host, "secret").rpc("Sys.GetConfig")
    assert twin.requests == 1
    assert twin.logins == []


async def test_media_methods(server, session) -> None:
    twin, host = server
    twin.answers["Media.List"] = {
        "result": {
            "list": [
                {"id": 3, "filename": "a.mp3", "type": "ALERT", "size": 28355},
                {"id": 4, "type": "AUDIO"},  # incomplete entries are skipped
            ]
        }
    }
    twin.answers["Media.MediaPlayer.PlayAlert"] = {
        "result": {"audio_clip": {"enable": True, "media_meta": {"duration": 2361}}}
    }
    twin.answers["Motion.GetConfig"] = {"result": {"wake_screen": True}}
    twin.answers["Media.GetStatus"] = {"result": {"playback": {"enable": False}}}
    client = WallDisplayClient(session, host, None)

    items = await client.list_media()
    assert [(i.id, i.filename, i.type, i.size) for i in items] == [(3, "a.mp3", "ALERT", 28355)]
    assert (await client.find_media("a.mp3")).id == 3
    assert (await client.play_alert(3)).duration == pytest.approx(2.361)
    await client.delete_media(3)
    await client.stop()
    assert await client.get_motion_config() == {"wake_screen": True}
    assert await client.get_media_status() == {"playback": {"enable": False}}
    await client.set_wake_screen(False)

    methods = [(c["method"], c["params"]) for c in twin.calls]
    assert ("Media.Delete", {"id": 3}) in methods
    assert ("Motion.GetConfig", {"id": 0}) in methods
    assert ("Motion.SetConfig", {"id": 0, "config": {"wake_screen": False}}) in methods
    assert ("Media.MediaPlayer.Stop", {}) in methods


async def test_play_alert_without_duration(server, session) -> None:
    twin, host = server
    twin.answers["Media.MediaPlayer.PlayAlert"] = {"result": {}}
    assert (await WallDisplayClient(session, host, None).play_alert(1)).duration == 0


async def test_play_alert_log_names_nothing_played(server, session, caplog) -> None:
    """The debug log shows how a clip plays, not what played (answer of an X2i)."""
    twin, host = server
    twin.answers["Media.MediaPlayer.PlayAlert"] = {
        "result": {
            "audio_clip": {
                "enable": True,
                "volume": 3,
                "media_type": "ALERT",
                "media_meta": {"title": "Clip title", "artist": "", "thumb": "data:"},
            },
            "resume": {"enable": False, "media_type": "RADIO", "media_meta": {"title": "Station"}},
        }
    }
    caplog.set_level(logging.DEBUG, logger="custom_components.wall_display_plus.api")
    assert (await WallDisplayClient(session, host, None).play_alert(11)).duration == 0
    assert "PlayAlert 11: ALERT at volume 3, duration not reported" in caplog.text
    assert "Clip title" not in caplog.text and "Station" not in caplog.text


async def test_find_media_waits_for_the_list(server, session) -> None:
    twin, host = server
    twin.list_delays = 2
    twin.answers["Media.List"] = {"result": {"list": [{"id": 7, "filename": "x", "type": "ALERT"}]}}
    with patch("custom_components.wall_display_plus.api.asyncio.sleep"):
        client = WallDisplayClient(session, host, None)
        assert (await client.find_media("x")).id == 7
        twin.list_delays = 99
        assert await client.find_media("x", attempts=3) is None


async def test_upload_in_chunks(server, session) -> None:
    twin, host = server
    data = bytes(range(256)) * 4
    with patch("custom_components.wall_display_plus.api.UPLOAD_CHUNK_SIZE", 500):
        await WallDisplayClient(session, host, None).upload(
            "homeassistant_abcdef_x.mp3", "ALERT", data
        )
    puts = [c["params"] for c in twin.calls if c["method"] == "Media.PutMedia"]
    assert [(p["append"], p["last"]) for p in puts] == [(False, False), (True, False), (True, True)]
    assert all(p["filename"] == "homeassistant_abcdef_x.mp3" and p["type"] == "ALERT" for p in puts)
    assert base64.b64decode("".join(p["data"] for p in puts)) == data
    assert twin.calls[-1]["method"] == "Media.Reload"


async def test_upload_empty(server, session) -> None:
    twin, host = server
    await WallDisplayClient(session, host, None).upload("homeassistant_abcdef_e.mp3", "ALERT", b"")
    puts = [c["params"] for c in twin.calls if c["method"] == "Media.PutMedia"]
    assert len(puts) == 1 and puts[0]["last"] is True and puts[0]["data"] == ""


async def test_device_info_uptime_and_memory(server, session) -> None:
    twin, host = server
    twin.shelly.update({"uptime": 13395, "app_uptime": 13369, "ram_free": 73235584})
    info = await WallDisplayClient(session, host, None).get_device_info()
    assert (info.uptime, info.app_uptime, info.ram_free) == (13395, 13369, 73235584)
    twin.shelly.update({"uptime": True, "ram_free": "x"})  # wrong types are ignored
    info = await WallDisplayClient(session, host, None).get_device_info()
    assert info.uptime is None and info.ram_free is None


async def test_screen_volume_restart_temperature(server, session) -> None:
    twin, host = server
    twin.answers["Ui.GetConfig"] = {"result": {"brightness": {"auto": True}}}
    twin.answers["Sys.GetInternalTemperatures"] = {
        "result": {"sht3x": -275.15, "thermal-cpufreq-0": 44.375}
    }
    client = WallDisplayClient(session, host, None)
    assert await client.get_ui_config() == {"brightness": {"auto": True}}
    await client.set_ui_config({"brightness": {"auto": False}})
    await client.set_volume(8)
    await client.restart_app()
    assert await client.get_cpu_temperature() == 44.375
    # A Wall Display reports it under another name
    twin.answers["Sys.GetInternalTemperatures"] = {
        "result": {"sht3x": 27.5, "cpu-sysrst": 37.5, "pmic-sysrst": 38.088}
    }
    assert await client.get_cpu_temperature() == 37.5
    twin.answers["Sys.GetInternalTemperatures"] = {"result": {"sht3x": -275.15}}
    assert await client.get_cpu_temperature() is None
    methods = [(c["method"], c["params"]) for c in twin.calls]
    assert ("Ui.SetConfig", {"config": {"brightness": {"auto": False}}}) in methods
    assert ("Media.SetVolume", {"volume": 8}) in methods
    assert ("Sys.RestartApplication", {}) in methods


async def test_read_is_asked_once_more(server, session) -> None:
    """A read that gets no answer is sent a second time and succeeds."""
    twin, host = server
    twin.answers["Media.GetStatus"] = {"result": {"volume": 7}}
    twin.failures = [500]
    assert await WallDisplayClient(session, host, None).rpc("Media.GetStatus") == {"volume": 7}
    assert [c["method"] for c in twin.calls] == ["Media.GetStatus", "Media.GetStatus"]


async def test_read_hanging_is_asked_once_more(server, session) -> None:
    """A read that hangs past the timeout is sent a second time."""
    twin, host = server
    twin.failures = ["hang"]
    client = WallDisplayClient(session, host, None)
    assert await client.rpc("Ui.GetConfig", client_timeout=ClientTimeout(total=0.2)) == {}
    assert len(twin.calls) == 2


async def test_read_gives_up_after_two_attempts(server, session) -> None:
    """Two unanswered attempts fail, and a timeout names itself in the error."""
    twin, host = server
    twin.failures = ["hang", "hang"]
    client = WallDisplayClient(session, host, None)
    with pytest.raises(WallDisplayError, match=r"^Motion\.GetConfig failed: TimeoutError$"):
        await client.rpc("Motion.GetConfig", client_timeout=ClientTimeout(total=0.2))
    assert len(twin.calls) == 2


async def test_write_is_not_repeated(server, session) -> None:
    """A write is sent once only: a repeated upload chunk or alert would do harm."""
    twin, host = server
    twin.failures = [500]
    with pytest.raises(WallDisplayError):
        await WallDisplayClient(session, host, None).play_alert(5)
    assert len(twin.calls) == 1


async def test_device_info_is_asked_once_more(server, session) -> None:
    twin, host = server
    twin.failures = [503]
    info = await WallDisplayClient(session, host, None).get_device_info()
    assert info.mac == "02a1b2c3d4e5"
