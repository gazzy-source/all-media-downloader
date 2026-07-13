# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| `main` (latest) | ✅ |
| Older tags | Best-effort only |

## Reporting a vulnerability

**Do not open a public GitHub issue for security problems.**

Please report privately:

1. Open a **GitHub Security Advisory** on this repository, or  
2. Email the maintainer listed in the repo profile / `Gazzy Labs` contact

Include:

- Affected version / commit
- Steps to reproduce
- Impact (token leak, path traversal, RCE, DoS, etc.)
- Any suggested fix

You should receive an acknowledgement within a few days when possible.

## Hard rules for operators

| Never commit | Why |
|--------------|-----|
| `.env` | Contains `BOT_TOKEN` |
| `cookies.txt` | Session hijacking risk |
| `data/history.json` | User privacy |
| Real tokens in issues/PRs | Instant bot takeover |

If a token leaks:

1. Revoke it in [@BotFather](https://t.me/BotFather) immediately  
2. Generate a new token  
3. Update `.env` and restart  

## Safe defaults

- Rate limiting per user  
- Single-instance lock (avoids split-brain polling)  
- Temp file cleanup  
- No token logging  

## Scope notes

This bot downloads **public** (or cookie-authenticated) media via [yt-dlp](https://github.com/yt-dlp/yt-dlp). Operators are responsible for:

- Compliance with Telegram Bot API rules  
- Site Terms of Service and copyright law in their jurisdiction  
- Not using the bot for unauthorized access to private content  
