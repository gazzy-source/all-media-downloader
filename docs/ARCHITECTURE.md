# Architecture

All-Media Downloader Bot is a **guided download cockpit** on top of yt-dlp and the Telegram Bot API — not a one-shot CLI dump into chat.

## High-level flow

```text
┌─────────────┐     URL      ┌──────────────────┐
│  Telegram   │ ───────────► │  handlers/       │
│  user chat  │ ◄─────────── │  download.py     │
└─────────────┘  messages    │  start.py        │
                             └────────┬─────────┘
                                      │
                    ┌─────────────────┼─────────────────┐
                    ▼                 ▼                 ▼
             session.py        rate_limit.py      history.py
           (wizard state)      (anti-spam)       (local JSON)
                    │
                    ▼
             downloader.py  ──► yt-dlp ──► FFmpeg (merge/audio/subs)
                    │
                    ▼
             resilient upload (timeouts · retries · document fallback)
```

## Core ideas

### 1. Session-driven wizard

`DownloadSession` holds metadata + user choices (`mode`, `quality`, `subtitle_lang`, …) for a short TTL.  
Callbacks are namespaced: `mode:{sid}:video`, `quality:{sid}:720`, `go:{sid}`.

This is what makes the bot feel like a **product**, not a raw command runner.

### 2. Format intelligence

After `extract_info`, we parse yt-dlp formats to learn:

- has video / audio / image  
- available heights  
- subtitle languages  
- rough size estimates  

Keyboards are built from that — empty promises (e.g. “4K” when only 360p exists) are avoided where possible.

### 3. Download manager

`DownloadManager`:

- runs blocking yt-dlp work in a thread pool  
- limits concurrency with a semaphore  
- streams progress hooks back to Telegram (throttled)  
- isolates each job under `temp/dl_<id>/`

### 4. Upload path (unique pain point)

Telegram Bot API uploads are slow and flaky for multi‑MB files. We:

- set global + per-request **5 minute** read/write timeouts  
- retry on `TimedOut` / network errors  
- fall back from `send_video` → `send_document`  
- delete the progress message after a successful send (one clean output)

### 5. Operator safeguards

| Mechanism | Purpose |
|-----------|---------|
| Single-instance lock (`data/bot.lock`) | One poller only — no duplicate replies |
| Rate limiter | Abuse resistance |
| Temp cleanup job | Disk hygiene |
| FFmpeg auto-discovery | WinGet paths without manual PATH surgery |
| Profile policy | Don’t clobber BotFather branding on restart |

## Module map

| Path | Responsibility |
|------|----------------|
| `bot/main.py` | Application wiring, timeouts, jobs, entry |
| `bot/config.py` | Env configuration |
| `bot/handlers/*` | User-facing commands & callback flow |
| `bot/keyboards/menus.py` | UI builders |
| `bot/services/downloader.py` | yt-dlp options & result types |
| `bot/services/session.py` | In-memory sessions |
| `bot/utils/ffmpeg.py` | Locate FFmpeg |
| `bot/utils/instance_lock.py` | Process mutex |
| `bot/utils/helpers.py` | URLs, formatting, progress bar |

## What we intentionally don’t do

- No cloud database by default (simpler self-host, better privacy)  
- No public file host (files go only to the requesting chat)  
- No multi-tenant SaaS billing layer (this is a self-hosted bot)  

Those can be added as optional plugins without breaking the core wizard.

## Extension points

1. **New download modes** — add a mode key + keyboard + yt-dlp option branch  
2. **Persistent storage** — swap `history.py` JSON for SQLite  
3. **Webhook mode** — replace `run_polling` for production scale  
4. **Local Bot API server** — raise the ~50 MB upload ceiling  
