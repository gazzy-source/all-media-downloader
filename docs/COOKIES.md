# Cookies for YouTube / Instagram (VPS)

YouTube often returns:

> Sign in to confirm you’re not a bot  
> The provided YouTube account cookies are no longer valid

**Datacenter VPS IPs** trigger this. Fresh Netscape cookies usually fix it — until Google rotates them again.

## Critical rules (or cookies die immediately)

1. Export cookies **only when logged into youtube.com**
2. After export: **do not open YouTube in that browser** until the bot has used them  
   (opening YT rotates `LOGIN_INFO` / PSID and kills the export)
3. Upload + restart the bot **right away** (within 1–2 minutes)
4. Prefer a **secondary Google account** used only for the bot
5. After the bot works, avoid heavy YouTube browsing in the **same browser profile** you exported from

## 1. Export on your PC (exact steps)

1. Install **Get cookies.txt LOCALLY** (Chrome Web Store).
2. Close other YouTube tabs.
3. Open **only** `https://www.youtube.com` and confirm you are logged in.
4. Click the extension → **Export** → save as:

```
C:\Users\gajal\Downloads\GL\cookies.txt
```

5. **Immediately** close that YouTube tab (do not open YouTube again yet).
6. Upload (step 2) right away.

Optional: also open Instagram logged-in, then export “all cookies” once for IG.

## 2. Upload to VPS (PowerShell)

```powershell
scp -i "C:\Users\gajal\Downloads\GL\ssh-key-2026-07-13.key" `
  "C:\Users\gajal\Downloads\GL\cookies.txt" `
  ubuntu@130.210.38.240:/opt/all-media-downloader/cookies.txt

ssh -i "C:\Users\gajal\Downloads\GL\ssh-key-2026-07-13.key" ubuntu@130.210.38.240 `
  "chmod 600 /opt/all-media-downloader/cookies.txt; rm -f /opt/all-media-downloader/data/cookies.sanitized.txt /opt/all-media-downloader/data/cookies.runtime.txt; sudo systemctl restart all-media-downloader"
```

The bot builds:

- `data/cookies.sanitized.txt` — media domains only (**source of truth**)
- `data/cookies.runtime.txt` — disposable copy yt-dlp may rewrite

Your uploaded `cookies.txt` is **never** overwritten by the bot.

## 3. Verify

```bash
cd /opt/all-media-downloader
export PATH="$HOME/.deno/bin:$PATH"
# Always test with a COPY so the original is not mutated
cp cookies.txt /tmp/yt-cookies-test.txt
.venv/bin/yt-dlp --cookies /tmp/yt-cookies-test.txt --skip-download \
  --print "%(id)s %(title).40s" "https://www.youtube.com/watch?v=jNQXAC9IVRw"
```

- Success → title prints; bot is ready.
- `cookies are no longer valid` → re-export (you reopened YouTube, or Google already rotated).
- `Sign in to confirm you're not a bot` with **fresh** cookies → VPS IP is hard-blocked; use a residential `PROXY=` in `.env` or re-export again from a secondary account.

## 4. Security

- Never commit `cookies.txt` to git
- `chmod 600` on the server
- Rotate/revoke if leaked
- Use a throwaway Google account if possible

## 5. Keep yt-dlp fresh

```bash
cd /opt/all-media-downloader
.venv/bin/pip install -U "yt-dlp[default]"
sudo systemctl restart all-media-downloader
```

## 6. If cookies keep dying within minutes

Datacenter IPs are treated as bots. Options:

1. Re-export more carefully (secondary account, no browser use after export)
2. Set a **residential HTTP/SOCKS proxy** in `/opt/all-media-downloader/.env`:

```env
PROXY=socks5://user:pass@host:port
```

Then `sudo systemctl restart all-media-downloader`.
