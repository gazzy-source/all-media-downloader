# Raise Telegram upload limit (Local Bot API)

Official `api.telegram.org` bots can upload only ~**50 MB**.

A **local Bot API server** allows up to ~**2 GB**.

> Your VPS has ~1 GB RAM. Local Bot API + this bot can be tight; add swap if needed.

## Requirements

1. `api_id` + `api_hash` from https://my.telegram.org  
2. Docker or the official binary: https://github.com/tdlib/telegram-bot-api  

## Quick Docker example

```bash
# On VPS — needs ~512MB+ free RAM
docker run -d --name telegram-bot-api --restart unless-stopped \
  -p 127.0.0.1:8081:8081 \
  -v /opt/telegram-bot-api-data:/var/lib/telegram-bot-api \
  -e TELEGRAM_API_ID=YOUR_API_ID \
  -e TELEGRAM_API_HASH=YOUR_API_HASH \
  aiogram/telegram-bot-api:latest \
  --local
```

Point the downloader bot at it (`.env`):

```env
TELEGRAM_API_URL=http://127.0.0.1:8081/bot
MAX_FILE_SIZE_MB=1900
```

Restart:

```bash
sudo systemctl restart all-media-downloader
```

## Without local API

Keep `MAX_FILE_SIZE_MB=49` and prefer 720p for long videos.
