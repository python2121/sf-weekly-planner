import calendar
import os
import re
import subprocess
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import markdown
from flask import Flask, abort, jsonify, redirect, render_template, url_for

EVENTS_DIR = Path(os.environ.get("EVENTS_DIR", "/work/events"))
WORK_DIR = Path(os.environ.get("WORK_DIR", "/work"))
RUN_TIME = os.environ.get("RUN_TIME", "10:30")
URGENT_WINDOW_DAYS = int(os.environ.get("URGENT_WINDOW_DAYS", "30"))
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-(music|general)\.md$")
ACTION_RE = re.compile(
    r"^\s*Action by:\s*(\d{4}-\d{2}-\d{2})(?:\s*[—\-]\s*(.+))?\s*$"
)
TITLE_RE = re.compile(r"\*\*([^*]+)\*\*")
URL_RE = re.compile(r"https?://[^\s)>\]]+")
MD_EXTENSIONS = ["fenced_code", "tables", "sane_lists", "nl2br"]

AUTH_ERROR_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"401\s+unauthorized",
        r"invalid[^.\n]{0,40}(oauth|token|credentials|api[- ]?key)",
        r"(token|credentials|session)[^.\n]{0,40}(expired|invalid|revoked)",
        r"authentication\s+(failed|error)",
        r"please\s+(log\s+in|login|re-?authenticate)",
        r"not\s+authenticated",
        r"CLAUDE_CODE_OAUTH_TOKEN[^.\n]{0,40}(invalid|expired|missing)",
    ]
]


def _detect_auth_failure(text: str) -> bool:
    return any(p.search(text) for p in AUTH_ERROR_PATTERNS)

app = Flask(__name__)


def _ts() -> str:
    return f"[{datetime.now().isoformat(timespec='seconds')}]"


