FROM node:22-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl git tini tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

WORKDIR /work

COPY .claude /work/.claude
COPY runner/trigger-server.js /usr/local/bin/trigger-server.js

ENV SF_HEADLESS=1
EXPOSE 5001

ENTRYPOINT ["tini", "--"]
CMD ["node", "/usr/local/bin/trigger-server.js"]
