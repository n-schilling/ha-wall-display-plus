# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versions follow [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-10

### Added

- Speaker media player: plays text to speech, media sources and audio URLs on the display's built-in speaker, as announcements. Audio is uploaded as an alert clip and played once.
- Clip cache on the display: an announcement played before starts without a new upload. Clips not played for 90 days (configurable) are deleted; clips missing on the display are uploaded again. `extra: {cache: false}` plays a clip without keeping it.
- Switch "Motion wakes screen", which writes to the display only when the value changes.
- Button "Clear speaker cache".
- Setup by IP address with optional password, reconfiguration, re-authentication, diagnostics. English and German.

[0.1.0]: https://github.com/n-schilling/ha-wall-display-plus/releases/tag/v0.1.0
