# Wall Display Plus for Home Assistant

[![Release](https://img.shields.io/github/v/release/n-schilling/ha-wall-display-plus)](https://github.com/n-schilling/ha-wall-display-plus/releases)
[![CI](https://github.com/n-schilling/ha-wall-display-plus/actions/workflows/ci.yaml/badge.svg?branch=main)](https://github.com/n-schilling/ha-wall-display-plus/actions/workflows/ci.yaml)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![HACS: custom](https://img.shields.io/badge/HACS-custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)

A custom integration that turns the built-in speaker of a Shelly Wall Display into a Home Assistant media player: `tts.speak`, announcements and audio files play on the display. It also adds a switch for whether motion wakes the screen.

It complements the official integration for the display, it does not replace it. This project is not affiliated with or endorsed by Shelly.

## Why

The official integration's media player only plays files that are already in the display's media library. `tts.speak` and audio URLs fail with "Unsupported media ID". This integration uploads the audio to the display first, plays it once and keeps it for the next time.

## How it works

1. Home Assistant creates the audio: text to speech straight from the TTS engine, other media sources and URLs by download.
2. The audio goes to the display's media library as an **alert** clip. Alert clips are separate from music: they do not show up in the music playlist or in the media browser of the official integration.
3. The display plays the clip once.
4. The clip stays on the display. The same announcement later starts at once, without creating and uploading it again. A clip is deleted when it was not played for 90 days (configurable). If a clip is gone from the display for whatever reason, it is uploaded again.

Announcements play one after another; a second one waits for the first to finish.

## Requirements

- Home Assistant 2026.10 or newer
- A wall display with a speaker, firmware 2.8 or newer. Tested with the Wall Display XL (SAWD-3A1XE10EU2) on firmware 2.8.2.
- A text-to-speech engine for announcements, for example Piper.

## Installation

1. In HACS: **⋮ → Custom repositories**, add `https://github.com/n-schilling/ha-wall-display-plus` as type **Integration**.
2. Install **Wall Display Plus** and restart Home Assistant.
3. **Settings → Devices & services → Add integration → Wall Display Plus**, enter the display's IP address. A password is only needed if one is set on the display.

## Entities

Since Home Assistant 2026.10 a device belongs to a single integration, so the entities get their own device next to the one of the official integration. Both carry the display's MAC address.

| Entity | What it does |
|---|---|
| Speaker (`media_player`) | Plays text to speech, media sources and audio URLs as announcements. |
| Motion wakes screen (`switch`) | Whether motion in front of the display wakes the screen. Touch always does. |
| Clear speaker cache (`button`) | Deletes every clip this integration put on the display. |

## Examples

Text to speech:

```yaml
action: tts.speak
target:
  entity_id: tts.piper
data:
  media_player_entity_id: media_player.living_room_wall_display_speaker
  message: The washing machine is done.
```

A text that changes every time (times, counts) should not be cached:

```yaml
action: media_player.play_media
target:
  entity_id: media_player.living_room_wall_display_speaker
data:
  media_content_type: music
  media_content_id: media-source://tts/tts.piper?message=Dinner+in+15+minutes
  extra:
    cache: false
```

An audio file:

```yaml
action: media_player.play_media
target:
  entity_id: media_player.living_room_wall_display_speaker
data:
  media_content_type: music
  media_content_id: https://example.com/doorbell.mp3
```

## Good to know

- **What can be played:** text to speech, media sources and absolute `http(s)` URLs that return audio. Paths of your Home Assistant instance such as `/api/...` are refused: they would be fetched with a signature, so anyone allowed to call `play_media` could copy protected data into the display's media library, which can be read without a password.
- **Volume** is the display's current media volume. Set it with the official integration's media player.
- **After changing the TTS voice**, press **Clear speaker cache**. Cached announcements keep the old voice until they expire otherwise.
- **The motion switch writes to the display only when the value changes.** Every write makes the official integration reload, and its entities are unavailable for a moment. Do not change the switch from an automation that triggers on those entities becoming available again: that is a loop.
- **Removing the integration** leaves the cached clips on the display. Press **Clear speaker cache** first.
- **Undocumented interface:** the media upload uses the calls of the display's own web interface, which are not in the published API documentation. A firmware update can change them. The speaker then fails with a clear error; please [open an issue](https://github.com/n-schilling/ha-wall-display-plus/issues/new/choose) with the firmware version.

## Development

```bash
python3.14 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

The tests run against a display simulated in memory (`tests/conftest.py`).

## Support

Questions, bugs and ideas: [open an issue](https://github.com/n-schilling/ha-wall-display-plus/issues/new/choose). Security problems: see [SECURITY.md](SECURITY.md).

## License

[Apache License 2.0](LICENSE)
