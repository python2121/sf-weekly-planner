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

WORKDIR /app

COPY web/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY .claude /work/.claude
COPY web/app.py /app/app.py
COPY web/static /app/static
COPY web/templates /app/templates

ENV EVENTS_DIR=/work/events
ENV WORK_DIR=/work
ENV SF_HEADLESS=1

EXPOSE 5000

ENTRYPOINT ["tini", "--"]
CMD ["python", "-u", "app.py"]
