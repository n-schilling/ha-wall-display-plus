"""Downloads of URLs: only administrators, only public addresses (review of 0.1.9)."""

from __future__ import annotations

from collections.abc import AsyncGenerator
import socket
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest
from pytest_homeassistant.common import MockConfigEntry, MockUser
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    DOMAIN as MP_DOMAIN,
    SERVICE_PLAY_MEDIA,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from homeassistant.helpers import entity_registry as er

from custom_components.wall_display_plus import audio
from custom_components.wall_display_plus.audio import (
    BlockedAddressError,
    _PublicResolver,
    is_public,
)

from .conftest import MAC, FakeDisplay

# The real session, taken before conftest replaces it for the other tests
REAL_PUBLIC_SESSION = audio._public_session

URL = "https://example.com/ding.mp3"
TTS = "media-source://tts/tts.piper?message=Hallo&language=de_DE"
AUDIO = {"content": b"ID3", "headers": {"Content-Type": "audio/mpeg"}}


def _speaker(hass: HomeAssistant) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        MP_DOMAIN, "wall_display_plus", f"{MAC}_speaker"
    )
    assert entity_id
    return entity_id


async def _play(hass: HomeAssistant, media_id: str, context: Context | None = None) -> None:
    await hass.services.async_call(
        MP_DOMAIN,
        SERVICE_PLAY_MEDIA,
        {
            ATTR_ENTITY_ID: _speaker(hass),
            ATTR_MEDIA_CONTENT_ID: media_id,
            ATTR_MEDIA_CONTENT_TYPE: "music",
        },
        blocking=True,
        context=context,
    )


# -- who may play a URL --


@pytest.fixture
async def user(hass: HomeAssistant, hass_owner_user: MockUser) -> Any:
    """A user without administrator rights who may control devices (not the owner)."""
    return await hass.auth.async_create_user("User", group_ids=["system-users"])


async def test_user_without_admin_rights_cannot_play_a_url(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    user: Any,
    display: FakeDisplay,
    setup_entry: MockConfigEntry,
) -> None:
    """Home Assistant downloads a URL itself; only administrators may make it."""
    aioclient_mock.get(URL, **AUDIO)
    with pytest.raises(Unauthorized):
        await _play(hass, URL, Context(user_id=user.id))
    assert aioclient_mock.call_count == 0
    assert not display.uploads


async def test_admin_and_automations_play_a_url(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_admin_user: MockUser,
    display: FakeDisplay,
    setup_entry: MockConfigEntry,
) -> None:
    """An administrator, and an automation or script (no user), play URLs."""
    aioclient_mock.get(URL, **AUDIO)
    await _play(hass, URL, Context(user_id=hass_admin_user.id))
    await _play(hass, URL, Context())
    assert len(display.played) == 2


async def test_user_without_admin_rights_plays_text_to_speech(
    hass: HomeAssistant,
    user: Any,
    display: FakeDisplay,
    setup_entry: MockConfigEntry,
) -> None:
    """Text to speech and media sources stay open to every user."""
    with patch(
        "custom_components.wall_display_plus.audio.tts.async_get_media_source_audio",
        AsyncMock(return_value=("mp3", b"ID3fake-audio")),
    ):
        await _play(hass, TTS, Context(user_id=user.id))
    assert display.played


async def test_unknown_user_cannot_play_a_url(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    display: FakeDisplay,
    setup_entry: MockConfigEntry,
) -> None:
    """A context with a user that no longer exists is no administrator."""
    with pytest.raises(Unauthorized):
        await _play(hass, URL, Context(user_id="gone"))
    assert aioclient_mock.call_count == 0


# -- which addresses --


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/a.mp3",
        "http://10.0.0.1/a.mp3",
        "http://192.168.1.1:8443/a.mp3",
        "http://172.30.32.1:8099/a.mp3",
        "http://169.254.169.254/latest",
        "http://100.64.0.1/a.mp3",
        "http://224.0.0.1/a.mp3",
        "http://0.0.0.0/a.mp3",
        "http://[::1]/a.mp3",
        "http://[fe80::1]/a.mp3",
        "http://[fd00::1]/a.mp3",
        "http://[::ffff:127.0.0.1]/a.mp3",
    ],
)
async def test_local_addresses_are_refused_before_any_request(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    display: FakeDisplay,
    setup_entry: MockConfigEntry,
    url: str,
) -> None:
    """Addresses of the local network, the host and the app network are never fetched."""
    aioclient_mock.get(url, **AUDIO)
    with pytest.raises(ServiceValidationError) as err:
        await _play(hass, url)
    assert err.value.translation_key == "url_not_public"
    assert aioclient_mock.call_count == 0


async def test_redirect_to_a_local_address_is_refused(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    display: FakeDisplay,
    setup_entry: MockConfigEntry,
) -> None:
    """A public URL that redirects into the local network does not reach it."""
    aioclient_mock.get(URL, status=302, headers={"Location": "http://127.0.0.1/relay/0?turn=off"})
    aioclient_mock.get("http://127.0.0.1/relay/0?turn=off", **AUDIO)
    with pytest.raises(ServiceValidationError, match="public addresses"):
        await _play(hass, URL)
    assert [str(call[1]) for call in aioclient_mock.mock_calls] == [URL]


