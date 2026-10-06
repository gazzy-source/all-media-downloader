# Optional cookie handling

Cookies may help with sources that require an authenticated session. They are credentials: use a dedicated account where appropriate, restrict file permissions, keep them out of Git and logs, and revoke/rotate them if exposed. Cookie validity and site access rules can change independently of this project.

Set `COOKIES_FILE` in the private `.env` to the source jar path, or place a `cookies.txt` beside the application as supported by current configuration. Upload the Netscape-format jar over a secure channel and restrict it to the service operator. Never paste tokens, cookie rows, or proxy credentials into issues or documentation.

In v1, the application preserves the uploaded source jar, creates a sanitized copy at `data/cookies.sanitized.txt`, and makes per-job disposable copies named `data/cookies.job_*.txt` for yt-dlp. The bot cleans up those job copies. Do not delete the sanitized/source data as a routine restart step. Follow [Operations](OPERATIONS.md) for production update and restart procedures.

If authenticated extraction fails, first verify the source jar is current using a secure, local test copy. Do not update yt-dlp or restart production solely as an automatic response; review the change and use the normal tested deployment process in [Operations](OPERATIONS.md).
