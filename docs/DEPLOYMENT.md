# Local and Docker setup

For local development, create a Python 3.11+ virtual environment, install `requirements.txt`, copy `.env.example` to `.env`, configure `BOT_TOKEN`, and run `python run.py`. See the [README](../README.md#local-setup) for platform-specific commands.

The repository's `docker-compose.yml` provides a containerized bot and supporting service setup for self-hosting. Configure `.env` first, then use:

```bash
docker compose up --build -d
docker compose logs -f bot
```

Persist the `data/` and `temp/` mounts and keep `.env`, tokens and cookies private. This Compose topology is not the same as the current production VM: there the bot runs as a host systemd service, while Kuma and bgutil run in containers. Do not use this quickstart as a production change procedure; use [Operations](OPERATIONS.md).

Telegram's bot upload limit is why the application defaults to a final file-size cap below 50 MB. See [Architecture](ARCHITECTURE.md) for current behavior and constraints.
