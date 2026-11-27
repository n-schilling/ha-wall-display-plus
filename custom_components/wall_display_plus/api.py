"""Local RPC client for the wall display.

The media methods used here are not part of the published API documentation.
They are the calls the display's own web interface makes, read from firmware
2.8.2. Every call goes through ``rpc`` so a changed firmware fails in one place
with a clear error.

A display with a password is logged in to the way its web interface does it:
with an ``auth`` object in the request itself. The display also announces HTTP
digest authentication in a header, but firmware 2.8.5 rejects it for the POST
requests used here whatever the password is.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import hashlib
import json
import logging
import secrets
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

from .const import AUTH_USER, SUPPORTED_MODELS, UPLOAD_CHUNK_SIZE

_LOGGER = logging.getLogger(__name__)

TIMEOUT = ClientTimeout(total=15)
UPLOAD_TIMEOUT = ClientTimeout(total=60)

# Firmware 2.8.4 sometimes leaves a single request unanswered for half a minute
# while the next one is answered at once. A request that only reads is sent once
# more after a short pause; a write is not, a repeated upload chunk or alert
# would do harm.
READ_ATTEMPTS = 2
RETRY_PAUSE = 1.0

# Firmware 2.8.2 answers an unknown method with -114 "Method not found!", and
# a method of a part the display does not have (the motion sensor on a Wall
# Display) with -114 "No such component"; 404 is the code of the general Gen2
# RPC protocol.
METHOD_NOT_FOUND = (-114, 404)

# Where Sys.GetInternalTemperatures reports the CPU: Wall Display XL, Wall Display
CPU_TEMPERATURE_KEYS = ("thermal-cpufreq-0", "cpu-sysrst")

UNAUTHORIZED = 401
# The display signs no method or address into the login: its web interface and
# the official integration use these fixed words in their place.
AUTH_HA2 = hashlib.sha256(b"dummy_method:dummy_uri").hexdigest()


class WallDisplayError(Exception):
    """The display could not be reached or answered with an error."""


class WallDisplayAuthError(WallDisplayError):
    """The display rejected the password."""


class WallDisplayUnsupportedError(WallDisplayError):
    """The firmware does not know a method this integration relies on."""


class WallDisplayStorageError(WallDisplayError):
    """Too little free storage on the display for a clip."""

    def __init__(self, free: int, needed: int) -> None:
        """Remember how much was free and how much was needed (bytes)."""
        super().__init__(f"{free} bytes free, {needed} bytes needed plus reserve")
        self.free = free
        self.needed = needed


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """What ``/shelly`` tells about the device."""

    mac: str
    model: str
    firmware: str
    app: str
    auth_enabled: bool
    fs_free: int | None = None  # bytes of free storage, None if not reported
    ram_free: int | None = None  # bytes of free memory
    uptime: int | None = None  # seconds since the device started
    app_uptime: int | None = None  # seconds since the display app started

    @property
    def is_supported(self) -> bool:
        """Return whether this is a model the integration supports."""
        return self.model in SUPPORTED_MODELS


@dataclass(frozen=True, slots=True)
class _Challenge:
    """What the display asks a login to be computed with."""

    realm: str
    nonce: int | str


@dataclass(frozen=True, slots=True)
class MediaItem:
    """One file in the display's media library."""

    id: int
    filename: str
    type: str
    size: int = 0  # bytes, 0 if not reported


@dataclass(frozen=True, slots=True)
class PlaybackResult:
    """What the display reports after starting a clip."""

    duration: float  # seconds, 0 when unknown


