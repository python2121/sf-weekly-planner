# SF Weekly Planner

Containerized Claude Code that generates daily San Francisco event digests, plus a small Flask UI and JSON API to browse and trigger them.

## What it does

A `/sf-daily` Claude Code slash command generates two markdown files per day — `YYYY-MM-DD-music.md` and `YYYY-MM-DD-general.md` — covering the next 14 days, plus a `horizon.md` covering 14 days through 6 months out. A web UI displays them, lets you flip through dates, shows a calendar, and surfaces "tickets coming due" from horizon entries marked with `Action by:` dates.

## Architecture

Single container. The Flask app at `web/app.py` does three things:

1. Serves the browser UI and JSON API on host port `7878`.
2. Runs a background scheduler thread that fires `/sf-daily` once on startup (idempotent — skips if today's files exist) and again at `RUN_TIME` every Monday.
3. Spawns `claude` as a subprocess for each run. A single in-process mutex serializes scheduled and manual runs; `/api/refresh` returns `409 busy` if a run is already in flight.

The image bundles Python (Flask) and Node (only as a runtime for the `@anthropic-ai/claude-code` CLI — there is no long-running Node process). The events bind-mount lands at `/work/events` so the same path is writable by the spawned `claude` and readable by Flask.

- `Dockerfile` — image definition.
- `web/app.py` — Flask + scheduler + runner.
- `.claude/commands/sf-daily.md` — the generation prompt. Edits picked up on next `docker compose build`.

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
| `RUN_TIME` | Weekly run time (Monday) in the runner's timezone, `HH:MM` 24h. Default `02:00`. |
| `TZ` | IANA timezone for the runner schedule and the UI. Default `America/Los_Angeles`. |
| `WEB_PORT` | Host port for the UI + API. Default `7878`. |

## Manually triggering a run

- From the UI: the `↻` button in the top-right.
- From the API: `curl -X POST http://<host>:7878/api/refresh`.
- From the host: `docker compose exec app sh -c 'cd /work && claude -p "/sf-daily --force" --dangerously-skip-permissions'`.

A single in-process mutex serializes runs, so concurrent triggers return `409 busy` rather than spawning duplicate `claude` processes.

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
docker compose logs -f app   # scheduler, claude subprocess output, Flask access log
```
