---
description: Generate SF events digests (music + general) and refresh the 6-month horizon file. Idempotent — skips if today's files exist unless --force is passed.
---

# /sf-daily

Generate San Francisco event digests. Arguments: $ARGUMENTS

## Output location

All files live in the `events/` directory relative to the project root (the current working directory when this command runs). On the laptop that resolves to `/Users/andrewnowicki/Documents/code/sf-weekly-planner/events/`. In the containerized runner that directory is a bind-mount onto the host's events volume.

## Step 1 — Idempotency check

1. Compute today's date in local time as `YYYY-MM-DD`.
2. Check the events dir for both `YYYY-MM-DD-music.md` and `YYYY-MM-DD-general.md`.
3. If both exist AND `--force` is NOT in `$ARGUMENTS`: print one line ("Already generated for <date>, skipping.") and stop. Do NOT touch `horizon.md`. Do NOT do the self-heal step.
4. If `--force` is passed, regenerate today's files regardless.

## Step 2 — Backfill (only if doing real work today)

Look at the previous 2 dates. For any date where BOTH files are missing, generate that date's files too. Cap strictly at 2 backfill days. Skip dates where even one file already exists.

## Step 3 — Generate the music file (`YYYY-MM-DD-music.md`)

**Sources (parallel WebFetch):**
- `https://19hz.info/eventlisting_BayArea.php` (electronic / dance)
- `http://www.foopee.com/punk/the-list/` (punk / hardcore / indie / metal / post-punk)

**Window:** next 14 days from the file's date.

**Include (both sources):**
- San Francisco proper only — drop Oakland, Berkeley, San Jose, peninsula, north bay.
- Bigger / better-known acts.
- Larger venues. Allowlist (not exhaustive): The Midway, Public Works, 1015 Folsom, Great Northern, Halcyon, August Hall, Bimbo's, Audio, DNA Lounge, Bottom of the Hill, Brick & Mortar, The Chapel, The Independent, Cafe du Nord, Great American Music Hall, The Fillmore, Regency Ballroom, Bill Graham Civic, Chase Center, Svn West, SF Mint, Club 550. The Midway plus Midway-affiliated offsite / warehouse events are explicit allowlist.
- **Burning Man–adjacent acts are a strong priority include.** Mayan Warrior, Robot Heart, Tale Of Us / Afterlife, Damian Lazarus / Crosstown Rebels, Bedouin, Mind Against, Black Coffee, Carl Cox, Dixon, Honey Dijon, Avalon Emerson, Pan-Pot, Todd Terje, Goom Gum, Roddy Lima/Weska, similar. Flag with `🔥 BM-adjacent` at the start of the entry.

**Exclude (both sources):**
- Jazz of any flavor.
- Small bars: F8, Knockout, Make-Out Room, Madrone, Bar Part Time, El Rio, Cat Club, The Stud (when small show), Kilowatt, Mr. Mahjong's, Mothership, Hedge Coffee, Phonobar, Bric-a-Brac, etc.
- Anything described as casual, lounge, happy-hour, after-work, free-entry low-key.
- Anything outside SF proper.

For borderline calls on artist notability, spawn parallel background subagents (general-purpose) with a tight WebSearch brief ("is <artist> a notable touring act in 2026") and decide based on results. Fan out, don't serialize.

**Output structure — music file gets three sections:**

```
## 🔥 BM-adjacent
<ranked list>

## Electronic / dance — large venue
<ranked list>

## Punk / hardcore / indie / metal — large venue
<ranked list, sourced from foopee>
```

**Per-event format:**
```
- **<Artist / event title>** — <Venue>, <Date> <Time>
  <ticket link>
  Why: <one-line notability anchor>
```

**Top of file:** one-sentence summary (count + window).
**Bottom of file:** any sources that failed to fetch.

## Step 4 — Generate the general file (`YYYY-MM-DD-general.md`)

**Sources (parallel WebFetch):**
- `https://sf.funcheap.com/`
- `https://dothebay.com/`
- `https://www.7x7.com/`
- `https://www.eventbrite.com/d/ca--san-francisco/events/`

Plus parallel background general-purpose subagents for WebSearch:
- "san francisco notable restaurant opening <current month> <current year>"
- "san francisco museum new exhibit <current month> <current year>"
- "burning man bay area event <current month> <current year>" and "san francisco playa-adjacent event <current year>"

**Window:** next 14 days.