class WallDisplayClient:
    """Talk to one wall display over its local RPC interface."""

    def __init__(self, session: ClientSession, host: str, password: str | None) -> None:
        """Initialize the client."""
        self._session = session
        self._base = f"http://{host}"
        self._password = password or None
        self._challenge: _Challenge | None = None
        self._nc = 0
        self._id = 0

    async def get_device_info(self) -> DeviceInfo:
        """Read the unauthenticated device information."""
        try:
            data = await _retrying(self._get_shelly, READ_ATTEMPTS)
        except (ClientError, TimeoutError, ValueError) as err:
            raise WallDisplayError(f"Cannot read device information: {_describe(err)}") from err
        return DeviceInfo(
            mac=str(data.get("mac", "")).lower(),
            model=str(data.get("model", "")),
            firmware=str(data.get("ver", "")),
            app=str(data.get("app", "")),
            auth_enabled=bool(data.get("auth_en", False)),
            fs_free=_int_or_none(data.get("fs_free")),
            ram_free=_int_or_none(data.get("ram_free")),
            uptime=_int_or_none(data.get("uptime")),
            app_uptime=_int_or_none(data.get("app_uptime")),
        )

    async def rpc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        client_timeout: ClientTimeout = TIMEOUT,
    ) -> dict[str, Any]:
        """Call one RPC method and return its result.

        A method that only reads is sent once more when the display does not
        answer (see READ_ATTEMPTS).
        """
        self._id += 1
        frame = {
            "id": self._id,
            "src": "wall_display_plus",
            "method": method,
            "params": params or {},
        }
        attempts = READ_ATTEMPTS if _reads(method) else 1
        try:
            answer = await _retrying(lambda: self._post(frame, client_timeout), attempts)
        except (ClientError, TimeoutError, ValueError) as err:
            raise WallDisplayError(f"{method} failed: {_describe(err)}") from err
        if not isinstance(answer, dict):
            raise WallDisplayError(f"{method}: unexpected answer {answer!r}")
        if "error" in answer:
            error = answer["error"]
            code = error.get("code") if isinstance(error, dict) else None
            _LOGGER.debug("%s answered with error %s", method, code)
            if code == UNAUTHORIZED:
                raise WallDisplayAuthError("The display rejected the password")
            if code in METHOD_NOT_FOUND:
                raise WallDisplayUnsupportedError(f"{method}: {error}")
            raise WallDisplayError(f"{method}: {error}")
        result = answer.get("result", {})
        return result if isinstance(result, dict) else {}

    async def _get_shelly(self) -> Any:
        async with self._session.get(f"{self._base}/shelly", timeout=TIMEOUT) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def _post(self, frame: dict[str, Any], client_timeout: ClientTimeout) -> Any:
        """Send a request; log in and send it once more if the display asks for it.

        A rejected request was not carried out, so sending it again is safe
        for a write too. The display's answer to a request without a valid
        login holds what the next login is computed with; it is kept for the
        requests that follow.
        """
        answer = await self._post_once(self._signed(frame), client_timeout)
        if self._password is None or (challenge := _challenge(answer)) is None:
            return answer
        self._challenge = challenge
        self._nc = 0
        answer = await self._post_once(self._signed(frame), client_timeout)
        if _challenge(answer) is not None:
            self._challenge = None  # the password is wrong
        return answer

    async def _post_once(self, frame: dict[str, Any], client_timeout: ClientTimeout) -> Any:
        async with self._session.post(
            f"{self._base}/rpc", json=frame, timeout=client_timeout
        ) as resp:
            if resp.status == UNAUTHORIZED:
                # Over HTTP the error is the whole answer, not a part of it
                try:
                    error = await resp.json(content_type=None)
                except ValueError:
                    error = None
                details = error if isinstance(error, dict) else {}
                return {"error": {**details, "code": UNAUTHORIZED}}
            resp.raise_for_status()
            return await resp.json(content_type=None)

    def _signed(self, frame: dict[str, Any]) -> dict[str, Any]:
        """Return the request with the login, once the display asked for one."""
        if self._challenge is None:
            return frame
        self._nc += 1
        count = f"{self._nc:08x}"
        cnonce = secrets.randbits(48)
        realm, nonce = self._challenge.realm, self._challenge.nonce
        ha1 = _sha256(f"{AUTH_USER}:{realm}:{self._password}")
        return {
            **frame,
            "auth": {
                "username": AUTH_USER,
                "realm": realm,
                "nonce": nonce,
                "cnonce": cnonce,
                "nc": count,
                "algorithm": "SHA-256",
                "response": _sha256(f"{ha1}:{nonce}:{count}:{cnonce}:auth:{AUTH_HA2}"),
            },
        }

    async def list_media(self) -> list[MediaItem]:
        """Return all files in the media library; entries it cannot read are left out."""
        result = await self.rpc("Media.List")
        entries = result.get("list")
        return [
            item
            for entry in (entries if isinstance(entries, list) else [])
            if (item := _media_item(entry)) is not None
        ]

    async def upload(self, filename: str, media_type: str, data: bytes) -> None:
        """Upload a file in chunks, the way the display's web interface does."""
        encoded = base64.b64encode(data).decode()
        # Each chunk is cut when it is sent, so the encoded data exists once
        count = max(1, -(-len(encoded) // UPLOAD_CHUNK_SIZE))
        for index in range(count):
            await self.rpc(
                "Media.PutMedia",
                {
                    "filename": filename,
                    "type": media_type,
                    "data": encoded[index * UPLOAD_CHUNK_SIZE : (index + 1) * UPLOAD_CHUNK_SIZE],
                    "append": index > 0,
                    "last": index == count - 1,
                },
                client_timeout=UPLOAD_TIMEOUT,
            )
        await self.rpc("Media.Reload")
        _LOGGER.debug("Uploaded a clip as %s: %s bytes in %s chunks", media_type, len(data), count)

    async def find_media(self, filename: str, attempts: int = 8) -> MediaItem | None:
        """Find a file by name; right after an upload the list can lag a moment."""
        for attempt in range(attempts):
            for item in await self.list_media():
                if item.filename == filename:
                    return item
            if attempt < attempts - 1:
                await asyncio.sleep(0.25)
        return None

    async def delete_media(self, media_id: int) -> None:
        """Delete one file from the media library."""
        await self.rpc("Media.Delete", {"id": media_id})

    async def play_alert(self, media_id: int) -> PlaybackResult:
        """Play an alert clip once, without touching the music playlist."""
        result = await self.rpc("Media.MediaPlayer.PlayAlert", {"id": media_id})
        _LOGGER.debug("PlayAlert %s answered: %s", media_id, result)
        clip = result.get("audio_clip") or result.get("playback")
        meta = clip.get("media_meta") if isinstance(clip, dict) else None
        duration_ms = meta.get("duration") if isinstance(meta, dict) else None
        if not isinstance(duration_ms, int | float) or isinstance(duration_ms, bool):
            return PlaybackResult(duration=0.0)  # unknown
        return PlaybackResult(duration=max(float(duration_ms) / 1000, 0.0))

    async def stop(self) -> None:
        """Stop what the media player is playing."""
        await self.rpc("Media.MediaPlayer.Stop")

    async def get_media_status(self) -> dict[str, Any]:
        """Return the media player status."""
        return await self.rpc("Media.GetStatus")

    async def get_ui_config(self) -> dict[str, Any]:
        """Return the screen configuration (brightness, dimming, screen saver)."""
        return await self.rpc("Ui.GetConfig")

    async def set_ui_config(self, config: dict[str, Any]) -> None:
        """Change the screen configuration.

        Like the display's web interface, callers send whole sections (for
        example the complete "brightness" object). Every write makes the
        official integration for the display reload, so callers only write
        changed values.
        """
        await self.rpc("Ui.SetConfig", {"config": config})

    async def get_internal_temperatures(self) -> dict[str, Any]:
        """Return every temperature the display reports, by sensor name."""
        return await self.rpc("Sys.GetInternalTemperatures")

    async def get_cpu_temperature(self) -> float | None:
        """Return the CPU temperature in °C, None if not reported."""
        result = await self.get_internal_temperatures()
        key = cpu_temperature_key(result)
        return None if key is None else float(result[key])

    async def set_volume(self, level: int) -> None:
        """Set the media volume (0-10)."""
        await self.rpc("Media.SetVolume", {"volume": level})

    async def restart_app(self) -> None:
        """Restart the display app (not the whole device)."""
        result = await self.rpc("Sys.RestartApplication")
        _LOGGER.debug("RestartApplication answered: %s", result)

    async def get_motion_config(self) -> dict[str, Any]:
        """Return the motion sensor configuration.

        A display without a motion sensor raises WallDisplayUnsupportedError.
        """
        return await self.rpc("Motion.GetConfig", {"id": 0})

    async def set_wake_screen(self, enabled: bool) -> None:
        """Set whether motion wakes the screen.

        Every SetConfig raises the device's configuration revision, which makes
        the official integration for this device reload its entry. Callers only
        call this when the value really changes.
        """
        await self.rpc("Motion.SetConfig", {"id": 0, "config": {"wake_screen": enabled}})


async def _retrying[T](request: Callable[[], Awaitable[T]], attempts: int) -> T:
    """Run a request; if the display does not answer, again after a pause."""
    for _ in range(attempts - 1):
        try:
            return await request()
        except (ClientError, TimeoutError) as err:
            _LOGGER.debug("No answer from the display, asking once more: %s", _describe(err))
        await asyncio.sleep(RETRY_PAUSE)
    return await request()


def _challenge(answer: Any) -> _Challenge | None:
    """Read what a login is computed with from an answer that asks for one.

    The display puts it into the error message as JSON text. None if the
    answer is no such request or names a login this client does not know.
    """
    error = answer.get("error") if isinstance(answer, dict) else None
    if not isinstance(error, dict) or error.get("code") != UNAUTHORIZED:
        return None
    message = error.get("message")
    try:
        asked = json.loads(message) if isinstance(message, str) else None
    except ValueError:
        return None
    if (
        not isinstance(asked, dict)
        or asked.get("auth_type") != "digest"
        or asked.get("algorithm") != "SHA-256"
    ):
        return None
    realm, nonce = asked.get("realm"), asked.get("nonce")
    if not isinstance(realm, str) or not realm:
        return None
    if not isinstance(nonce, int | str) or isinstance(nonce, bool):
        return None
    return _Challenge(realm=realm, nonce=nonce)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _reads(method: str) -> bool:
    """Return whether an RPC method only reads (Media.List, Ui.GetConfig, ...)."""
    return method.rpartition(".")[2].startswith(("Get", "List"))


def _describe(err: Exception) -> str:
    """Return the error text; a timeout has none of its own."""
    return str(err) or type(err).__name__


def _media_item(entry: Any) -> MediaItem | None:
    """Read one entry of Media.List; None if it is not what firmware 2.8 sends."""
    if not isinstance(entry, dict):
        return None
    media_id, filename = _int_or_none(entry.get("id")), entry.get("filename")
    if media_id is None or not isinstance(filename, str):
        return None
    return MediaItem(
        id=media_id,
        filename=filename,
        type=str(entry.get("type", "")),
        size=_int_or_none(entry.get("size")) or 0,
    )


def cpu_temperature_key(temperatures: dict[str, Any]) -> str | None:
    """Return under which name Sys.GetInternalTemperatures reports the CPU, None if not."""
    return next(
        (
            key
            for key in CPU_TEMPERATURE_KEYS
            if isinstance(temperatures.get(key), int | float)
            and not isinstance(temperatures.get(key), bool)
        ),
        None,
    )


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
