# Cookies for YouTube / Instagram (VPS)

YouTube often returns:

> Sign in to confirm you’re not a bot

Datacenter IPs trigger this. **Browser cookies from a real logged-in account** usually fix it.

## 1. Export cookies on your PC

1. Install a browser extension that exports **Netscape** cookies, e.g.  
   **“Get cookies.txt LOCALLY”** (Chrome/Firefox).
2. Log into **youtube.com** in that browser.
3. Export cookies for `youtube.com` (or all sites).
4. Save the file as `cookies.txt`.

## 2. Upload to the VPS

```powershell
scp -i "C:\Users\gajal\Downloads\GL\ssh-key-2026-07-13.key" `
  "C:\path\to\cookies.txt" `
  ubuntu@130.210.38.240:/opt/all-media-downloader/cookies.txt
```

On the VPS:

```bash
chmod 600 /opt/all-media-downloader/cookies.txt
# ensure .env has:
# COOKIES_FILE=cookies.txt
sudo systemctl restart all-media-downloader
sudo journalctl -u all-media-downloader -n 20 --no-pager
# should log: Cookies: cookies.txt  (or full path)
```

## 3. Security

- `cookies.txt` is **secret** (session hijack risk). Never commit it to git.
- Refresh cookies if YouTube breaks again (sessions expire).
- Prefer a secondary Google account used only for the bot.

## 4. Update yt-dlp often

```bash
cd /opt/all-media-downloader
.venv/bin/pip install -U yt-dlp
sudo systemctl restart all-media-downloader
```
