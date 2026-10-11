"""Turn what was asked to play into audio, once for all displays that play it."""

from __future__ import annotations

from dataclasses import dataclass, replace
import io
import json
import logging
import mimetypes
from typing import Any

from aiohttp import ClientError, ClientResponseError, ClientTimeout
import mutagen
import yarl

from homeassistant.components import media_source, tts
from homeassistant.components.media_player.browse_media import async_process_play_media_url
from homeassistant.components.media_source.const import URI_SCHEME
from homeassistant.components.media_source.error import Unresolvable
from homeassistant.components.tts.const import DOMAIN as TTS_DOMAIN
from homeassistant.components.tts.media_source import parse_media_source_id
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

MAX_CLIP_BYTES = 20 * 1024 * 1024
FETCH_TIMEOUT = ClientTimeout(total=30)

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
    except mutagen.MutagenError as err:
        _LOGGER.debug("Audio length unreadable: %s", err)
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
                return await _async_resolve_and_fetch(hass, media_id, target_entity_id, None)
        return await _async_resolve_and_fetch(hass, media_id, target_entity_id, media_id)
    # Anything else must be a plain http(s) URL. It is fetched as given and
    # never signed: signing would let any user who may call play_media
    # read authenticated Home Assistant paths through the display.
    parsed = yarl.URL(media_id)
    if not parsed.is_absolute() or parsed.scheme not in ("http", "https"):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unsupported_media",
            translation_placeholders={"media_id": _without_query(media_id)},
        )
    return await _async_fetch(hass, media_id, media_id)


async def _async_resolve_and_fetch(
    hass: HomeAssistant, media_id: str, target_entity_id: str | None, source: str | None
) -> Announcement:
    """Resolve a media source item and download it."""
    item = await media_source.async_resolve_media(hass, media_id, target_entity_id)
    if not (item.mime_type or "").startswith("audio/"):
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_audio")
    # Media sources resolve to paths of this instance that may need a
    # signature; only these are signed.
    url = async_process_play_media_url(hass, item.url)
    return await _async_fetch(hass, url, source)


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


async def _async_fetch(hass: HomeAssistant, url: str, source: str | None) -> Announcement:
    """Download audio; anything that is not audio is refused."""
    session = async_get_clientsession(hass)
    try:
        async with session.get(url, timeout=FETCH_TIMEOUT) as resp:
            resp.raise_for_status()
            mime_type = resp.content_type or ""
            if not mime_type.startswith("audio/"):
                raise HomeAssistantError(translation_domain=DOMAIN, translation_key="not_audio")
            if _content_length(resp.headers.get("Content-Length")) > MAX_CLIP_BYTES:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="clip_too_large"
                )
            data = await resp.content.read(MAX_CLIP_BYTES + 1)
    except (ClientError, TimeoutError) as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="fetch_failed",
            translation_placeholders={"error": _describe_fetch_error(err, url)},
        ) from err
    if len(data) > MAX_CLIP_BYTES:
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="clip_too_large")
    return Announcement(source=source, extension=_extension(url, mime_type), data=data)


def _content_length(value: str | None) -> int:
    """Return the announced size; a missing or broken header counts as unknown (0).

    The download is limited to MAX_CLIP_BYTES + 1 anyway.
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
    """Drop query and fragment: they may carry tokens or signatures."""
    try:
        return str(yarl.URL(url).with_query(None).with_fragment(None))
    except TypeError, ValueError:
        return "<invalid URL>"


def _describe_fetch_error(err: Exception, url: str) -> str:
    """Describe a download error without the URL's query (tokens, keys)."""
    if isinstance(err, ClientResponseError):
        return f"HTTP {err.status} {err.message}".strip()
    return f"{type(err).__name__} for {_without_query(url)}"