**Include:**
- Notable restaurant openings (chef-driven, distinctive concept).
- Significant museum exhibits and openings (SFMOMA, Asian Art Museum, de Young, Legion of Honor, Contemporary Jewish Museum, YBCA, MoAD).
- Interesting workshops and classes (ceramics, craft, art-making, unusual skills).
- Distinctive cultural happenings, talks by notable people, film series, design/architecture.
- **Burning Man–adjacent events of any kind — strong priority include.** Same `🔥 BM-adjacent` tag.
- **Especially notable opera, ballet, and galas** at SF Opera, SF Ballet, War Memorial Opera House, Herbst, Davies Symphony Hall, the museums, etc. Bar is high: include premieres, season openings, notable guest choreographers/conductors/singers, major fundraising galas with a real cultural draw, anniversary or signature productions. Default off; flip on only when the event is distinctive.

**Exclude:**
- Older-crowd skew by default: routine formal galas, classical chamber recitals, wine-country tours, traditional symphony — UNLESS the event meets the high bar above.
- Jazz.
- Generic networking, chamber-of-commerce, professional mixers.
- Kids / family-only events.
- Anything outside SF proper.

**Per-event format:** same as music.
**Ranking:** BM-adjacent first, then unusual / distinctive, then notable.

## Step 5 — Refresh horizon file (`horizon.md`)

Only runs when Step 3 or Step 4 actually generated something. Overwrite (do not append).

**Window:** 14 days out through 6 months out from today.

**Heavy hitters only**, across both categories:
- Major tour announcements stopping in SF.
- BM-adjacent acts coming to town.
- Major museum exhibits opening.
- Big cultural one-offs worth grabbing tickets for early.

Use WebSearch heavily. Target official venue calendars (Midway, Public Works, 1015, Great Northern, Halcyon, DNA Lounge, The Independent, The Chapel, The Fillmore, Bill Graham Civic, Chase Center, SFMOMA, Asian Art Museum, de Young, Legion of Honor) and reputable announcements. Fan out parallel general-purpose subagents per venue or category.

**Format:** group by month. Compact entries, ranked within month by interestingness.

**Per-entry shape (same as daily files, plus an optional 4th line):**
```
- **<title>** — <venue>, <date>
  <ticket link>
  Why: <one-line notability anchor>
  Action by: YYYY-MM-DD — <one-line reason>
```

**Action by — when to add it (and when not to):**

Add an `Action by:` line ONLY when the entry has a real, time-bound buying action. Examples that warrant it:
- Tickets are not yet on sale and the on-sale date is known.
- Presale window opens/closes on a specific date.
- Early-bird pricing has a hard deadline.
- The act + venue combination historically sells out within hours/days of going on sale (BM-adjacent headliners at mid-size venues, popular tours at Bill Graham Civic / Fillmore / The Independent, etc.).

Skip the line when the entry is open-ended (museum exhibits running for months, casual buy-anytime shows, festivals with broad availability). Better silent than guessed — if the on-sale date isn't findable via WebFetch / WebSearch, omit the line.

**Action by date semantics:**
- Must be a future date (today or later). If it has passed, either drop the line or drop the entry depending on whether tickets are still realistically gettable.
- The reason text is a single short clause: "on sale Fri 10am PT, sells out in hours", "presale ends 7/15", "early bird through 8/1", etc.

The web UI parses these lines and surfaces entries whose `Action by:` falls in the next 30 days — so this line is what makes a horizon entry actionable rather than informational.

**Top of file:** date last refreshed + window covered.

## Step 6 — Self-heal cron

If the environment variable `SF_HEADLESS` is set, skip this entire step — the containerized runner schedules itself via the host OS, and the Claude-internal cron tools are not available.

After successful generation only (skip on no-op exits):

1. Call CronList.
2. Look for two recurring jobs with prompt `/sf-daily` at cron expressions `13 10 * * *` and `13 17 * * *`.
3. For any that are missing OR more than 5 days old (recurring jobs auto-expire after 7 days): delete if present, then CronCreate with:
   - `cron`: the missing expression
   - `prompt`: `/sf-daily`
   - `recurring`: true
   - `durable`: true

The cron entries are project-scoped (persist in this project's `.claude/scheduled_tasks.json`) — they only fire while Claude is open in this directory. That's intentional.

## Failure handling

- If a WebFetch source blocks or fails (foopee is HTTP-only and may not respond well to HTTPS upgrade), note it inline at the bottom of the relevant file ("Source X unavailable") and have subagents fall back to WebSearch for that source's content.
- Never fail the whole run because one source is down.
- If both 19hz AND foopee fail, still write the music file with a clear notice and any events surfaced via WebSearch fallback.

## Output discipline

- Plain markdown, no tables.
- Each event entry ~3 lines max.
- No filler prose between entries.
- The file IS the output — don't summarize what you did afterward.
