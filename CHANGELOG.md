# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versions follow [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-10

### Added

- Speaker media player: plays text to speech, media sources and audio URLs on the display's built-in speaker, as announcements. Audio is uploaded as an alert clip and played once.
- Clip cache on the display: an announcement played before starts without a new upload. Clips not played for 90 days (configurable) are deleted; clips missing on the display are uploaded again. `extra: {cache: false}` plays a clip without keeping it.
- Cache size limit, 500 MB by default (configurable): the clips played longest ago make room for new ones; a clip larger than the limit is played once and not kept.
- Clip names `homeassistant_<tag>_<key>.<ext>` (at most 50 ASCII bytes; the firmware stores up to 250): the fixed prefix shows where a file comes from, the random tag per instance keeps several Home Assistant instances on one display apart. Clips of others are never touched.
- Free storage check before every upload: at least 100 MB must stay free. Own clips are deleted first; if that is not enough, nothing is uploaded and a repair issue appears. Other files on the display are never touched.
- Maximum text length for text to speech, 500 characters by default (configurable 50–5000): longer texts are refused before the engine runs, so a text pasted by mistake costs no computing time on the host.
- Switch "Motion wakes screen", which writes to the display only when the value changes.
- Screen settings: switches for dimming, turning off and the screen saver; numbers for minimum brightness and the three timeouts (10–3600 s). Written only on change, as whole sections like the display's web interface does. Manual and automatic brightness are left out on purpose.
- Volume per announcement: `extra: {volume: 0-1}`; the display goes back to its previous volume afterwards, at the latest before the next announcement.
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

[0.1.0]: https://github.com/n-schilling/ha-wall-display-plus/releases/tag/v0.1.0
