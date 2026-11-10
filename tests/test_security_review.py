"""Fixes from the privacy and security review of 0.1.8."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from ipaddress import ip_address
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest
from pytest_homeassistant.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD
from homeassistant.core import Context, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from custom_components.wall_display_plus.audio import Announcement, _async_fetch, _without_query
from custom_components.wall_display_plus.const import DOMAIN
from custom_components.wall_display_plus.sync import MAX_HELD_ANNOUNCEMENTS, get_sync_manager

from .conftest import HOST, MAC, FakeDisplay

# Larger than what one read of the stream returns
CLIP_SIZE = 1_300_000


@pytest.fixture
async def audio_url(socket_enabled: None) -> AsyncGenerator[str]:
    """Serve audio in small pieces, without Content-Length, as a slow server does."""

    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "audio/mpeg"})
        await response.prepare(request)
        for _ in range(CLIP_SIZE // 10_000):
            await response.write(b"x" * 10_000)
            await asyncio.sleep(0)
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_get("/clip.mp3", handler)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    yield f"http://127.0.0.1:{server.port}/clip.mp3"
    await server.close()


async def test_download_reads_the_whole_clip(hass: HomeAssistant, audio_url: str) -> None:
    """A clip that arrives in pieces is read to the end, not cut after the first."""
    first = await _async_fetch(async_get_clientsession(hass), audio_url, cache=True)
    second = await _async_fetch(async_get_clientsession(hass), audio_url, cache=True)
    assert len(first.data) == CLIP_SIZE
    # The cache key depends on the content only, not on how it arrived
    assert first.source == second.source


async def test_download_stops_past_the_limit(hass: HomeAssistant, audio_url: str) -> None:
    """A streamed clip larger than the limit is refused while it is downloaded."""
    with (
        patch("custom_components.wall_display_plus.audio.MAX_CLIP_BYTES", 100_000),
        pytest.raises(HomeAssistantError) as err,
    ):
        await _async_fetch(async_get_clientsession(hass), audio_url, cache=True)
    assert err.value.translation_key == "clip_too_large"


def test_credentials_in_url_are_not_shown() -> None:
    """User and password of a URL never reach an error message."""
    assert (
        _without_query("https://user:secret@example.com:8443/a.mp3?token=x#y")
        == "https://example.com:8443/a.mp3"
    )


def _zeroconf(host: str, name: str) -> ZeroconfServiceInfo:
    return ZeroconfServiceInfo(
        ip_address=ip_address(host),
        ip_addresses=[ip_address(host)],
        hostname="localhost.local.",
        name=f"{name}._shelly._tcp.local.",
        port=80,
        type="_shelly._tcp.local.",
        properties={"app": "WallDisplay", "gen": "2"},
    )


def _entry_with_password(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=MAC, data={CONF_HOST: HOST, CONF_PASSWORD: "secret"}
    )
    entry.add_to_hass(hass)
    return entry


@pytest.mark.parametrize("name", ["Kitchen Display", "ShellyWallDisplay-0A0B0C0D0E0F"])
async def test_zeroconf_never_asks_the_password_of_a_known_display(
    hass: HomeAssistant, display: FakeDisplay, name: str
) -> None:
    """A device that reports our display's MAC under another name gets no login.

    Before, it was offered for confirmation, and the password typed there moved
    the display to the device's address.
    """
    entry = _entry_with_password(hass)
    display.auth_enabled = True
    display.password = "secret"
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=_zeroconf("192.0.2.66", name),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == HOST


async def test_zeroconf_confirm_never_moves_a_display(
    hass: HomeAssistant, display: FakeDisplay
) -> None:
    """A display set up while its discovery waited keeps its address."""
    display.auth_enabled = True
    display.password = "secret"
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=_zeroconf("192.0.2.66", "Kitchen Display"),
    )
    assert result["step_id"] == "zeroconf_confirm"
    entry = _entry_with_password(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "secret"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == HOST


async def test_reauth_at_another_display(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """A password accepted by another display at the address is not stored."""
    display.auth_enabled = True
    display.password = "new"
    result = await setup_entry.start_reauth_flow(hass)
    display.mac = "0a0b0c0d0e0f"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_device"
    assert setup_entry.data[CONF_PASSWORD] is None


async def test_waiting_sessions_fetch_their_audio_later(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """Only a few sessions hold their audio; the others fetch it when one is done."""
    manager = get_sync_manager(hass)
    entity_id = er.async_get(hass).async_get_entity_id("media_player", DOMAIN, f"{MAC}_speaker")
    assert entity_id
    speaker = manager.members[entity_id]
    played_before_fetch: list[int] = []

    async def fetch(_hass: HomeAssistant, media_id: str, _target: str | None) -> Announcement:
        played_before_fetch.append(len(display.played))
        return Announcement(media_id, "mp3", b"ID3")

    calls = MAX_HELD_ANNOUNCEMENTS + 2
    with patch("custom_components.wall_display_plus.sync.async_get_announcement", fetch):
        await asyncio.gather(
            *(
                manager.async_play(Context(), f"http://192.0.2.1/{n}.mp3", {}, None, [speaker])
                for n in range(calls)
            )
        )
    assert len(display.played) == calls
    assert played_before_fetch[:MAX_HELD_ANNOUNCEMENTS] == [0] * MAX_HELD_ANNOUNCEMENTS
    assert all(played >= 1 for played in played_before_fetch[MAX_HELD_ANNOUNCEMENTS:])
