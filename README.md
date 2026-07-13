<p align="center">
  <img src="docs/assets/banner.svg" alt="All-Media Downloader Bot" width="720" />
</p>

<h1 align="center">All-Media Downloader Bot</h1>

<p align="center">
  <strong>Guided multi-platform media downloader for Telegram — by Gazzy Labs</strong>
</p>

<p align="center">
  Download <b>video · audio · images · subtitles</b> from YouTube, Instagram, TikTok, X, Facebook, Pinterest, Reddit and <b>1000+ sites</b> — with a smart quality wizard, resilient uploads, and operator-friendly defaults.
</p>

<p align="center">
  <a href="#-why-this-isnt-just-another-ytdlp-wrapper"><img src="https://img.shields.io/badge/design-guided%20wizard-7C3AED?style=flat-square" alt="Guided wizard" /></a>
  <a href="#-features"><img src="https://img.shields.io/badge/platforms-1000%2B-0EA5E9?style=flat-square" alt="1000+ platforms" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-22C55E?style=flat-square" alt="MIT" /></a>
  <a href="#-quick-start"><img src="https://img.shields.io/badge/python-3.11%2B-3776AB?style=flat-square" alt="Python" /></a>
  <a href="https://github.com/yt-dlp/yt-dlp"><img src="https://img.shields.io/badge/engine-yt--dlp-F59E0B?style=flat-square" alt="yt-dlp" /></a>
</p>

---

## Why this isn’t just another yt-dlp wrapper

Most Telegram download bots are thin shells: paste link → dump one format → pray the upload works.

**All-Media Downloader Bot** is built as a **product-grade download cockpit**:

| Differentiator | What it means for users |
|----------------|-------------------------|
| **Guided format matrix** | Mode → quality → audio codec / subtitle language → confirm. Only options that make sense for *that* link. |
| **Honest quality picker** | 480p / 720p / 1080p / Max are derived from real format metadata — not fake buttons. |
| **Upload resilience** | 5‑minute media timeouts, automatic retries, document fallback after timeout. |
| **Windows-first ops** | Auto-discovers FFmpeg from WinGet (`Gyan.FFmpeg`) when it’s not on `PATH`. |
| **Single-instance lock** | Prevents the classic “bot answers twice” disaster from multiple pollers. |
| **Operator privacy** | Rate limits, session TTL, temp cleanup, history/stats kept local (never required in git). |
| **BotFather-safe profile** | Does **not** overwrite your name/description on every restart unless you opt in via `.env`. |

Built and maintained by **Gazzy Labs**.

---

## Features

### Media
- **Video** at 480p · 720p · 1080p · Max available  
- **Video + subtitles** (language pick, embed + optional `.srt`)  
- **Audio only** — MP3 / M4A / Opus  
- **Images** — pins, photos, best available size  

### Platforms (via yt-dlp)
YouTube · Instagram · TikTok · X/Twitter · Facebook · Pinterest · Reddit · Vimeo · Twitch · SoundCloud · LinkedIn · Threads · Bilibili · Rumble · VK · and [many more](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md)

### Product UX
- Metadata preview (title, platform, duration, resolutions)  
- Live progress bar + speed + ETA  
- Persistent reply keyboard + inline wizard  
- Per-user history & global stats  
- Friendly error messages (private, geo-block, missing FFmpeg, oversized file)

### Ops
- Docker + docker-compose  
- Optional cookies / proxy  
- Configurable concurrency & rate limits  
- Periodic temp + session cleanup  

---

## Demo flow

```text
You  →  paste https://youtu.be/…
Bot →  🎬 title · platform · available formats
You  →  🎥 Video  |  🎞 +Subs  |  🎵 Audio  |  🖼 Image
You  →  720p / 1080p / Max …
Bot →  progress ▓▓▓▓▓░░░░  then one clean media message
```

---

## Quick start

### 1. Requirements

