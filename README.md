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
4. The clip stays on the display, within a size limit (500 MB by default). The same announcement later starts at once, without creating and uploading it again. A clip is deleted when it was not played for 90 days (configurable). If a clip is gone from the display for whatever reason, it is uploaded again.

Announcements play one after another; a second one waits for the first to finish.

## Supported devices

| Device | Status |
|---|---|
| Wall Display XL (SAWD-3A1XE10EU2), firmware 2.8.2 | Tested |
| Other wall displays with a speaker and firmware 2.8 or newer | Expected to work, not tested |
| Wall displays without a speaker | Can be added, but there is nothing to hear |

Requirements: Home Assistant 2026.10 or newer, and a text-to-speech engine for announcements (for example Piper).

## Installation

1. In HACS: **⋮ → Custom repositories**, add `https://github.com/n-schilling/ha-wall-display-plus` as type **Integration**.
2. Install **Wall Display Plus** and restart Home Assistant.
3. Displays on the network are discovered and shown under **Settings → Devices & services**. Or add one by hand: **Add integration → Wall Display Plus**.

### Installation parameters

| Parameter | Description |
|---|---|
| Host | IP address or host name of the display. Discovered displays fill it in themselves. A new address is picked up automatically only for displays without a password, and only if the device at the new address reports the same MAC; with a password, use **Reconfigure**. |
| Password | Only if a password is set on the display (user `admin`, digest authentication). Leave empty otherwise. |

The address and the password can be changed later with **Reconfigure**. If the display rejects the password, Home Assistant asks for the new one.

### Configuration parameters

**Settings → Devices & services → Wall Display Plus → Configure**:

| Option | Default | Description |
|---|---|---|
| Keep clips for | 90 days | A cached clip is deleted from the display when it was not played for this many days (1–365). |
| Maximum cache size | 500 MB | The cached clips may take at most this much storage on the display (10–10000 MB). When a new clip does not fit, the clips played longest ago are deleted first. A single clip larger than the limit is played once and not kept. |

## Entities and functions

Since Home Assistant 2026.10 a device belongs to a single integration, so the entities get their own device next to the one of the official integration. Both carry the display's MAC address.

| Entity | What it does |
|---|---|
| Speaker (`media_player`) | Plays text to speech, media sources and audio URLs as announcements. Supports `tts.speak`, `media_player.play_media` (with `announce`), stop and media browsing (audio only). |
| Motion wakes screen (`switch`, configuration) | Whether motion in front of the display wakes the screen. Touch always does. |
| Clear speaker cache (`button`, configuration) | Deletes every clip this integration put on the display. |

`media_player.play_media` accepts `extra: {cache: false}` to play a clip without keeping it.

## Data updates

The integration polls the display every 30 seconds for the motion setting and the media player status. Playing, the switch and the button act immediately. Old clips are cleaned up after each clip and once a day.

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

No motion wake-up at night:

```yaml
triggers:
  - trigger: time
    at: "22:00:00"
actions:
  - action: switch.turn_off
    target:
      entity_id: switch.living_room_wall_display_motion_wakes_screen
```

## Use cases

- Household announcements: washing machine, dishwasher, dryer done.
- Reminders and warnings where people are: window open while it rains, door left open, high CO₂.
- Doorbell or alarm sounds on the display in the living room.
- Voice assistant answers on the display.
- A dark display in the evening: motion does not wake it, touch still does.

## Known limitations

- **Volume** is the display's current media volume. Set it with the official integration's media player.
- **The TTS voice is not part of the cache.** After changing the voice, press **Clear speaker cache**; cached announcements keep the old voice until they expire otherwise.
- **What can be played:** text to speech, media sources and absolute `http(s)` URLs that return audio, up to 20 MB. Paths of your Home Assistant instance such as `/api/...` are refused: they would be fetched with a signature, so anyone allowed to call `play_media` could copy protected data into the display's media library, which can be read without a password.
- **The motion switch writes to the display only when the value changes.** Every write makes the official integration reload, and its entities are unavailable for a moment. Do not change the switch from an automation that triggers on those entities becoming available again: that is a loop.
- **Storage:** before every upload the integration checks the display's free storage. At least 100 MB must stay free; if not, it first deletes its own clips (never photos, videos, music or ringtones). If that is not enough, the clip is not uploaded and a repair issue "storage almost full" appears.
- **Undocumented interface:** the media upload uses the calls of the display's own web interface, which are not in the published API documentation. A firmware update can change them; the integration then shows a repair issue.
- Discovery works over IPv4 only. It never moves a display with a password to a new address: anyone on the network can announce a display's name, and the login would go to that device. Use **Reconfigure** after an address change.

## Privacy

- **Clips stay on the display** until they were not played for the configured time (90 days by default) or the cache needs room. They are audio files in the display's media library.
- **Set a password on the display.** Without one, any device on your network can list the media library. The file names do not reveal the texts (they are keyed with a random secret per display that only Home Assistant knows), but the files are there.
- **For sensitive texts use `extra: {cache: false}`:** the clip is deleted right after it was played.
- **The connection to the display is plain HTTP** (the display offers nothing else). Announcements travel unencrypted through your local network.
- **Diagnostics hold nothing personal:** no password, address or MAC, no cache keys, file names or play times, nothing about what the display played. Error messages leave out the query part of URLs, which may carry tokens.
- **Clear speaker cache** also creates a new secret.
- The integration sends no telemetry and contacts no third party; it only downloads URLs you ask it to play.

## Troubleshooting

| Problem | What to do |
|---|---|
| Nothing to hear | Check the volume of the display (official integration's media player) and that a Bluetooth speaker is not connected instead. |
| Repair issue "the firmware does not support the speaker" | The firmware changed the interface. [Open an issue](https://github.com/n-schilling/ha-wall-display-plus/issues/new/choose) with model and firmware. The issue disappears once a clip plays again. |
| Entities unavailable | The display is off or has a new address. Without a password, discovery picks up the new address; with a password, use **Reconfigure**. |
| Repair issue "storage almost full" | Delete photos, videos or music in the display's web interface. The issue disappears once a clip plays again. |
| "Unsupported media" or "not audio" | Only text to speech, media sources and http(s) URLs with audio can be played. |
| Old voice after changing it | Press **Clear speaker cache**. |
| Something else | Download the diagnostics from the integration's page and attach them to an issue. They hold no password, address or announcement texts. |

## Removal

1. **Settings → Devices & services → Wall Display Plus → ⋮ → Delete**. The integration deletes its clips from the display on the way.
2. Remove the integration in HACS and restart Home Assistant.

If the display was not reachable during the removal, its clips stay there. Add the integration again, press **Clear speaker cache** and remove it, or delete the `wdp_` files in the display's web interface.

The display's motion setting stays as it was last set.

## Development

```bash
python3.14 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/pytest --cov=custom_components.wall_display_plus
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy custom_components
```

The tests run against a display simulated in memory (`tests/conftest.py`) and, for the RPC client, against a local HTTP twin with digest authentication (`tests/test_api.py`). The rules of the Home Assistant integration quality scale are tracked in [`quality_scale.yaml`](custom_components/wall_display_plus/quality_scale.yaml).

## Support

Questions, bugs and ideas: [open an issue](https://github.com/n-schilling/ha-wall-display-plus/issues/new/choose). Security problems: see [SECURITY.md](SECURITY.md).

## License

[Apache License 2.0](LICENSE)
