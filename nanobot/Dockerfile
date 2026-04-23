FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir nanobot-ai

# nanobot reads its config from /root/.nanobot/config.json.
# The real config is mounted by docker-compose at runtime (read-only).
RUN mkdir -p /root/.nanobot

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/health || exit 1

# We use nanobot's OpenAI-compatible gateway as the audit endpoint.
CMD ["nanobot", "gateway"]