| Tool | Notes |
|------|--------|
| **Python 3.11+** | 3.12 / 3.13 recommended |
| **FFmpeg** | Required for merge / audio / subtitles |
| **Telegram bot token** | From [@BotFather](https://t.me/BotFather) |

**FFmpeg (Windows):**

```powershell
winget install Gyan.FFmpeg
```

**FFmpeg (Debian/Ubuntu):**

```bash
sudo apt update && sudo apt install -y ffmpeg
```

### 2. Install

```bash
git clone https://github.com/gazzy-source/all-media-downloader.git
cd all-media-downloader

python -m venv .venv

# Windows
.\.venv\Scripts\Activate.ps1

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
```

### 3. Configure `.env`

```env
BOT_TOKEN=123456:ABC-your-token-from-BotFather
ADMIN_IDS=your_telegram_user_id
```

> **Never commit `.env`.** It is gitignored.

### 4. Run

```bash
python run.py
```

Windows helpers:

```powershell
.\scripts\setup.ps1
.\scripts\run.ps1
```

Open your bot in Telegram → `/start` → paste a link.

---

## Configuration reference

| Variable | Default | Description |
|----------|---------|-------------|
| `BOT_TOKEN` | — | **Required.** From @BotFather |
| `ADMIN_IDS` | empty | Comma-separated Telegram IDs (bypass rate limit) |
| `MAX_CONCURRENT_DOWNLOADS` | `3` | Parallel download jobs |
| `MAX_FILE_SIZE_MB` | `49` | Soft cap before refusing Telegram upload (~50 MB Bot API) |
| `RATE_LIMIT_PER_HOUR` | `30` | Per-user downloads / hour |
| `DOWNLOAD_DIR` / `TEMP_DIR` | `downloads` / `temp` | Storage paths |
| `COOKIES_FILE` | — | Netscape cookies for login/age walls |
| `PROXY` | — | `http://` or `socks5://` proxy |
| `FFMPEG_LOCATION` | auto | Folder containing `ffmpeg` binary |
| `BOT_NAME` | — | Optional API override (leave empty to keep BotFather) |
| `BOT_DESCRIPTION` | — | Optional full description override |
| `BOT_SHORT_DESCRIPTION` | — | Optional short about override |

Full BotFather checklist: [`BOTFATHER.md`](BOTFATHER.md)

---

## Docker

```bash
cp .env.example .env
# set BOT_TOKEN in .env

docker compose up --build -d
```

FFmpeg is included in the image.

---

## Project layout

```text
all-media-downloader-bot/
├── bot/
│   ├── main.py              # App entry, polling, timeouts, profile policy
│   ├── config.py            # Env-driven settings
│   ├── handlers/            # Commands + download wizard
│   ├── keyboards/           # Inline / reply UI
│   ├── services/
│   │   ├── downloader.py    # yt-dlp engine + format intelligence
│   │   ├── session.py       # Multi-step download state
│   │   ├── history.py       # Local JSON history & stats
│   │   └── rate_limit.py    # Anti-spam
│   └── utils/               # URL helpers, FFmpeg discovery, instance lock
├── docs/                    # Architecture & deployment guides
├── scripts/                 # Windows setup / run helpers
├── .env.example
├── Dockerfile
├── docker-compose.yml
└── run.py
```

Deep dive: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) · deploy tips: [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)

---

## Updating extractors

Sites break often. Keep yt-dlp fresh:

```bash
pip install -U yt-dlp
```

---

## Contributing

Contributions are welcome — bug fixes, new UX, docs, and tests.

1. Fork → feature branch → PR  
2. Read [`CONTRIBUTING.md`](CONTRIBUTING.md)  
3. Be kind: [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)

---

## Security

See [`SECURITY.md`](SECURITY.md). Report vulnerabilities privately — never paste real bot tokens in issues.

---

## Legal & ethics

- This software is provided under the **MIT License** (see [`LICENSE`](LICENSE)).  
- **You** are responsible for how you use it.  
- Only download content you have the right to access.  
- Respect platform Terms of Service and local copyright law.  
- Gazzy Labs does not host or redistribute third-party media.

---

## Credits

| Project | Role |
|---------|------|
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Extraction & download engine |
| [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) | Telegram Bot API framework |
| [FFmpeg](https://ffmpeg.org/) | Merge, convert, subtitles |

**All-Media Downloader Bot** — designed & built by **Gazzy Labs**.

---

## Star history

If this project saves you time, a ⭐ on GitHub helps others find a **well-built** open alternative.
