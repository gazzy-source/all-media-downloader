# @BotFather setup — All-Media Downloader Bot

Copy-paste these into [@BotFather](https://t.me/BotFather).

> The running bot **does not overwrite** your profile unless you set
> `BOT_NAME` / `BOT_DESCRIPTION` / `BOT_SHORT_DESCRIPTION` in `.env`.

## 1. Create the bot

```
/newbot
```

- **Name:** `All-Media Downloader Bot` (or your brand)
- **Username:** must end in `bot` (e.g. `AllMediaDownloaderBot`)

Save the **HTTP API token** — treat it like a password.

## 2. About / bio

```
/setabouttext
```

```
Download videos, audio, and images from any platform instantly. Built by Gazzy Labs.
```

## 3. Description (shown before Start)

```
/setdescription
```

```
Download videos, audio, and images from any platform instantly.

Supports YouTube, Instagram, TikTok, X, Facebook, Pinterest & 1000+ sites.
Choose 480p · 720p · 1080p · Max · Audio · Subtitles · Images.

Guided wizard · resilient uploads · self-hosted.
Built by Gazzy Labs.
```

## 4. Commands

Commands are also set automatically on bot start. To set manually:

```
/setcommands
```

```
start - Start the bot
help - How to use
platforms - Supported platforms
history - Your recent downloads
stats - Usage statistics
settings - Preferences & limits
cancel - Cancel current download
```

## 5. Profile picture

```
/setuserpic
```

Upload a clear square logo.

## 6. Privacy (groups)

```
/setprivacy
```

- **Enable** (default): bot only sees commands in groups  
- **Disable**: bot sees all messages (needed if users paste links without commands)

For a personal DM downloader, **Enable** is fine.

## 7. Local config

```env
BOT_TOKEN=123456789:AA...your_token_here
ADMIN_IDS=your_numeric_telegram_id
```

Find your user id via [@userinfobot](https://t.me/userinfobot) or similar.

## Security

If the token ever leaks (chat, screenshot, git history):

1. BotFather → your bot → **API Token** → **Revoke**  
2. Put the new token in `.env` only  
3. Restart the bot  
