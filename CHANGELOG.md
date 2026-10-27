# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- A display that briefly does not answer no longer becomes unavailable for 10 minutes. Firmware 2.8.4 sometimes leaves a single request unanswered for half a minute; every such hiccup made all entities unavailable until the next poll, and switching them in that time did nothing (for example *Motion wakes screen* from an automation).
  - A request that only reads is sent once more after a second. Writes are never repeated.
  - A failed poll keeps the last values and is repeated after a minute. The entities only become unavailable after three failed polls in a row, and are then polled every minute until the display answers.
  - The timeout per request is 15 instead of 20 seconds.
- An error message after a timeout names the timeout instead of ending empty ("Motion.GetConfig failed: TimeoutError").
- The Release workflow can no longer be started by hand from a branch other than `main`; a manual run from `dev` would have tagged and published untested code.

## [0.1.7] - 2026-10-12

### Changed

- At an announcement volume of 0 % (or `extra: {volume: 0}`) an announcement is skipped: no text to speech, no upload, and the next announcement no longer waits for a silent clip. With several displays, a display at 0 % is left out and the others play without waiting for it.

### Added

- README: night mode example (quieter announcements and no motion wake-up at night, alarms still loud).

## [0.1.6] - 2026-10-11

### Fixed

- The speaker no longer shows *playing* after an announcement once the display was polled: firmware 2.8.2 keeps reporting an ended alert clip as playing, so the state now comes from the speaker's own timing only.

## [0.1.5] - 2026-10-11

### Fixed

- A file that changes behind the same URL or media source is played in its new version: downloaded clips are cached by their content instead of their address. Before, the old audio played for up to 90 days.
- Stopping a clip frees the display at once: the next announcement no longer waits for the rest of the stopped clip, and the volume goes back right away.
- The cleanup never deletes an uncached clip (`cache: false`, voice assistant streams) that is still to be played.
- The speaker no longer stays *playing* for up to 10 minutes when a poll happened to read the status while its own clip played.
- A firmware without the CPU temperature, or a failure reading it, no longer makes the display unavailable; the temperature is only read while its sensor is enabled.
- No full cleanup after every clip any more (one request less for the display per announcement); expired and orphaned clips go in the daily cleanup.
- Clips keep their real file type (for example `.opus`) instead of being renamed to `.mp3`; only unusable extensions become `.mp3`.
- A broken `Content-Length` header no longer breaks a download.
- The test button falls back to Home Assistant's language when the engine has no fitting and no default language.
- An upload keeps the encoded audio in memory only once.
- "Reconfigure" on a speaker group failed with an error; it now points to **Configure**, where the group's speakers are changed.
- README: troubleshooting for the announcement volume, the model check and groups; removal of speaker groups.

## [0.1.4] - 2026-10-11

### Added

- Attribute "File prefix" on the sensor "Cached clips": the `homeassistant_<tag>_` prefix of this entry's files on the display. The texts of the clips are still not shown anywhere.

## [0.1.3] - 2026-10-11

### Fixed

- "Test announcement volume" asks the text-to-speech engine in a language it knows: Home Assistant's "de" becomes the engine's "de_DE" (Piper refused "de"); without a fitting language the engine's default language is used.

## [0.1.2] - 2026-10-11

### Added

- Button "Test announcement volume": speaks a short test sentence at the announcement volume, in Home Assistant's language (German or English), with the voice of the preferred voice assistant or the default text-to-speech engine.

## [0.1.1] - 2026-10-11

### Changed

- Only the Wall Display XL (SAWD-3A1XE10EU2) is offered and set up. Other wall displays have the same media interface but may have no speaker or motion sensor, which the firmware does not report; they are no longer offered in discovery and are refused when added by hand.

### Fixed

- Adding a display by hand no longer aborts with "already in progress" while Home Assistant shows the same display as discovered; the manual setup takes over.

## [0.1.0] - 2026-10-11

### Added

