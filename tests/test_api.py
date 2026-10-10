"""Tests for the RPC client against a local HTTP server that behaves like the display."""

from __future__ import annotations

import base64
from collections.abc import AsyncGenerator
import hashlib
import secrets
from typing import Any
from unittest.mock import patch

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
import pytest

from custom_components.wall_display_plus.api import (
    WallDisplayAuthError,
    WallDisplayClient,
    WallDisplayError,
    WallDisplayUnsupportedError,
)

REALM = "shellywalldisplay-02a1b2c3d4e5"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class DisplayServer:
    """A minimal HTTP twin of the display, with optional SHA-256 digest auth."""

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
        self.nonce = secrets.token_hex(8)

    def _authorized(self, request: web.Request) -> bool:
        if self.password is None:
            return True
        header = request.headers.get("Authorization", "")
        if not header.startswith("Digest "):
            return False
        fields = dict(part.strip().split("=", 1) for part in header[7:].split(",") if "=" in part)
        fields = {k: v.strip('"') for k, v in fields.items()}
        ha1 = _sha(f"admin:{REALM}:{self.password}")
        ha2 = _sha(f"{request.method}:{fields.get('uri')}")
        expected = _sha(
            f"{ha1}:{fields.get('nonce')}:{fields.get('nc')}:{fields.get('cnonce')}:"
            f"{fields.get('qop')}:{ha2}"
        )
        return fields.get("response") == expected

    async def shelly_handler(self, request: web.Request) -> web.Response:
        if self.shelly == "broken":
            return web.Response(text="not json", content_type="text/plain")
        if self.shelly == "error":
            return web.Response(status=500)
        return web.json_response(self.shelly)

    async def rpc_handler(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.Response(
                status=401,
                headers={
                    "WWW-Authenticate": (
                        f'Digest qop="auth", realm="{REALM}", nonce="{self.nonce}", '
                        "algorithm=SHA-256"
                    )
                },
            )
        frame = await request.json()
        self.calls.append(frame)
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
    assert info.is_wall_display
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


async def test_digest_auth(server, session) -> None:
    twin, host = server
    twin.password = "secret"
    twin.answers["Sys.GetConfig"] = {"result": {"ok": True}}
    assert await WallDisplayClient(session, host, "secret").rpc("Sys.GetConfig") == {"ok": True}
    with pytest.raises(WallDisplayAuthError):
        await WallDisplayClient(session, host, "wrong").rpc("Sys.GetConfig")
    with pytest.raises(WallDisplayAuthError):
        await WallDisplayClient(session, host, None).rpc("Sys.GetConfig")


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
