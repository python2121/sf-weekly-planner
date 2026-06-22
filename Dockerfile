FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git gnupg tini tzdata \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Non-root user — Claude Code refuses --dangerously-skip-permissions when run as root.
# The runtime UID/GID can be overridden via docker-compose `user:` to match the
# owner of the host events volume.
RUN useradd -m -u 1000 -s /bin/bash app

WORKDIR /app

COPY web/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY .claude /work/.claude
COPY web/app.py /app/app.py
COPY web/static /app/static
COPY web/templates /app/templates

RUN mkdir -p /work/events \
    && chown -R 1000:1000 /work /app \
    && chmod -R a+rX /work /app

ENV EVENTS_DIR=/work/events
ENV WORK_DIR=/work
ENV SF_HEADLESS=1

USER 1000:1000

EXPOSE 5000

ENTRYPOINT ["tini", "--"]
CMD ["python", "-u", "app.py"]
