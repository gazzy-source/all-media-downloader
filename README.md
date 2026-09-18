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
  <a href="https://github.com/gazzy-source/all-media-downloader/actions/workflows/ci.yml"><img src="https://github.com/gazzy-source/all-media-downloader/actions/workflows/ci.yml/badge.svg?style=flat-square" alt="CI status" /></a>
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
- **No cookies required** — public posts download out of the box  
- Optional PO-token provider / cookies / proxy  
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
| `COOKIES_FILE` | — | Optional. Netscape cookies, only for private/age-walled posts |
| `POT_PROVIDER_URL` | auto-detect | bgutil PO-token provider. Unlocks full-quality YouTube without cookies |
| `PROXY` | — | `http://` or `socks5://` proxy |
| `PROXY_HOSTS` | all | Comma-separated hosts to route through `PROXY`. Empty = everything |
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

Compose also starts a **bgutil PO-token provider** and points the bot at it via
`POT_PROVIDER_URL` — that is what gives cookieless YouTube its full quality.

---

## YouTube without cookies

The bot needs **no cookies for public videos**. YouTube is tried in this order,
and whichever step works is remembered for the rest of the process:

| Step | Client | Needs | Quality |
|------|--------|-------|---------|
| 1 | yt-dlp default rotation | a PO token for the top formats | up to 4K |
| 2 | `android` | nothing | 360p |
| 3 | `android_vr` | nothing | 360p |

Since YouTube moved to SABR, the highest media URLs from step 1 return **HTTP
403** unless a PO token is attached. Measured with no cookies and no provider
(yt-dlp 2026.8.19): 480p and 720p come straight from step 1, while a 1080p
request 403s and falls back to step 2 at 360p. Step 2 always returns bytes, so a
download degrades instead of failing — and captions report the resolution
actually delivered, not the button that was pressed.

The order is quality-first on purpose: leading with `android` would cap every
download at 360p even where 720p is available. The winning step is remembered,
so a server that always 403s pays the failed attempt only once per process.

To get 720p/1080p without cookies, run a PO-token provider:

```bash
docker compose up -d bgutil-provider     # compose wires POT_PROVIDER_URL for you
# or, outside compose, publish port 4416 and set:
# POT_PROVIDER_URL=http://127.0.0.1:4416
```

An unset `POT_PROVIDER_URL` is probed once at startup against
`http://127.0.0.1:4416`; the startup log states which mode is active.

`cookies.txt` remains optional and is only needed for private, members-only, or
age-restricted content.

---

## Running on a VPS (datacenter IP)

Several platforms rate the server's IP, not the request. Measured on an Oracle
Cloud VPS with this code and no cookies, all five of these failed while the same
code passed them from a residential IP:

| Platform | What the server gets |
|----------|----------------------|
| YouTube | `Sign in to confirm you're not a bot` on **every** player client |
| Reddit | `Account authentication is required` |
| SoundCloud | `This video is DRM protected` |
| Tumblr | HTTP 403 |
| Bilibili | HTTP 412 |

These are not extractor bugs, and a PO-token provider does **not** fix them: the
provider mints tokens successfully from that host, yet YouTube still refuses
every client, because the block lands on the initial player request.

Everything else works from the same host — X/Twitter, Instagram, Facebook,
Pinterest, Twitch, Rumble, VK, Snapchat and LinkedIn all download normally.

### Fix: Cloudflare WARP as a local SOCKS proxy (free)

WARP gives the host a non-datacenter egress IP at no cost and without
credentials. In **proxy mode** it only opens a local SOCKS listener — it does
not touch the default route, so SSH and everything else are unaffected.

```bash
curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg   | sudo gpg --yes --dearmor -o /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ $(lsb_release -cs) main"   | sudo tee /etc/apt/sources.list.d/cloudflare-client.list
sudo apt-get update && sudo apt-get install -y cloudflare-warp

sudo warp-cli --accept-tos registration new
sudo warp-cli --accept-tos mode proxy          # proxy only — no default-route change
sudo warp-cli --accept-tos proxy port 40000
sudo warp-cli --accept-tos connect
sudo systemctl enable --now warp-svc           # reconnects by itself on restart
```

Then point the bot at it, proxying only the hosts that need it so the platforms
that already work stay off the proxy and stay fast:

```dotenv
PROXY=socks5://127.0.0.1:40000
PROXY_HOSTS=youtube.com,youtu.be,music.youtube.com,reddit.com,redd.it,soundcloud.com
```

Measured on the same Oracle VPS after enabling it — **YouTube, Reddit and
SoundCloud all recovered**, YouTube at the full quality ladder (1080p+ formats
listed, a 720p merge in 6.1s):

| | Before | After |
|---|---|---|
| Platforms working | 10 / 15 | **13 / 15** |
| YouTube | bot-walled on every client | full ladder via WARP + PO token |

`Tumblr` and `Bilibili` still fail, and a proxy will not help unless it exits in
another country: both Oracle and free WARP egress from `IN` here, Tumblr is
blocked in India and Bilibili geo-restricts. Free WARP cannot pick an exit
country — these need a proxy located elsewhere.

A paid residential/mobile proxy works the same way; just replace `PROXY`.
`PROXY_HOSTS` matters more there, since those bill per GB and video is heavy.

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
