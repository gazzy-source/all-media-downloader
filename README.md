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
  <a href="#testing--quality"><img src="https://img.shields.io/badge/tests-399%20passing-16A34A?style=flat-square" alt="399 tests" /></a>
  <a href="#testing--quality"><img src="https://img.shields.io/badge/coverage-73%25-16A34A?style=flat-square" alt="73% coverage" /></a>
</p>

---

## Why this isn’t just another yt-dlp wrapper

Most Telegram download bots are thin shells: paste link → dump one format → pray the upload works.

**All-Media Downloader Bot** is built as a **product-grade download cockpit**:

| Differentiator | What it means for users |
|----------------|-------------------------|
| **Guided format matrix** | Mode → quality → audio codec / subtitle language. Only options that make sense for *that* link, and the last choice starts the download — no dead-end confirm tap. |
| **Honest quality picker** | 480p / 720p / 1080p / Max are derived from real format metadata — not fake buttons. Captions report the height actually delivered, so a 360p fallback never claims to be 1080p. |
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

## Tech stack

| Layer | Choice | Why this one |
|-------|--------|--------------|
| Language | **Python 3.11+** | Structural pattern matching, `X \| None` unions, `asyncio.TaskGroup`-era stdlib. CI runs 3.11 / 3.12 / 3.13. |
| Bot framework | **python-telegram-bot 21.x** (`[job-queue]`) | Mature async API, built-in `JobQueue` for the cleanup loop, per-request timeout control for multi-MB uploads. |
| Extraction engine | **yt-dlp** | 1000+ extractors, actively maintained. Pinned to a recent floor because site extractors break weekly. |
| TLS impersonation | **curl_cffi** | Forges a real Chrome JA3/TLS fingerprint, which is what gets past Instagram/Facebook bot walls without cookies. |
| Anti-bot tokens | **bgutil PO-token provider** | Supplies YouTube PO tokens so cookieless extraction reaches the full format ladder. |
| Media processing | **FFmpeg** | DASH/HLS muxing, audio extraction, subtitle embedding. Auto-discovered on Windows from WinGet. |
| Concurrency | `asyncio` + a dedicated `ThreadPoolExecutor` | yt-dlp is blocking; downloads run in their own pool so one job can't starve the event loop. A semaphore caps parallelism. |
| Persistence | Flat JSON | History/stats are small, local and operator-owned. No database to run or back up on a 1 GB VPS. |
| Packaging | `pyproject.toml`, Docker, docker-compose | Reproducible local, container and systemd deploys. |
| CI | **GitHub Actions** | Import smoke test, `compileall`, full suite across a 3-version Python matrix on every push and PR. |

**Tooling:** `pytest` + `pytest-asyncio` + `pytest-cov` · `pyflakes` · `coverage` (branch mode) · `systemd` · `journalctl` · `docker`

---

## Engineering notes

The interesting work in this project was not wiring up a bot API — it was keeping
extraction working against platforms that actively fight it. A few problems worth
writing down, each found by measurement rather than assumption:

<details>
<summary><b>Cookieless YouTube after SABR</b></summary>

YouTube's move to SABR means the default client's media URLs return **HTTP 403**
unless a PO token is attached, so "it extracts fine" and "it downloads" stopped
being the same thing. Tested every player client directly: `tv` errors,
`ios`/`mweb`/`tv_simply`/`web_safari`/`web_embedded` return storyboards only, and
`android_vr` lists a format but 403s on the stream. `android` was the one client
that reliably returned bytes.

The ladder is deliberately **quality-first, not success-first**: the full format
rotation leads so a trusted IP or a reachable token provider still gets 1080p+,
with `android` directly behind it as a guaranteed 360p floor. A sticky winner
promotes whichever actually worked, so a server that always 403s pays the failed
attempt once per process rather than once per download.

Two latent bugs fell out of this: the PO-token `base_url` was passed as a bare
string where the plugin reads `_configuration_arg(...)[0]`, so it silently
resolved to `"h"`; and fallback strategies replaced `extractor_args` wholesale,
dropping the token config exactly when it was needed.
</details>

<details>
<summary><b>"Unknown codec" is not "no codec"</b></summary>

yt-dlp uses the *string* `"none"` for "this track is absent" and `None` for
"unknown". Collapsing them with `f.get("vcodec") or "none"` is an easy mistake
and it quietly broke four platforms: Twitch clips and Rumble videos were
classified image-only, LinkedIn posts read as audio-only, and every X/Twitter
video hid its Audio option — because X's progressive formats report
`acodec: None`.
</details>

<details>
<summary><b>Format selectors that can never match</b></summary>

`b` and `best` only match a format carrying **both** tracks. Reddit serves no
progressive format at all — every video entry is video-only, every audio entry
audio-only — so a progressive-first selector matched nothing and *every* Reddit
download failed. Every selector now ends in an unrestricted `bv*+ba` merge, and
Reddit leads with it. A parametrised test asserts the property across all
hosts × qualities rather than spot-checking one string.
</details>

