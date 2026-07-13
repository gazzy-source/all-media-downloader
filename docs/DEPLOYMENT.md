# Deployment guide

## Local (development)

```bash
python -m venv .venv
source .venv/bin/activate   # or .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env
# edit BOT_TOKEN
python run.py
```

## Docker (recommended on a VPS)

```bash
cp .env.example .env
nano .env   # BOT_TOKEN, ADMIN_IDS
docker compose up --build -d
docker compose logs -f bot
```

### Persist data

`docker-compose.yml` mounts:

- `./downloads` — optional long-term files  
- `./temp` — working directory  
- `./data` — history, stats, instance lock  

## Systemd (Linux bare metal)

`/etc/systemd/system/all-media-bot.service`:

```ini
[Unit]
Description=All-Media Downloader Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=bot
WorkingDirectory=/opt/all-media-downloader-bot
Environment=PATH=/opt/all-media-downloader-bot/.venv/bin
ExecStart=/opt/all-media-downloader-bot/.venv/bin/python run.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now all-media-bot
```

## Production checklist

- [ ] Unique bot token; never committed  
- [ ] `ADMIN_IDS` set to your Telegram user id  
- [ ] FFmpeg installed (or Docker image)  
- [ ] `pip install -U yt-dlp` on a schedule (cron weekly)  
- [ ] Disk space for `temp/` (cleanup job every 15 min)  
- [ ] Only **one** process/container running the bot  
- [ ] Optional: `COOKIES_FILE` for age-restricted sources  
- [ ] Optional: reverse proxy / firewall if you later switch to webhooks  

## Telegram 50 MB limit

Official Bot API bots can upload ~**50 MB**. For larger files:

1. Prefer lower quality / audio-only, or  
2. Run a [local Bot API server](https://github.com/tdlib/telegram-bot-api) (advanced)

## Scaling notes

Polling + `MAX_CONCURRENT_DOWNLOADS` is fine for personal / small communities.  
For heavy traffic: webhooks, worker queue, shared object storage, and a single orchestrator process.

## Updating

```bash
git pull
source .venv/bin/activate
pip install -r requirements.txt
pip install -U yt-dlp
# restart process / docker compose up -d --build
```
