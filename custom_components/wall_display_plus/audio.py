"""Turn what was asked to play into audio, once for all displays that play it."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
import hashlib
import io
import ipaddress
import json
import logging
import mimetypes
import socket
from typing import Any

from aiohttp import (
    ClientError,
    ClientResponse,
    ClientResponseError,
    ClientSession,
    ClientTimeout,
    TCPConnector,
    hdrs,
)
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.resolver import DefaultResolver
import mutagen
import yarl

from homeassistant.components import media_source, tts
from homeassistant.components.media_player.browse_media import async_process_play_media_url
from homeassistant.components.media_source.const import URI_SCHEME
from homeassistant.components.media_source.error import Unresolvable
from homeassistant.components.tts.const import DOMAIN as TTS_DOMAIN
from homeassistant.components.tts.media_source import parse_media_source_id
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util.ssl import client_context

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

MAX_CLIP_BYTES = 20 * 1024 * 1024
FETCH_CHUNK_BYTES = 64 * 1024
FETCH_TIMEOUT = ClientTimeout(total=30)
# Redirects a URL may take; each target is checked like the URL itself.
MAX_REDIRECTS = 3
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# Bit rate for estimating the length of audio whose format mutagen cannot
# read. Speech is usually encoded with less, so the estimate errs on the long
# side: the next announcement waits a little longer instead of cutting in.
ESTIMATE_BITS_PER_SECOND = 32_000


@dataclass(frozen=True, slots=True)
class Announcement:
    """Audio to play and what its cache key is derived from (None: never cached).

    Each display derives its own key from the source with its own secret.
    """

    source: str | None
    extension: str
    data: bytes
    duration: float = 0.0


def audio_duration(data: bytes) -> float:
    """Return the length of the audio in seconds.

    The display does not report the length of alert clips, but the speaker
    needs it: to wait before the next clip, to go back to the previous volume
    and to delete an uncached clip only after it was played.
    """
    try:
        info = mutagen.File(io.BytesIO(data))
    except Exception as err:
        _LOGGER.debug("Audio length unreadable: %s", _describe(err))
        info = None
    length = getattr(getattr(info, "info", None), "length", None)
    if isinstance(length, int | float) and length > 0:
        return float(length)
    return len(data) * 8 / ESTIMATE_BITS_PER_SECOND


def _is_tts(media_id: str) -> bool:
    return media_id.startswith(f"{URI_SCHEME}{TTS_DOMAIN}/")


def tts_message(media_id: str) -> str | None:
    """Return the text of a text-to-speech request, None for anything else."""
    if not _is_tts(media_id):
        return None
    try:
        parsed = parse_media_source_id(media_id)
    except Unresolvable:
        return None
    message = parsed.get("message")
    return message if isinstance(message, str) else None


async def async_check_permission(
    hass: HomeAssistant, context: Context | None, media_id: str
) -> None:
    """Let only administrators play a URL; automations and scripts may.

    Home Assistant itself downloads a URL, from wherever it can reach. Text to
    speech and media sources are open to every user, as in other players.
    """
    if media_source.is_media_source_id(media_id) or context is None or context.user_id is None:
        return
    user = await hass.auth.async_get_user(context.user_id)
    if user is None or not user.is_admin:
        raise Unauthorized(context=context)


async def async_get_announcement(
    hass: HomeAssistant, media_id: str, target_entity_id: str | None
) -> Announcement:
    """Get the audio and its length for text to speech, a media source item or a URL."""
    announcement = await _async_get_audio(hass, media_id, target_entity_id)
    duration = await hass.async_add_executor_job(audio_duration, announcement.data)
    return replace(announcement, duration=duration)


async def _async_get_audio(
    hass: HomeAssistant, media_id: str, target_entity_id: str | None
) -> Announcement:
    if media_source.is_media_source_id(media_id):
        if _is_tts(media_id):
            try:
                return await _async_get_tts_audio(hass, media_id)
            except Unresolvable:
                # Not a message ID (for example a one-time stream of the
                # voice assistant): resolve it like any media source and
                # do not cache it.
                return await _async_resolve_and_fetch(hass, media_id, target_entity_id, cache=False)
        return await _async_resolve_and_fetch(hass, media_id, target_entity_id, cache=True)
    # Anything else must be a plain http(s) URL. It is fetched as given,
    # never signed (that would open authenticated Home Assistant paths), and
    # only from public addresses (see _async_fetch_public).
    parsed = yarl.URL(media_id)
    if not parsed.is_absolute() or parsed.scheme not in ("http", "https"):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unsupported_media",
            translation_placeholders={"media_id": _without_query(media_id)},
        )
    return await _async_fetch_public(hass, media_id, cache=True)


async def _async_resolve_and_fetch(
    hass: HomeAssistant, media_id: str, target_entity_id: str | None, *, cache: bool
) -> Announcement:
    """Resolve a media source item and download it."""
    item = await media_source.async_resolve_media(hass, media_id, target_entity_id)
    if not (item.mime_type or "").startswith("audio/"):
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_audio")
    if not yarl.URL(item.url).is_absolute():
        # A path of this instance (local media, for example): it is signed
        # and fetched from Home Assistant itself.
        url = async_process_play_media_url(hass, item.url)
        return await _async_fetch(async_get_clientsession(hass), url, cache=cache)
    # A media source may point elsewhere (a radio stream, for example): the
    # same rules as for a URL apply.
    return await _async_fetch_public(hass, item.url, cache=cache)


async def _async_get_tts_audio(hass: HomeAssistant, media_id: str) -> Announcement:
    parsed = parse_media_source_id(media_id)
    source: str | None
    if "stream" in parsed:
        source = None  # a one-time stream, nothing to cache
    else:
        options: dict[str, Any] = dict(parsed["options"])
        options.pop("use_file_cache", None)
        source = json.dumps({"message": parsed["message"], **options}, sort_keys=True, default=str)
    try:
        extension, data = await tts.async_get_media_source_audio(hass, media_id)
    except HomeAssistantError:
        raise
    except Exception as err:
        # TTS engines fail in many ways. Pass on only the type: the
        # engine's message may quote the text to speak.
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="tts_failed",
            translation_placeholders={"error": type(err).__name__},
        ) from err
    return Announcement(source=source, extension=extension, data=data)


class BlockedAddressError(ClientError):
    """The URL points to an address that is not public."""


def is_public(address: str) -> bool:
    """Return whether an IP address is public (not local, private or reserved)."""
    ip = ipaddress.ip_address(address.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


class _PublicResolver(AbstractResolver):
    """Resolve names like aiohttp, but refuse names with a non-public address.

    The check runs on the very addresses aiohttp then connects to, so a name
    that resolves to another address a moment later (DNS rebinding) gains
    nothing.
    """

    def __init__(self) -> None:
        self._resolver = DefaultResolver()

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        results = await self._resolver.resolve(host, port, family)
        if not results or not all(is_public(result["host"]) for result in results):
            raise BlockedAddressError(f"{host} has an address that is not public")
        return results

    async def close(self) -> None:
        await self._resolver.close()


def _check_url(url: yarl.URL) -> None:
    """Refuse a URL that is not http(s) or names a non-public IP address.

    Host names are checked by _PublicResolver when connecting.
    """
    if url.scheme not in ("http", "https") or not url.host:
        raise BlockedAddressError("not an http(s) URL")
    try:
        public = is_public(url.host)
    except ValueError:
        return  # a host name
    if not public:
        raise BlockedAddressError(f"{url.host} is not a public address")


@asynccontextmanager
async def _public_session(hass: HomeAssistant) -> AsyncIterator[ClientSession]:
    """Return a session that connects to public addresses only (for one download)."""
    connector = TCPConnector(resolver=_PublicResolver(), ssl=client_context())
    async with ClientSession(connector=connector) as session:
        yield session


async def _async_fetch_public(hass: HomeAssistant, url: str, *, cache: bool) -> Announcement:
    """Download audio from a public address.

    Home Assistant would otherwise fetch addresses of the local network on
    behalf of whoever may play a URL, and switch devices that act on a plain
    GET request. Every redirect is followed by hand and checked the same way.
    """
    async with _public_session(hass) as session:
        try:
            return await _async_fetch(session, url, cache=cache, check=_check_url)
        except BlockedAddressError as err:
            _LOGGER.debug("Refused %s: %s", _without_query(url), err)
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="url_not_public"
            ) from err


async def _async_fetch(
    session: ClientSession,
    url: str,
    *,
    cache: bool,
    check: Callable[[yarl.URL], None] | None = None,
) -> Announcement:
    """Download audio; anything that is not audio is refused.

    With a check, redirects are followed by hand and every target is checked
    before it is requested. Error messages carry no details of the answer
    (status, host): they would tell what is reachable at an address.

    A downloaded clip is cached by its content, not by its address: a file
    that changes behind the same URL is uploaded anew. The audio is
    downloaded for every announcement anyway; the cache saves the upload.
    """
    target = yarl.URL(url)
    try:
        for _ in range(MAX_REDIRECTS + 1):
            if check is not None:
                check(target)
            async with session.get(
                target, timeout=FETCH_TIMEOUT, allow_redirects=check is None
            ) as resp:
                if resp.status in REDIRECT_STATUSES and hdrs.LOCATION in resp.headers:
                    target = target.join(yarl.URL(resp.headers[hdrs.LOCATION]))
                    continue
                mime_type, body = await _async_read_audio(resp)
                break
        else:
            raise BlockedAddressError("too many redirects")
    except BlockedAddressError:
        raise
    except (ClientError, TimeoutError, ValueError) as err:
        _LOGGER.debug("Download of %s failed: %s", _without_query(url), _describe_fetch_error(err))
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="fetch_failed") from err
    data = bytes(body)
    source = f"content:{hashlib.sha256(data).hexdigest()}" if cache else None
    return Announcement(source=source, extension=_extension(url, mime_type), data=data)


async def _async_read_audio(resp: ClientResponse) -> tuple[str, bytearray]:
    """Read an audio answer up to MAX_CLIP_BYTES; return its MIME type and body."""
    resp.raise_for_status()
    mime_type = resp.content_type or ""
    if not mime_type.startswith("audio/"):
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="not_audio")
    if _content_length(resp.headers.get(hdrs.CONTENT_LENGTH)) > MAX_CLIP_BYTES:
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="clip_too_large")
    # read(n) returns only what is buffered so far, not n bytes:
    # collect the chunks until the end or the limit.
    body = bytearray()
    async for chunk in resp.content.iter_chunked(FETCH_CHUNK_BYTES):
        body += chunk
        if len(body) > MAX_CLIP_BYTES:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="clip_too_large")
    return mime_type, body


def _content_length(value: str | None) -> int:
    """Return the announced size; a missing or broken header counts as unknown (0).

    The download stops past MAX_CLIP_BYTES anyway.
    """
    try:
        return int(value or 0)
    except ValueError:
        return 0


def _extension(url: str, mime_type: str) -> str:
    """Pick a file extension from the MIME type or the URL."""
    guessed = mimetypes.guess_extension(mime_type.split(";")[0].strip()) if mime_type else None
    if guessed:
        return guessed.lstrip(".").replace("mpga", "mp3")
    return yarl.URL(url).suffix.lstrip(".") or "mp3"


def _without_query(url: str) -> str:
    """Drop user, password, query and fragment: they may carry credentials or tokens."""
    try:
        return str(yarl.URL(url).with_user(None).with_query(None).with_fragment(None))
    except TypeError, ValueError:
        return "<invalid URL>"


def _describe(err: Exception) -> str:
    """Return the error text; a timeout has none of its own."""
    return str(err) or type(err).__name__


def _describe_fetch_error(err: Exception) -> str:
    """Describe a download error for the log; the URL's query (tokens) stays out."""
    if isinstance(err, ClientResponseError):
        return f"HTTP {err.status} {err.message}".strip()
    return _describe(err)
