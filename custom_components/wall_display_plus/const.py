"""Constants for Wall Display Plus."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "wall_display_plus"

# A config entry is either a display or a speaker group (experimental).
CONF_TYPE: Final = "type"
TYPE_GROUP: Final = "group"
CONF_MEMBERS: Final = "members"

CONF_CACHE_DAYS: Final = "cache_days"
CONF_CACHE_MAX_MB: Final = "cache_max_mb"
CONF_MAX_TEXT_LENGTH: Final = "max_text_length"
DEFAULT_CACHE_DAYS: Final = 90
MIN_CACHE_DAYS: Final = 1
MAX_CACHE_DAYS: Final = 365
DEFAULT_CACHE_MAX_MB: Final = 500
MIN_CACHE_MAX_MB: Final = 10
MAX_CACHE_MAX_MB: Final = 10000
# Longest text sent to the text-to-speech engine, in characters. It is checked
# before the engine runs, so a text pasted by mistake costs no computing time.
# 500 characters are about 35 s of speech.
DEFAULT_MAX_TEXT_LENGTH: Final = 500
MIN_MAX_TEXT_LENGTH: Final = 50
MAX_MAX_TEXT_LENGTH: Final = 5000

# An upload must leave at least this much storage free on the display.
FREE_SPACE_RESERVE: Final = 100 * 1024 * 1024

# The display only accepts the user name "admin" for digest authentication.
AUTH_USER: Final = "admin"

# Polling is rare on purpose: the display is weak, and everything the
# integration does itself updates at once (writes set the new state, changes to
# the media library and failed announcements trigger an immediate refresh).
# Users who need fresher values call homeassistant.update_entity.
UPDATE_INTERVAL: Final = timedelta(minutes=10)
CLEANUP_INTERVAL: Final = timedelta(hours=24)

# Every clip this integration uploads is named
# "homeassistant_<tag>_<key>.<ext>". The tag is random per config entry, so two
# Home Assistant instances on one display never touch each other's clips.
# Files without this integration's prefix and tag are never touched.
CLIP_PREFIX: Final = "homeassistant_"
TAG_LENGTH: Final = 6
KEY_LENGTH: Final = 24

# Firmware 2.8.2 stores file names up to 250 bytes (UTF-8). A longer name is
# accepted without error but the file never appears in the library.
MAX_FILENAME_BYTES: Final = 250

# The display has a separate media type for notification sounds. Clips of this
# type stay out of the music library and its playlist.
MEDIA_TYPE_ALERT: Final = "ALERT"

# The display's own web interface uploads base64 in chunks of this size.
UPLOAD_CHUNK_SIZE: Final = 1024 * 1024

# Extra time after a clip before the next one starts or a clip is deleted.
PLAYBACK_MARGIN: Final = 0.5

STORAGE_VERSION: Final = 1
