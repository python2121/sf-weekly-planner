import calendar
import os
import re
from datetime import date, timedelta
from pathlib import Path

import markdown
import requests
from flask import Flask, abort, jsonify, redirect, render_template, url_for

EVENTS_DIR = Path(os.environ.get("EVENTS_DIR", "/data/events"))
RUNNER_URL = os.environ.get("RUNNER_URL", "http://runner:5001")
URGENT_WINDOW_DAYS = int(os.environ.get("URGENT_WINDOW_DAYS", "30"))
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-(music|general)\.md$")
ACTION_RE = re.compile(
    r"^\s*Action by:\s*(\d{4}-\d{2}-\d{2})(?:\s*[—\-]\s*(.+))?\s*$"
)
TITLE_RE = re.compile(r"\*\*([^*]+)\*\*")
URL_RE = re.compile(r"https?://[^\s)>\]]+")
MD_EXTENSIONS = ["fenced_code", "tables", "sane_lists", "nl2br"]

app = Flask(__name__)


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

        # Walk back to the enclosing list item (line starting with "- ").
        item_start = i
        for j in range(i - 1, -1, -1):
            if lines[j].startswith("- "):
                item_start = j
                break
            if not lines[j].strip():
                break  # left the entry block without finding a bullet

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
    cal = calendar.Calendar(firstweekday=6)  # Sunday-start
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
    runner_status: dict | None = None
    runner_error: str | None = None
    try:
        r = requests.get(f"{RUNNER_URL}/status", timeout=3)
        if r.ok:
            runner_status = r.json()
        else:
            runner_error = f"runner returned {r.status_code}"
    except requests.RequestException as e:
        runner_error = str(e)
    return jsonify(
        {
            "runner": runner_status,
            "runner_error": runner_error,
            "events_dir": str(EVENTS_DIR),
            "events_dir_exists": EVENTS_DIR.exists(),
            "available_dates": len(dates),
            "latest_date": dates[-1].isoformat() if dates else None,
            "horizon_exists": (EVENTS_DIR / "horizon.md").exists(),
        }
    )


@app.route("/api/refresh", methods=["POST"])
def refresh():
    try:
        r = requests.post(f"{RUNNER_URL}/run", timeout=5)
    except requests.RequestException as e:
        return jsonify({"status": "unreachable", "error": str(e)}), 502
    if r.status_code == 202:
        return jsonify({"status": "started"}), 202
    if r.status_code == 409:
        return jsonify({"status": "busy"}), 409
    return jsonify({"status": "error", "code": r.status_code}), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
