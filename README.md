# SF Weekly Planner

Containerized Claude Code that generates daily San Francisco event digests, plus a small Flask UI and JSON API to browse and trigger them.

## What it does

A `/sf-daily` Claude Code slash command generates two markdown files per day — `YYYY-MM-DD-music.md` and `YYYY-MM-DD-general.md` — covering the next 14 days, plus a `horizon.md` covering 14 days through 6 months out. A web UI displays them, lets you flip through dates, shows a calendar, and surfaces "tickets coming due" from horizon entries marked with `Action by:` dates.

## Architecture

- `runner/` — Docker container that owns Claude Code. A small Node process serves an internal HTTP endpoint (`/run`, `/status`) and schedules `/sf-daily` once per day at `RUN_TIME`. Runs `/sf-daily` once on startup too (idempotent — skips if today's files exist).
- `web/` — Flask app on port `7878` (host). Reads the event volume read-only. Hosts the browser UI and the JSON API.
- `.claude/commands/sf-daily.md` — the generation prompt itself. Edits here are picked up on the next `docker compose build`.
- Both containers bind-mount the same host directory (the value of `EVENTS_VOLUME` in `.env`) — runner writes to it, web reads from it.

## Deploy

```bash
git clone <repo>
cd sf-weekly-planner
cp .env.example .env
# edit .env — see "Configuration" below
docker compose up -d --build
```

Then visit `http://<host>:7878`.

### Configuration (`.env`)

| Var | Notes |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | Long-lived token from `claude setup-token` on your laptop. Uses your Pro/Max subscription. Expires after one year — set a calendar reminder. |
| `EVENTS_VOLUME` | Absolute host path where event markdown lives. Both containers bind-mount it. Path can contain spaces — do not quote in `.env`. |
| `RUN_TIME` | Daily run time in the runner's timezone, `HH:MM` 24h. Default `10:30`. |
| `TZ` | IANA timezone for the runner schedule and the UI. Default `America/Los_Angeles`. |
| `WEB_PORT` | Host port for the UI + API. Default `7878`. |

## Manually triggering a run

- From the UI: the `↻` button in the top-right.
- From the API: `curl -X POST http://<host>:7878/api/refresh`.
- From the host: `docker compose exec runner sh -c 'cd /work && claude -p "/sf-daily --force" --dangerously-skip-permissions'`.

The runner has a single in-process mutex, so concurrent triggers return `409 busy` rather than spawning duplicate runs.

## API

Full spec in [`openapi.yaml`](./openapi.yaml). Quick check:

```bash
curl http://<host>:7878/api          # list endpoints
curl http://<host>:7878/api/status   # runner + event store health
curl http://<host>:7878/api/day/$(date +%F)
```

No auth — designed for trusted home/LAN deployment. Put a reverse proxy in front if you expose it beyond that.

## Logs

```bash
docker compose logs -f runner   # runner schedule + claude output
docker compose logs -f web      # Flask access log
```
