# Changelog

All notable changes to this project are documented here.

Format inspired by [Keep a Changelog](https://keepachangelog.com/).  
This project follows a practical semver for open-source releases.

## [1.0.0] — 2026-07-13

### Added

- Guided download wizard (mode → quality / audio / image → confirm)  
- Multi-platform support via yt-dlp (1000+ extractors)  
- Video qualities: 480p, 720p, 1080p, Max  
- Audio extraction: MP3, M4A, Opus  
- Subtitle language selection + embed  
- Image resolution options when available  
- Live progress updates (bar, speed, ETA)  
- Per-user history and global stats (local JSON)  
- Rate limiting and admin bypass  
- FFmpeg auto-discovery (including Windows WinGet layouts)  
- Single-instance process lock  
- Resilient Telegram uploads (long timeouts, retries, document fallback)  
- Docker + docker-compose  
- Profile policy: does not overwrite BotFather branding unless `.env` overrides are set  
- Open-source docs: architecture, deployment, security, contributing  

### Security

- `.env`, cookies, and runtime data excluded from version control  

[1.0.0]: https://github.com/gazzy-source/all-media-downloader/releases/tag/v1.0.0
