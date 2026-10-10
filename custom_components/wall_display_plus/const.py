"""Constants for Wall Display Plus."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "wall_display_plus"

CONF_CACHE_DAYS: Final = "cache_days"
CONF_CACHE_MAX_MB: Final = "cache_max_mb"
DEFAULT_CACHE_DAYS: Final = 90
MIN_CACHE_DAYS: Final = 1
MAX_CACHE_DAYS: Final = 365
DEFAULT_CACHE_MAX_MB: Final = 500
MIN_CACHE_MAX_MB: Final = 10
MAX_CACHE_MAX_MB: Final = 10000

# An upload must leave at least this much storage free on the display.
FREE_SPACE_RESERVE: Final = 100 * 1024 * 1024

# The display only accepts the user name "admin" for digest authentication.
AUTH_USER: Final = "admin"

UPDATE_INTERVAL: Final = timedelta(seconds=30)
CLEANUP_INTERVAL: Final = timedelta(hours=24)

# Every clip this integration uploads starts with this prefix. Clips without
# it belong to someone else and are never touched.
CLIP_PREFIX: Final = "wdp_"

# The display has a separate media type for notification sounds. Clips of this
# type stay out of the music library and its playlist.
MEDIA_TYPE_ALERT: Final = "ALERT"

# The display's own web interface uploads base64 in chunks of this size.
UPLOAD_CHUNK_SIZE: Final = 1024 * 1024

# Extra time after a clip before the next one starts or a clip is deleted.
PLAYBACK_MARGIN: Final = 0.5

STORAGE_VERSION: Final = 1
