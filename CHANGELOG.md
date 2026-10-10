# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versions follow [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-10

### Added

- Speaker media player: plays text to speech, media sources and audio URLs on the display's built-in speaker, as announcements. Audio is uploaded as an alert clip and played once.
- Clip cache on the display: an announcement played before starts without a new upload. Clips not played for 90 days (configurable) are deleted; clips missing on the display are uploaded again. `extra: {cache: false}` plays a clip without keeping it.
- Cache size limit, 500 MB by default (configurable): the clips played longest ago make room for new ones; a clip larger than the limit is played once and not kept.
- Free storage check before every upload: at least 100 MB must stay free. Own clips are deleted first; if that is not enough, nothing is uploaded and a repair issue appears. Other files on the display are never touched.
- Switch "Motion wakes screen", which writes to the display only when the value changes.
- Button "Clear speaker cache".
- Only audio is played: media sources and absolute http(s) URLs whose answer is audio. Paths of the Home Assistant instance are refused and never fetched with a signature.
- Privacy: clip file names are keyed with a random secret per display (HMAC), so they do not reveal the texts; clearing the cache creates a new secret. Diagnostics hold no address, MAC, cache keys, file names, play times or played titles. Error messages leave out URL queries and TTS engine messages. Removing the integration deletes its clips from the display.
- Discovery of displays on the network (zeroconf); a known display with a new address is updated only without a password and only if the device at the new address reports the same MAC.
- Setup by IP address with optional password (digest authentication), reconfiguration, re-authentication, diagnostics, a repair issue when the firmware no longer supports the speaker. English and German.
- Checked against the Home Assistant integration quality scale up to platinum (`quality_scale.yaml`): strict typing, 99 % test coverage, translated errors, brand images.

[0.1.0]: https://github.com/n-schilling/ha-wall-display-plus/releases/tag/v0.1.0