<details>
<summary><b>A 10 MB range request that broke fragmented downloads</b></summary>

A global `http_chunk_size` makes yt-dlp fetch via HTTP Range, which fragmented
HLS/DASH cannot serve — the fragment returns unusable and the job dies with
"The downloaded file is empty". Bisecting the option set proved it was the sole
cause of every Reddit and VK failure. It is now scoped to YouTube, where
chunking is the documented throttling mitigation.
</details>

<details>
<summary><b>A self-defeating bot-detection fingerprint</b></summary>

The bot enabled curl_cffi Chrome impersonation *and* forced its own
`Chrome/131` User-Agent header — so the TLS handshake advertised one browser and
the header another. Anti-bot systems fingerprint exactly that mismatch.
Measured over repeated runs against Bilibili: **both = 1/3 success, either alone
= 3/3**. The forced UA is now dropped whenever impersonation is active.
</details>

<details>
<summary><b>Datacenter IP reputation, and what actually fixes it</b></summary>

Several platforms rate the server's IP, not the request. On an Oracle Cloud VPS,
YouTube refused every client, Reddit demanded account auth and SoundCloud
reported DRM — while identical code passed all three from a residential IP.

A PO-token provider does **not** fix this: it mints tokens successfully from the
blocked host and YouTube still refuses, because the block lands on the initial
player request. Routing egress through **Cloudflare WARP in proxy mode** (free,
no credentials, and it leaves the host default route untouched) recovered all
three. `PROXY_HOSTS` limits the proxy to the hosts that need it, so metered
bandwidth isn't spent on the platforms that already work.
</details>

<details>
<summary><b>The error path that crashed</b></summary>

Every unreadable link answered "Something went wrong" instead of the reason.
`editMessageText` accepts an *inline* keyboard only, and the error branch passed
the persistent reply keyboard — Telegram returned
`BadRequest: Inline keyboard expected`, which escaped to the global handler. The
failure reason was unreachable by construction.

Worse, an existing test *asserted* the bug (`reply_markup is not None`). The fix
is guarded structurally: an AST scan fails if any `edit_*` call anywhere in
`bot/` is handed a reply-keyboard factory.
</details>

---

## Testing & quality

```bash
pytest                        # 399 passing, branch coverage on bot/
pytest -m live                # 14 opt-in tests that hit the real network
RUN_LIVE_TESTS=1 pytest       # enable them
python -m pyflakes bot/       # lint
```

| | |
|---|---|
| Automated tests | **399** passing, plus **14** opt-in live-network tests |
| Coverage | **73%** of `bot/`, branch mode |
| CI | GitHub Actions on Python **3.11 / 3.12 / 3.13**, every push and PR |
| Source | ~5.0k lines in `bot/`, ~4.0k lines of tests |

Beyond ordinary unit tests, the suite includes a few **structural** ones that
catch whole classes of mistake rather than single instances:

- **Keyboard contract** — AST-scans `bot/` and fails if any `edit_*` call is
  passed a reply keyboard, which Telegram rejects at runtime.
- **Selector invariants** — asserts every format selector, across all hosts and
  qualities, can reach a merge fallback.
- **Suite hygiene** — fails on duplicate test class/method names, which silently
  shadow earlier tests (this caught four assertions that had stopped running).
- **Environment independence** — proxy and token settings are pinned per-test, so
  the suite cannot pass locally and fail on a server that configures them.

Fixes are verified against the broken revision before being committed: a test
that cannot fail on the bug it describes is not worth having.

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
| `CHANNEL_REPLACE_LINK` | `1` | In channels, delete the link post after posting the downloaded media |
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

## Channels: the link is replaced by the media

Post a link in a channel the bot administers and the bot posts the downloaded
media as a new message, then deletes the original link post — so nothing has to
be tidied up by hand.

The media is sent **before** the link is deleted, on purpose: if the upload
fails, the link is still there. A failed download never destroys what somebody
posted.

The bot needs the **Delete messages** admin right in the channel. Without it the
media is still posted and the link is simply left in place; the journal says so
(`Could not remove the link post …`).

Music and podcast links (`music.youtube.com`, SoundCloud, Bandcamp, Mixcloud,
Apple Podcasts) are delivered as an **audio** message rather than a video.
Spotify is not supported: yt-dlp has no Spotify extractor because its tracks are
DRM-protected, so those links get the normal "no downloadable media" reply.

Set `CHANNEL_REPLACE_LINK=0` to keep the link post.

Groups and DMs are unaffected.

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
├── tests/                   # 399 tests incl. structural + opt-in live-network
├── docs/                    # Architecture & deployment guides
├── scripts/                 # Setup / health-endpoint helpers
├── .github/workflows/       # CI: 3.11/3.12/3.13 matrix
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
