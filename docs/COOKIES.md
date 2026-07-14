# Cookies for YouTube / Instagram (VPS)

YouTube often returns:

> Sign in to confirm you’re not a bot  
> The provided YouTube account cookies are no longer valid

**Datacenter VPS IPs** trigger this. Fresh Netscape cookies usually fix it — until Google rotates them again.

## Critical rules (or cookies die immediately)

1. Export cookies **only when logged into youtube.com**
2. After export: **do not open YouTube in that browser** until the bot has used them  
   (opening YT rotates `LOGIN_INFO` / PSID and kills the export)
3. Upload + restart the bot **right away**
4. Prefer a **secondary Google account** used only for the bot

## 1. Export on your PC

1. Install **Get cookies.txt LOCALLY** (Chrome).
2. Open `https://www.youtube.com` (logged in).
3. Export → save as:

```
C:\Users\gajal\Downloads\GL\cookies.txt
```

4. Optional: also open Instagram logged-in, then export “all cookies” once for IG.

## 2. Upload to VPS (PowerShell)

```powershell
scp -i "C:\Users\gajal\Downloads\GL\ssh-key-2026-07-13.key" `
  "C:\Users\gajal\Downloads\GL\cookies.txt" `
  ubuntu@130.210.38.240:/opt/all-media-downloader/cookies.txt

ssh -i "C:\Users\gajal\Downloads\GL\ssh-key-2026-07-13.key" ubuntu@130.210.38.240 `
  "chmod 600 /opt/all-media-downloader/cookies.txt; rm -f /opt/all-media-downloader/data/cookies.sanitized.txt; sudo systemctl restart all-media-downloader"
```

The bot auto-builds `data/cookies.sanitized.txt` (media domains only).

## 3. Verify

```bash
cd /opt/all-media-downloader
export PATH="$HOME/.deno/bin:$PATH"
.venv/bin/yt-dlp --cookies cookies.txt --skip-download \
  --print "%(id)s %(title).40s" "https://www.youtube.com/watch?v=jNQXAC9IVRw"
```

If you see `cookies are no longer valid` → re-export and re-upload.

## 4. Security

- Never commit `cookies.txt` to git
- `chmod 600` on the server
- Rotate/revoke if leaked

## 5. Keep yt-dlp fresh

```bash
cd /opt/all-media-downloader
.venv/bin/pip install -U "yt-dlp[default]"
sudo systemctl restart all-media-downloader
```