class Runner:
    """Owns the claude subprocess lifecycle. Single in-process mutex."""

    def __init__(self):
        self._lock = threading.Lock()
        self._running = False
        self._last_run: dict | None = None

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def last_run(self) -> dict | None:
        with self._lock:
            return dict(self._last_run) if self._last_run else None

    def try_start(self, force: bool) -> bool:
        """Returns True if a run was started (in a background thread),
        False if a run is already in progress."""
        with self._lock:
            if self._running:
                return False
            self._running = True
        threading.Thread(target=self._run, args=(force,), daemon=True).start()
        return True

    def _run(self, force: bool):
        started_at = datetime.now()
        exit_code: int | None = None
        error: str | None = None
        auth_failed = False
        output_tail = ""
        try:
            arg = "/sf-daily --force" if force else "/sf-daily"
            print(f'{_ts()} spawn: claude -p "{arg}"', flush=True)
            try:
                result = subprocess.run(
                    ["claude", "-p", arg, "--dangerously-skip-permissions"],
                    cwd=str(WORK_DIR),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                exit_code = result.returncode
                if result.stdout:
                    print(result.stdout, flush=True)
                if result.stderr:
                    print(result.stderr, flush=True)
                combined = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
                output_tail = combined[-2000:]
                if exit_code != 0:
                    auth_failed = _detect_auth_failure(combined)
                print(f"{_ts()} claude exited code={exit_code}", flush=True)
            except FileNotFoundError:
                error = "claude binary not found on PATH"
                print(f"{_ts()} {error}", flush=True)
            except Exception as e:
                error = f"run error: {e}"
                print(f"{_ts()} {error}", flush=True)
        finally:
            with self._lock:
                self._last_run = {
                    "started_at": started_at,
                    "ended_at": datetime.now(),
                    "exit_code": exit_code,
                    "auth_failed": auth_failed,
                    "error": error,
                    "output_tail": output_tail,
                    "force": force,
                }
                self._running = False


runner = Runner()


def _parse_run_time() -> tuple[int, int]:
    try:
        h, m = RUN_TIME.split(":")
        return int(h), int(m)
    except ValueError:
        print(f"{_ts()} invalid RUN_TIME={RUN_TIME!r}, defaulting to 10:30", flush=True)
        return 10, 30


def schedule_loop():
    # Initial run on startup. Idempotent — sf-daily skips if today's files exist.
    runner.try_start(force=False)
    while True:
        # Wait for any in-flight run to finish before computing next.
        while runner.running:
            time.sleep(5)
        h, m = _parse_run_time()
        now = datetime.now()
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        sleep_s = (target - now).total_seconds()
        print(
            f"{_ts()} next scheduled run at {target.isoformat(timespec='seconds')} "
            f"(in {sleep_s:.0f}s)",
            flush=True,
        )
        time.sleep(sleep_s)
        runner.try_start(force=False)


def parse_iso_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        abort(404)


def available_dates() -> list[date]:
    if not EVENTS_DIR.exists():
        return []
    dates: set[date] = set()
    for p in EVENTS_DIR.iterdir():
        m = DATE_RE.match(p.name)
        if m:
            dates.add(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
    return sorted(dates)


def adjacent_date(d: date, direction: str) -> date | None:
    dates = available_dates()
    if direction == "prev":
        prior = [x for x in dates if x < d]
        return prior[-1] if prior else None
    later = [x for x in dates if x > d]
    return later[0] if later else None


def render_md(path: Path) -> str | None:
    if not path.exists():
        return None
    return markdown.markdown(path.read_text(), extensions=MD_EXTENSIONS)


def md_payload(path: Path) -> dict | None:
    if not path.exists():
        return None
    text = path.read_text()
    return {
        "markdown": text,
        "html": markdown.markdown(text, extensions=MD_EXTENSIONS),
    }


def serialize_urgent(item: dict) -> dict:
    return {
        "title": item["title"],
        "action_date": item["action_date"].isoformat(),
        "days_left": item["days_left"],
        "reason": item["reason"],
        "url": item["url"],
    }


def parse_horizon_actions() -> list[dict]:
    """Extract entries from horizon.md that have an `Action by:` line."""
    p = EVENTS_DIR / "horizon.md"
    if not p.exists():
        return []
    lines = p.read_text().splitlines()
    items: list[dict] = []
    for i, line in enumerate(lines):
        m = ACTION_RE.match(line)
        if not m:
            continue
        try:
            action_date = date.fromisoformat(m.group(1))
        except ValueError:
            continue
        reason = (m.group(2) or "").strip()

        item_start = i
        for j in range(i - 1, -1, -1):
            if lines[j].startswith("- "):
                item_start = j
                break
            if not lines[j].strip():
                break

        block = "\n".join(lines[item_start : i + 1])
        title_m = TITLE_RE.search(block)
        url_m = URL_RE.search(block)
        title = title_m.group(1).strip() if title_m else lines[item_start].lstrip("- ").strip()
        items.append(
            {
                "title": title,
                "action_date": action_date,
                "reason": reason,
                "url": url_m.group(0) if url_m else None,
            }
        )
    return items


def urgent_items(window_days: int = URGENT_WINDOW_DAYS) -> list[dict]:
    today = date.today()
    cutoff = today + timedelta(days=window_days)
    return sorted(
        [
            {**i, "days_left": (i["action_date"] - today).days}
            for i in parse_horizon_actions()
            if today <= i["action_date"] <= cutoff
        ],
        key=lambda x: x["action_date"],
    )


@app.context_processor
def inject_urgent():
    items = urgent_items()
    return {"urgent_count": len(items), "urgent_top": items[:4]}


@app.context_processor
def inject_banner():
    lr = runner.last_run
    if not lr:
        return {"banner": None}
    if lr["auth_failed"]:
        return {
            "banner": {
                "kind": "auth",
                "title": "Claude OAuth token expired or invalid",
                "detail": (
                    "Daily generation is failing. On your laptop run "
                    "`claude setup-token`, paste the new token into "
                    "CLAUDE_CODE_OAUTH_TOKEN in .env on the server, then "
                    "`docker compose up -d` to restart."
                ),
            }
        }
    if lr["error"] or (lr["exit_code"] not in (0, None)):
        return {
            "banner": {
                "kind": "error",
                "title": "Last generation run failed",
                "detail": (
                    lr["error"]
                    or f"claude exited with code {lr['exit_code']}. Check `docker compose logs app` for details."
                ),
            }
        }
    return {"banner": None}


@app.route("/")
def index():
    dates = available_dates()
    if dates:
        return redirect(url_for("day", date_str=dates[-1].isoformat()))
    today = date.today()
    return redirect(url_for("calendar_view", year=today.year, month=today.month))


@app.route("/day/<date_str>")
def day(date_str: str):
    d = parse_iso_date(date_str)
    music_html = render_md(EVENTS_DIR / f"{date_str}-music.md")
    general_html = render_md(EVENTS_DIR / f"{date_str}-general.md")
    return render_template(
        "day.html",
        day_date=d,
        music_html=music_html,
        general_html=general_html,
        prev_date=adjacent_date(d, "prev"),
        next_date=adjacent_date(d, "next"),
    )


@app.route("/calendar")
def calendar_today():
    today = date.today()
    return redirect(url_for("calendar_view", year=today.year, month=today.month))


@app.route("/calendar/<int:year>/<int:month>")
def calendar_view(year: int, month: int):
    if not (1 <= month <= 12):
        abort(404)
    cal = calendar.Calendar(firstweekday=6)
    weeks = cal.monthdatescalendar(year, month)
    dates_with_events = {
        d for d in available_dates() if d.year == year and d.month == month
    }
    prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    return render_template(
        "calendar.html",
        year=year,
        month=month,
        month_name=calendar.month_name[month],
        weeks=weeks,
        dates_with_events=dates_with_events,
        prev_month=prev_month,
        next_month=next_month,
        today=date.today(),
    )


@app.route("/horizon")
def horizon():
    html = render_md(EVENTS_DIR / "horizon.md")
    return render_template("horizon.html", html=html)


@app.route("/urgent")
def urgent():
    return render_template(
        "urgent.html",
        items=urgent_items(),
        window_days=URGENT_WINDOW_DAYS,
    )


@app.route("/api")
def api_index():
    return jsonify(
        {
            "endpoints": {
                "GET  /api/dates": "list all available event dates",
                "GET  /api/day/<YYYY-MM-DD>": "music + general markdown/html for a date",
                "GET  /api/calendar/<year>/<month>": "dates with events in a given month",
                "GET  /api/horizon": "horizon markdown/html + urgent items",
                "GET  /api/urgent": "items with upcoming Action by dates",
                "GET  /api/status": "runner status + event store summary",
                "POST /api/refresh": "force-regenerate today (returns 202/409)",
            }
        }
    )


@app.route("/api/dates")
def api_dates():
    dates = available_dates()
    return jsonify(
        {
            "dates": [d.isoformat() for d in dates],
            "count": len(dates),
            "latest": dates[-1].isoformat() if dates else None,
        }
    )


@app.route("/api/day/<date_str>")
def api_day(date_str: str):
    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        return jsonify({"error": "invalid date, expected YYYY-MM-DD"}), 400
    prev_d = adjacent_date(d, "prev")
    next_d = adjacent_date(d, "next")
    return jsonify(
        {
            "date": date_str,
            "music": md_payload(EVENTS_DIR / f"{date_str}-music.md"),
            "general": md_payload(EVENTS_DIR / f"{date_str}-general.md"),
            "prev": prev_d.isoformat() if prev_d else None,
            "next": next_d.isoformat() if next_d else None,
        }
    )


@app.route("/api/calendar/<int:year>/<int:month>")
def api_calendar(year: int, month: int):
    if not (1 <= month <= 12):
        return jsonify({"error": "month out of range"}), 400
    dates = [
        d.isoformat()
        for d in available_dates()
        if d.year == year and d.month == month
    ]
    return jsonify({"year": year, "month": month, "dates": dates, "count": len(dates)})


@app.route("/api/horizon")
def api_horizon():
    payload = md_payload(EVENTS_DIR / "horizon.md")
    return jsonify(
        {
            "horizon": payload,
            "urgent": [serialize_urgent(i) for i in urgent_items()],
        }
    )


@app.route("/api/urgent")
def api_urgent():
    items = urgent_items()
    return jsonify(
        {
            "items": [serialize_urgent(i) for i in items],
            "count": len(items),
            "window_days": URGENT_WINDOW_DAYS,
        }
    )


@app.route("/api/status")
def api_status():
    dates = available_dates()
    lr = runner.last_run
    last_run_payload = None
    if lr:
        last_run_payload = {
            "started_at": lr["started_at"].isoformat(timespec="seconds"),
            "ended_at": lr["ended_at"].isoformat(timespec="seconds"),
            "exit_code": lr["exit_code"],
            "auth_failed": lr["auth_failed"],
            "error": lr["error"],
            "force": lr["force"],
        }
    return jsonify(
        {
            "runner": {"running": runner.running, "last_run": last_run_payload},
            "events_dir": str(EVENTS_DIR),
            "events_dir_exists": EVENTS_DIR.exists(),
            "available_dates": len(dates),
            "latest_date": dates[-1].isoformat() if dates else None,
            "horizon_exists": (EVENTS_DIR / "horizon.md").exists(),
        }
    )


@app.route("/api/refresh", methods=["POST"])
def refresh():
    if runner.try_start(force=True):
        return jsonify({"status": "started"}), 202
    return jsonify({"status": "busy"}), 409


if __name__ == "__main__":
    threading.Thread(target=schedule_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, threaded=True)
