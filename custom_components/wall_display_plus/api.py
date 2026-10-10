"""Local RPC client for the wall display.

The media methods used here are not part of the published API documentation.
They are the calls the display's own web interface makes, read from firmware
2.8.2. Every call goes through ``rpc`` so a changed firmware fails in one place
with a clear error.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout, DigestAuthMiddleware

from .const import AUTH_USER, UPLOAD_CHUNK_SIZE

TIMEOUT = ClientTimeout(total=20)
UPLOAD_TIMEOUT = ClientTimeout(total=60)

# Firmware 2.8.2 answers an unknown method with -114 "Method not found!";
# 404 is the code of the general Gen2 RPC protocol.
METHOD_NOT_FOUND = (-114, 404)


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

    @property
    def is_wall_display(self) -> bool:
        """Return whether this is a wall display."""
        return self.app.startswith("WallDisplay") or self.model.startswith("SAWD")


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
        self._middlewares = (DigestAuthMiddleware(AUTH_USER, password),) if password else ()
        self._id = 0

    async def get_device_info(self) -> DeviceInfo:
        """Read the unauthenticated device information."""
        try:
            async with self._session.get(f"{self._base}/shelly", timeout=TIMEOUT) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except (ClientError, TimeoutError, ValueError) as err:
            raise WallDisplayError(f"Cannot read device information: {err}") from err
        return DeviceInfo(
            mac=str(data.get("mac", "")).lower(),
            model=str(data.get("model", "")),
            firmware=str(data.get("ver", "")),
            app=str(data.get("app", "")),
            auth_enabled=bool(data.get("auth_en", False)),
            fs_free=int(data["fs_free"]) if isinstance(data.get("fs_free"), int) else None,
        )

    async def rpc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        client_timeout: ClientTimeout = TIMEOUT,
    ) -> dict[str, Any]:
        """Call one RPC method and return its result."""
        self._id += 1
        frame = {
            "id": self._id,
            "src": "wall_display_plus",
            "method": method,
            "params": params or {},
        }
        try:
            async with self._session.post(
                f"{self._base}/rpc",
                json=frame,
                timeout=client_timeout,
                middlewares=self._middlewares,
            ) as resp:
                if resp.status == 401:
                    raise WallDisplayAuthError("The display rejected the password")
                resp.raise_for_status()
                answer = await resp.json(content_type=None)
        except WallDisplayError:
            raise
        except (ClientError, TimeoutError, ValueError) as err:
            raise WallDisplayError(f"{method} failed: {err}") from err
        if not isinstance(answer, dict):
            raise WallDisplayError(f"{method}: unexpected answer {answer!r}")
        if "error" in answer:
            error = answer["error"]
            code = error.get("code") if isinstance(error, dict) else None
            if code == 401:
                raise WallDisplayAuthError("The display rejected the password")
            if code in METHOD_NOT_FOUND:
                raise WallDisplayUnsupportedError(f"{method}: {error}")
            raise WallDisplayError(f"{method}: {error}")
        result = answer.get("result", {})
        return result if isinstance(result, dict) else {}

    async def list_media(self) -> list[MediaItem]:
        """Return all files in the media library."""
        result = await self.rpc("Media.List")
        return [
            MediaItem(
                id=int(item["id"]),
                filename=str(item["filename"]),
                type=str(item.get("type", "")),
                size=int(item.get("size") or 0),
            )
            for item in result.get("list", [])
            if "id" in item and "filename" in item
        ]

    async def upload(self, filename: str, media_type: str, data: bytes) -> None:
        """Upload a file in chunks, the way the display's web interface does."""
        encoded = base64.b64encode(data).decode()
        chunks = [
            encoded[start : start + UPLOAD_CHUNK_SIZE]
            for start in range(0, len(encoded), UPLOAD_CHUNK_SIZE)
        ] or [""]
        for index, chunk in enumerate(chunks):
            await self.rpc(
                "Media.PutMedia",
                {
                    "filename": filename,
                    "type": media_type,
                    "data": chunk,
                    "append": index > 0,
                    "last": index == len(chunks) - 1,
                },
                client_timeout=UPLOAD_TIMEOUT,
            )
        await self.rpc("Media.Reload")

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
        clip = result.get("audio_clip") or result.get("playback") or {}
        meta = clip.get("media_meta") or {}
        duration_ms = meta.get("duration") or 0
        return PlaybackResult(duration=max(float(duration_ms) / 1000, 0.0))

    async def stop(self) -> None:
        """Stop what the media player is playing."""
        await self.rpc("Media.MediaPlayer.Stop")

    async def get_media_status(self) -> dict[str, Any]:
        """Return the media player status."""
        return await self.rpc("Media.GetStatus")

    async def get_motion_config(self) -> dict[str, Any]:
        """Return the motion sensor configuration."""
        return await self.rpc("Motion.GetConfig", {"id": 0})

    async def set_wake_screen(self, enabled: bool) -> None:
        """Set whether motion wakes the screen.

        Every SetConfig raises the device's configuration revision, which makes
        the official integration for this device reload its entry. Callers only
        call this when the value really changes.
        """
        await self.rpc("Motion.SetConfig", {"id": 0, "config": {"wake_screen": enabled}})