- Speaker media player: plays text to speech, media sources and audio URLs on the display's built-in speaker, as announcements. Audio is uploaded as an alert clip and played once.
- Clip cache on the display: an announcement played before starts without a new upload. Clips not played for 90 days (configurable) are deleted; clips missing on the display are uploaded again. `extra: {cache: false}` plays a clip without keeping it.
- Cache size limit, 500 MB by default (configurable): the clips played longest ago make room for new ones; a clip larger than the limit is played once and not kept.
- Clip names `homeassistant_<tag>_<key>.<ext>` (at most 50 ASCII bytes; the firmware stores up to 250): the fixed prefix shows where a file comes from, the random tag per instance keeps several Home Assistant instances on one display apart. Clips of others are never touched.
- Free storage check before every upload: at least 100 MB must stay free. Own clips are deleted first; if that is not enough, nothing is uploaded and a repair issue appears. Other files on the display are never touched.
- Maximum text length for text to speech, 500 characters by default (configurable 50–5000): longer texts are refused before the engine runs, so a text pasted by mistake costs no computing time on the host.
- Switch "Motion wakes screen", which writes to the display only when the value changes.
- Screen settings: switches for dimming, turning off and the screen saver; numbers for minimum brightness and the three timeouts (10–3600 s). Written only on change, as whole sections like the display's web interface does. Manual and automatic brightness are left out on purpose.
- Number "Announcement volume" (70 % by default, kept across restarts): the volume of every announcement without its own, `tts.speak` included. The display goes back to its previous volume afterwards, so music keeps its own volume.
- Volume per announcement: `extra: {volume: 0-1}` instead of the announcement volume; the display goes back to its previous volume afterwards, at the latest before the next announcement.
- The length of every clip is read from the audio (firmware 2.8.2 does not report it for alert clips), so the volume goes back, the next announcement starts and an uncached clip is deleted only after the clip has played.
- Diagnostic sensors: free memory, free storage, cached clips, cache size, announcement delay, app started (shows app crashes), and – disabled by default – CPU temperature and cache hit rate.
- Button "Restart display app" (only the app, not the device).
- Button "Clear speaker cache".
- Only audio is played: media sources and absolute http(s) URLs whose answer is audio. Paths of the Home Assistant instance are refused and never fetched with a signature.
- Privacy: clip file names are keyed with a random secret per display (HMAC), so they do not reveal the texts; clearing the cache creates a new secret. Diagnostics hold no address, MAC, cache keys, file names, play times or played titles. Error messages leave out URL queries and TTS engine messages. Removing the integration deletes its clips from the display.
- Polls the display every 10 minutes only (the display is a small device). Changes the integration makes, uploads and deletions of clips and failed announcements refresh at once; `homeassistant.update_entity` refreshes on demand.
- Discovery of displays on the network (zeroconf); a known display with a new address is updated only without a password and only if the device at the new address reports the same MAC.
- Setup by IP address with optional password (digest authentication), reconfiguration, re-authentication, diagnostics, a repair issue when the firmware no longer supports the speaker. English and German.
- Several displays at the same time (**experimental**): the speakers of one action call (`tts.speak` with several speakers, `play_media` with several entities, a media player group helper) play one announcement together. The audio is created once; displays that have the clip wait for those that upload it, at most 10 seconds after the first is ready. A display not ready by then does not play and is reported as an error.
- Speaker groups (**experimental**): a fixed group of speakers as its own media player, added once at least two displays are set up, members changed in its options. It waits until all members have started and reports failed members. Members are stored by their registry ID, so they stay in the group when their entity ID changes.
- Checked against the Home Assistant integration quality scale up to platinum (`quality_scale.yaml`): strict typing, 99 % test coverage, translated errors, brand images.

[0.1.7]: https://github.com/n-schilling/ha-wall-display-plus/releases/tag/v0.1.7
[0.1.6]: https://github.com/n-schilling/ha-wall-display-plus/releases/tag/v0.1.6
[0.1.5]: https://github.com/n-schilling/ha-wall-display-plus/releases/tag/v0.1.5
[0.1.4]: https://github.com/n-schilling/ha-wall-display-plus/releases/tag/v0.1.4
[0.1.3]: https://github.com/n-schilling/ha-wall-display-plus/releases/tag/v0.1.3
[0.1.2]: https://github.com/n-schilling/ha-wall-display-plus/releases/tag/v0.1.2
[0.1.1]: https://github.com/n-schilling/ha-wall-display-plus/releases/tag/v0.1.1
[0.1.0]: https://github.com/n-schilling/ha-wall-display-plus/releases/tag/v0.1.0
