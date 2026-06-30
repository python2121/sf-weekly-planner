import calendar
import json
import os
import re
import subprocess
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import markdown
from dateutil import parser as dateutil_parser
from flask import Flask, abort, jsonify, redirect, render_template, url_for

EVENTS_DIR = Path(os.environ.get("EVENTS_DIR", "/work/events"))
WORK_DIR = Path(os.environ.get("WORK_DIR", "/work"))
RUN_TIME = os.environ.get("RUN_TIME", "02:00")
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


def _trunc(s, n=80):
    s = str(s).replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


def _summarize_tool_input(name: str, inp: dict) -> str:
    if name == "WebFetch":
        return _trunc(inp.get("url", ""))
    if name == "WebSearch":
        return _trunc(inp.get("query", ""))
    if name in ("Read", "Write", "Edit"):
        return _trunc(inp.get("file_path", ""))
    if name == "Bash":
        return _trunc(inp.get("command", ""), 120)
    if name == "Task":
        return _trunc(inp.get("description") or inp.get("prompt", ""), 100)
    if name in ("Grep", "Glob"):
        return _trunc(inp.get("pattern", ""))
    try:
        return _trunc(json.dumps(inp), 100)
    except Exception:
        return "…"


def _format_stream_event(raw: str) -> str | None:
    """Pretty-print one NDJSON event from `claude --output-format stream-json`."""
    try:
        ev = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    t = ev.get("type")
    if t == "assistant":
        content = ev.get("message", {}).get("content", []) or []
        out_lines = []
        for item in content:
            kind = item.get("type")
            if kind == "text":
                text = (item.get("text") or "").strip()
                if text:
                    out_lines.append(f"  💬 {_trunc(text, 300)}")
            elif kind == "tool_use":
                name = item.get("name", "?")
                inp = item.get("input") or {}
                out_lines.append(f"  → {name}: {_summarize_tool_input(name, inp)}")
        return "\n".join(out_lines) if out_lines else None
    if t == "result":
        sub = ev.get("subtype", "")
        cost = ev.get("total_cost_usd", 0) or 0
        ms = ev.get("duration_ms", 0) or 0
        return f"  ✓ {sub} (${cost:.3f}, {ms/1000:.1f}s)"
    # system init, user (tool results), etc. — skip
    return None

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
                proc = subprocess.Popen(
                    [
                        "claude",
                        "-p",
                        arg,
                        "--verbose",
                        "--output-format",
                        "stream-json",
                        "--dangerously-skip-permissions",
                    ],
                    cwd=str(WORK_DIR),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                ring: list[str] = []
                assert proc.stdout is not None
                for raw in proc.stdout:
                    raw = raw.rstrip("\n")
                    if not raw:
                        continue
                    ring.append(raw)
                    if len(ring) > 500:
                        ring.pop(0)
                    formatted = _format_stream_event(raw)
                    if formatted:
                        print(formatted, flush=True)
                proc.wait()
                exit_code = proc.returncode
                combined = "\n".join(ring)
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
        print(f"{_ts()} invalid RUN_TIME={RUN_TIME!r}, defaulting to 02:00", flush=True)
        return 2, 0


MONDAY = 0


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
        days_ahead = (MONDAY - now.weekday()) % 7
        if days_ahead == 0 and target <= now:
            days_ahead = 7
        target += timedelta(days=days_ahead)
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


BM_ADJACENT_RE = re.compile(r"\s*BM[- ]?adjacent[.,]?", re.IGNORECASE)


def _strip_bm_adjacent(text: str) -> str:
    return BM_ADJACENT_RE.sub("", text)


def render_md(path: Path) -> str | None:
    if not path.exists():
        return None
    return markdown.markdown(_strip_bm_adjacent(path.read_text()), extensions=MD_EXTENSIONS)


def md_payload(path: Path) -> dict | None:
    if not path.exists():
        return None
    text = path.read_text()
    return {
        "markdown": text,
        "html": markdown.markdown(_strip_bm_adjacent(text), extensions=MD_EXTENSIONS),
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


# ---- Event-date extraction (for the "events calendar" view) ----

MONTH_DATE_RE = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?"
    r"|Aug(?:ust)?|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
    r"\s+\d{1,2}(?:st|nd|rd|th)?(?:[,\s]+\d{4})?\b",
    re.IGNORECASE,
)
NUMERIC_DATE_RE = re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b")
LIST_ITEM_START = re.compile(r"^- ")


def _parse_event_date(entry_text: str, source_date: date) -> date | None:
    """Best-effort: extract the first plausible event date from a list-item block.
    Returns None when nothing parseable falls inside a sane window relative to
    the file's source date."""
    default = datetime(source_date.year, source_date.month, source_date.day)
    earliest = source_date - timedelta(days=7)
    latest = source_date + timedelta(days=180)
    for pattern in (MONTH_DATE_RE, NUMERIC_DATE_RE):
        for m in pattern.finditer(entry_text):
            try:
                parsed = dateutil_parser.parse(m.group(0), default=default)
            except (ValueError, OverflowError, TypeError):
                continue
            d = parsed.date()
            # Year rollover: a "Jan 5" parsed against a December source likely means next year.
            if d < source_date - timedelta(days=30):
                try:
                    d = d.replace(year=d.year + 1)
                except ValueError:
                    continue
            if earliest <= d <= latest:
                return d
    return None


NORMALIZE_TITLE_RE = re.compile(r"[^\w\s]")
COLLAPSE_WS_RE = re.compile(r"\s+")
BM_ADJACENT_DETECT_RE = re.compile(r"🔥|BM[- ]?adjacent", re.IGNORECASE)

# Strip these as a prefix so "SFMOMA: Show X" and "Show X" collapse.
TITLE_VENUE_PREFIX_RE = re.compile(
    r"^(?:sfmoma|moad|cjm|ybca|de\s+young|legion\s+of\s+honor|"
    r"asian\s+art\s+museum|contemporary\s+jewish\s+museum|"
    r"yerba\s+buena(?:\s+center(?:\s+for\s+the\s+arts)?)?|"
    r"sf\s+opera|sf\s+ballet|war\s+memorial\s+opera\s+house|"
    r"herbst|davies\s+symphony\s+hall|the\s+fillmore|the\s+chapel|"
    r"public\s+works|the\s+midway|great\s+northern|halcyon|"
    r"1015\s+folsom|august\s+hall|bimbo'?s|audio|dna\s+lounge|"
    r"bottom\s+of\s+the\s+hill|brick\s*(?:&|and)?\s*mortar|"
    r"the\s+independent|cafe\s+du\s+nord|great\s+american\s+music\s+hall|"
    r"regency\s+ballroom|bill\s+graham\s+civic|chase\s+center|"
    r"svn\s+west|sf\s+mint|club\s+550)"
    r"\s*[:\-—–]\s*",
    re.IGNORECASE,
)
TITLE_PAREN_RE = re.compile(r"\([^)]*\)|\[[^\]]*\]")
# "Title — Subtitle" / "Title – Subtitle" — drop the subtitle. Not regular hyphen,
# since that would shred hyphenated names ("post-punk", "K-Hand").
TITLE_EM_DASH_TRAIL_RE = re.compile(r"\s+[—–]\s+.*$")
TITLE_JOINER_RE = re.compile(
    r"\s+(?:w/|with|feat\.?|ft\.?|vs\.?|x|and|&|\+|b2b)\s+",
    re.IGNORECASE,
)
TITLE_STOPWORDS = frozenset({
    "the", "a", "an", "of", "for", "at", "in", "on", "to", "presents",
})


def _title_tokens(title: str) -> frozenset[str]:
    """Tokenize for fuzzy dedup. Strips 🔥/BM-adjacent markers, venue prefixes,
    parenthetical noise, trailing em-dash subtitles, joiners (w/, +, &, b2b, …),
    punctuation, and short stopwords. Returns empty when the title is just
    marker noise — caller should drop those entries."""
    t = BM_ADJACENT_DETECT_RE.sub(" ", title)
    t = COLLAPSE_WS_RE.sub(" ", t).strip()
    t = TITLE_VENUE_PREFIX_RE.sub("", t)
    t = TITLE_PAREN_RE.sub(" ", t)
    t = TITLE_EM_DASH_TRAIL_RE.sub("", t)
    t = TITLE_JOINER_RE.sub(" ", t)
    t = NORMALIZE_TITLE_RE.sub(" ", t)
    return frozenset(
        w for w in t.lower().split()
        if len(w) > 1 and w not in TITLE_STOPWORDS
    )


def _is_title_dup(a: frozenset[str], b: frozenset[str]) -> bool:
    """Either side is a subset of the other, or Jaccard >= 0.5."""
    if not a or not b:
        return False
    inter = len(a & b)
    if inter == 0:
        return False
    if inter == len(a) or inter == len(b):
        return True
    return inter / len(a | b) >= 0.5


def _extract_list_blocks(text: str) -> list[str]:
    """Split a markdown body into list-item blocks. A block runs from a `- ` line
    through its indented continuations, ending on a blank line or a new `- `."""
    blocks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if LIST_ITEM_START.match(line):
            if current:
                blocks.append("\n".join(current))
            current = [line]
        elif current and (line.startswith("  ") or line.startswith("\t")):
            current.append(line)
        elif not line.strip():
            if current:
                blocks.append("\n".join(current))
                current = []
        else:
            if current:
                blocks.append("\n".join(current))
                current = []
    if current:
        blocks.append("\n".join(current))
    return blocks


def collect_events_by_date() -> dict[date, list[dict]]:
    """Walk every daily file, parse out event-date candidates, dedupe across files.
    Horizon is intentionally excluded — it's an open-ended outlook, not a per-day list.

    Dedup is per-date with fuzzy token-set matching (see `_is_title_dup`), so
    variants like "🔥 Jinjer", "JINJER", and "🔥 🔥 BM-adjacent Jinjer w/ Crystal
    Lake, Entheos" collapse to one entry. When two entries merge, the longer
    original title wins — terser repeats from a later digest don't overwrite a
    more informative one. Marker-only entries (e.g. a stray "🔥 BM-adjacent"
    section header that got parsed as a list item) tokenize to nothing and are
    dropped."""
    if not EVENTS_DIR.exists():
        return {}
    by_date: dict[date, list[dict]] = {}
    for p in sorted(EVENTS_DIR.iterdir()):
        m = DATE_RE.match(p.name)
        if not m:
            continue
        source_date = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        kind = m.group(4)
        try:
            text = p.read_text()
        except OSError:
            continue
        for block in _extract_list_blocks(text):
            title_m = TITLE_RE.search(block)
            if not title_m:
                continue
            raw_title = title_m.group(1).strip()
            tokens = _title_tokens(raw_title)
            if not tokens:
                continue
            event_date = _parse_event_date(block, source_date)
            if not event_date:
                continue
            url_m = URL_RE.search(block)
            # Strip 🔥/BM-adjacent markers from the display title — the upcoming-list
            # template already prepends a single 🔥 flag based on `bm_adjacent`, so
            # leaving the markers in produces triple-fire titles when the longer
            # "🔥 🔥 BM-adjacent X" variant wins the dedup.
            title = COLLAPSE_WS_RE.sub(" ", BM_ADJACENT_DETECT_RE.sub(" ", raw_title)).strip()
            entry = {
                "title": title,
                "kind": kind,
                "event_date": event_date,
                "source_date": source_date,
                "block": block,
                "url": url_m.group(0) if url_m else None,
                "bm_adjacent": bool(BM_ADJACENT_DETECT_RE.search(block)),
                "_tokens": tokens,
            }
            bucket = by_date.setdefault(event_date, [])
            for i, kept in enumerate(bucket):
                if _is_title_dup(tokens, kept["_tokens"]):
                    if len(title) > len(kept["title"]):
                        bucket[i] = entry
                    break
            else:
                bucket.append(entry)

    for entries in by_date.values():
        for e in entries:
            e.pop("_tokens", None)
    return by_date


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


@app.route("/events-calendar")
def events_calendar_today():
    today = date.today()
    return redirect(url_for("events_calendar_view", year=today.year, month=today.month))


@app.route("/events-calendar/<int:year>/<int:month>")
def events_calendar_view(year: int, month: int):
    if not (1 <= month <= 12):
        abort(404)
    events_by_date = collect_events_by_date()
    cal = calendar.Calendar(firstweekday=6)
    weeks = cal.monthdatescalendar(year, month)
    counts = {d: len(events_by_date.get(d, [])) for week in weeks for d in week}

    today = date.today()
    upcoming: list[dict] = []
    for d in sorted(events_by_date.keys()):
        if d < today:
            continue
        for e in events_by_date[d]:
            upcoming.append(e)

    prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    return render_template(
        "events_calendar.html",
        year=year,
        month=month,
        month_name=calendar.month_name[month],
        weeks=weeks,
        counts=counts,
        upcoming=upcoming,
        prev_month=prev_month,
        next_month=next_month,
        today=today,
    )


@app.route("/events-day/<date_str>")
def events_day(date_str: str):
    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        abort(404)
    events_by_date = collect_events_by_date()
    events = events_by_date.get(d, [])
    for e in events:
        e["html"] = markdown.markdown(_strip_bm_adjacent(e["block"]), extensions=MD_EXTENSIONS)

    sorted_dates = sorted(events_by_date.keys())
    prior = [x for x in sorted_dates if x < d]
    later = [x for x in sorted_dates if x > d]
    return render_template(
        "events_day.html",
        events=events,
        day_date=d,
        prev_event_date=prior[-1] if prior else None,
        next_event_date=later[0] if later else None,
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
