# Contributing to All-Media Downloader Bot

Thanks for helping make this the **best self-hosted multi-platform Telegram downloader** it can be.

## Ways to contribute

- Bug reports with repro steps (platform, URL type, quality mode)  
- Documentation fixes  
- UX improvements to the wizard / keyboards  
- FFmpeg / Windows path edge cases  
- Tests for pure helpers (`bot/utils/*`)  
- Optional features that don’t break the simple self-host story  

## Development setup

```bash
git clone https://github.com/gazzy-source/all-media-downloader.git
cd all-media-downloader
python -m venv .venv
source .venv/bin/activate  # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env
# BOT_TOKEN=... for live tests only — never commit it
python run.py
```

## Code guidelines

1. **No secrets** in code, commits, or screenshots (tokens, cookies).  
2. Prefer small, focused PRs.  
3. Match existing structure: handlers → services → utils.  
4. Keep the wizard flow user-friendly; don’t dump raw yt-dlp dumps into chat.  
5. Log useful ops info; never log full bot tokens.  
6. New dependencies need a short justification in the PR.  

## Pull request checklist

- [ ] Describes *what* and *why*  
- [ ] Works with Python 3.11+  
- [ ] `.env` / tokens not included  
- [ ] Docs updated if behavior or config changed  
- [ ] Manual test notes (e.g. “YouTube 720p + audio OK”)  

## Issue labels (suggested)

| Label | Use |
|-------|-----|
| `bug` | Something broken |
| `enhancement` | New capability |
| `docs` | Documentation only |
| `platform` | Site-specific extractor issue (often upstream yt-dlp) |
| `good first issue` | Friendly for newcomers |

## Upstream issues

If a **specific website** stops working, check [yt-dlp issues](https://github.com/yt-dlp/yt-dlp/issues) first — many “bot bugs” are extractor updates.

## License

By contributing, you agree your contributions are licensed under the MIT License (same as this repository).
