FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# POT_PROVIDER_URL=http://bgutil-provider:4416 (set in docker-compose.yml) is
# used by the bgutil yt-dlp plugin (in requirements.txt) to fetch PO tokens.
# The provider container itself ships Node.js — nothing else needed here.

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Unprivileged user: ffmpeg and the extractors parse hostile media and HTML,
# so a parser bug must not hand out root in the container.
RUN useradd --system --uid 10001 --home-dir /app bot     && mkdir -p downloads temp data     && chown -R bot:bot /app
USER bot

ENV PYTHONUNBUFFERED=1

CMD ["python", "run.py"]
