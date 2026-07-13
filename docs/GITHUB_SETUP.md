# Connect this project to GitHub (step by step)

Follow these steps **in order**. Your real bot token must never appear on GitHub.

## Before you start

1. You have a [GitHub](https://github.com) account  
2. Git is installed (`git --version`)  
3. Local project works  
4. `.env` exists only on your machine and is listed in `.gitignore`  

Optional: install [GitHub CLI](https://cli.github.com/) (`gh`) for faster repo creation.

---

## Step 1 — Confirm secrets stay private

From the project folder:

```powershell
# Must print nothing (or only .env.example)
git status   # after init
# Double-check:
Get-Content .gitignore | Select-String "\.env"
```

Never run `git add .env`.

---

## Step 2 — Create an empty GitHub repository

### Option A — Website

1. Open [https://github.com/new](https://github.com/new)  
2. **Repository name:** `all-media-downloader-bot` (recommended)  
3. **Description:** `Guided multi-platform Telegram media downloader by Gazzy Labs`  
4. **Public** (for open source)  
5. **Do not** add README, .gitignore, or license on GitHub (we already have them)  
6. Click **Create repository**  
7. Copy the repo URL, e.g. `https://github.com/YOUR_USER/all-media-downloader-bot.git`

### Option B — GitHub CLI

```powershell
gh auth login
gh repo create all-media-downloader-bot --public --source=. --remote=origin --description "Guided multi-platform Telegram media downloader by Gazzy Labs"
```

(If the repo is created empty without push, use Step 4 remote URL.)

---

## Step 3 — Local git init & first commit

Already done for you if maintainers ran setup; otherwise:

```powershell
cd C:\Users\gajal\all-media-downloader-bot
git init
git branch -M main
git add .
git status   # verify .env is NOT listed
git commit -m "Initial open-source release: All-Media Downloader Bot v1.0.0"
```

---

## Step 4 — Connect remote & push

```powershell
git remote add origin https://github.com/YOUR_USER/all-media-downloader-bot.git
git push -u origin main
```

Use SSH if you prefer:

```powershell
git remote add origin git@github.com:YOUR_USER/all-media-downloader-bot.git
git push -u origin main
```

---

## Step 5 — GitHub repository settings (recommended)

On the repo page → **Settings**:

| Setting | Suggestion |
|---------|------------|
| **About** → Description | Same as README tagline |
| **About** → Topics | `telegram-bot`, `yt-dlp`, `downloader`, `python`, `open-source`, `gazzy-labs` |
| **About** → Website | Optional demo / docs |
| Features | Issues ✅ · Discussions optional |
| **Actions** | Allow GitHub Actions (CI workflow included) |

**Security → Code security:** enable Dependabot alerts if available.

---

## Step 6 — First release (optional)

1. **Releases** → **Draft a new release**  
2. Tag: `v1.0.0`  
3. Title: `All-Media Downloader Bot v1.0.0`  
4. Paste notes from `CHANGELOG.md`  
5. Publish  

---

## Step 7 — After going public

1. Update README clone URL placeholders (`<YOU>/<REPO>`) if any remain  
2. Update `pyproject.toml` `[project.urls]` to your real GitHub path  
3. **Revoke the bot token if it was ever pasted in chat** and put a fresh one only in local `.env`  
4. Share the repo link  

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `rejected` on push | Create empty repo without README; or `git pull --rebase` then push |
| Auth failed | Use [PAT](https://github.com/settings/tokens) or `gh auth login` |
| `.env` staged by mistake | `git reset HEAD .env` and confirm gitignore |
| CI red | Check Actions tab; usually Python/setup issue on PR |

---

## Suggested repo blurb (for social)

> Open-sourced **All-Media Downloader Bot** by Gazzy Labs — a guided Telegram wizard for video/audio/images from 1000+ sites (yt-dlp), with smart quality picks, resilient uploads, and self-host defaults. MIT licensed.