async def test_redirects_between_public_addresses_are_followed(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    display: FakeDisplay,
    setup_entry: MockConfigEntry,
) -> None:
    """A redirect to another public address is followed, also a relative one."""
    aioclient_mock.get(URL, status=301, headers={"Location": "https://example.org/x"})
    aioclient_mock.get("https://example.org/x", status=307, headers={"Location": "/ding.mp3"})
    aioclient_mock.get("https://example.org/ding.mp3", **AUDIO)
    await _play(hass, URL)
    assert display.played


async def test_too_many_redirects(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    display: FakeDisplay,
    setup_entry: MockConfigEntry,
) -> None:
    """A redirect loop ends after a few hops."""
    aioclient_mock.get(URL, status=302, headers={"Location": URL})
    with pytest.raises(ServiceValidationError):
        await _play(hass, URL)
    assert aioclient_mock.call_count == audio.MAX_REDIRECTS + 1


async def test_redirect_to_another_scheme_is_refused(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    display: FakeDisplay,
    setup_entry: MockConfigEntry,
) -> None:
    """Only http and https, also after a redirect."""
    aioclient_mock.get(URL, status=302, headers={"Location": "file:///etc/passwd"})
    with pytest.raises(ServiceValidationError):
        await _play(hass, URL)


async def test_media_source_pointing_into_the_network_is_refused(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    display: FakeDisplay,
    setup_entry: MockConfigEntry,
) -> None:
    """A media source with an absolute URL follows the rules of a URL."""
    item = MagicMock(url="http://10.0.0.5/stream.mp3", mime_type="audio/mpeg")
    with (
        patch(
            "custom_components.wall_display_plus.audio.media_source.async_resolve_media",
            AsyncMock(return_value=item),
        ),
        pytest.raises(ServiceValidationError, match="public addresses"),
    ):
        await _play(hass, "media-source://radio_browser/abc")
    assert aioclient_mock.call_count == 0


async def test_media_source_with_a_public_url(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    display: FakeDisplay,
    setup_entry: MockConfigEntry,
) -> None:
    """A media source with a public URL (a radio station, for example) plays."""
    item = MagicMock(url=URL, mime_type="audio/mpeg")
    aioclient_mock.get(URL, **AUDIO)
    with patch(
        "custom_components.wall_display_plus.audio.media_source.async_resolve_media",
        AsyncMock(return_value=item),
    ):
        await _play(hass, "media-source://radio_browser/abc")
    assert display.played


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("93.184.215.14", True),
        ("2606:2800:21f:cb07:6820:80da:af6b:8b2c", True),
        ("127.0.0.1", False),
        ("10.1.2.3", False),
        ("fe80::1%eth0", False),
        ("::ffff:10.0.0.1", False),
        ("239.255.255.250", False),
    ],
)
def test_is_public(address: str, public: bool) -> None:
    """Private, local, reserved and multicast addresses are not public."""
    assert is_public(address) is public


def _result(host: str) -> dict[str, Any]:
    return {
        "hostname": "example.com",
        "host": host,
        "port": 80,
        "family": socket.AF_INET,
        "proto": 0,
        "flags": 0,
    }


async def test_resolver_refuses_a_name_with_a_local_address() -> None:
    """A name is refused if any of its addresses is local (DNS rebinding)."""
    resolver = _PublicResolver()
    with (
        patch.object(
            resolver._resolver,
            "resolve",
            AsyncMock(return_value=[_result("93.184.215.14"), _result("192.168.1.1")]),
        ),
        pytest.raises(BlockedAddressError),
    ):
        await resolver.resolve("example.com", 80)
    with patch.object(
        resolver._resolver, "resolve", AsyncMock(return_value=[_result("93.184.215.14")])
    ):
        assert await resolver.resolve("example.com", 80) == [_result("93.184.215.14")]
    with (
        patch.object(resolver._resolver, "resolve", AsyncMock(return_value=[])),
        pytest.raises(BlockedAddressError),
    ):
        await resolver.resolve("example.com", 80)
    await resolver.close()


@pytest.fixture
async def local_server(socket_enabled: None) -> AsyncGenerator[TestServer]:
    """A server on the loopback interface that would answer with audio."""
    requests = 0

    async def handler(request: web.Request) -> web.Response:
        nonlocal requests
        requests += 1
        return web.Response(body=b"ID3", content_type="audio/mpeg")

    app = web.Application()
    app.router.add_get("/clip.mp3", handler)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    server.requests = lambda: requests  # type: ignore[attr-defined]
    yield server
    await server.close()


async def test_real_session_does_not_connect_to_a_local_name(
    hass: HomeAssistant, local_server: TestServer
) -> None:
    """The session of a download resolves names itself and refuses local ones."""
    with (
        patch.object(audio, "_public_session", REAL_PUBLIC_SESSION),
        pytest.raises(ServiceValidationError),
    ):
        await audio._async_fetch_public(
            hass, f"http://localhost:{local_server.port}/clip.mp3", cache=True
        )
    assert local_server.requests() == 0  # type: ignore[attr-defined]
