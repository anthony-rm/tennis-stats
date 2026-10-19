# Tennis Betting Dashboard

A local web dashboard that, every time it starts:

1. scans **every tennis match listed on Winamax** in a real (headless) Chrome
   (all days; the table is **grouped by day** and today's matches are the
   selected lines by default — a scan that comes back empty never wipes the
   stored list or its odds: a blank result means the page did not render),
2. opens each match page and extracts **every betting market** (odds), including
   the `Nombre de jeux` (total games) and `Écart de jeux` (games spread) ladders,
3. fetches **Tennis Abstract player stats** for every player found,
4. shows one big table — **two rows per match** (top = first player, bottom =
   second) × **127 data columns** in 11 groups (A–L, without I), grouped by the
   official match day and showing kick-off times on **your own clock**, with a
   checkbox per cell (every Winamax odd line also carries a 🎾 **copy**
   button that puts the French *ANALYSE DU JOUR* template on the clipboard),
5. copies the selected cells to the clipboard as **LLM-friendly structured text**
   (default), TSV (Excel/Google Sheets), Markdown or JSON, in one click — or
   copies just the list of matches as `A vs B` lines.

Built per `PLAN.md`. Local, personal, low-rate tool: no login automation, no
bets placed, human-like delays, results never redistributed.

## Install on macOS (no terminal)

The app ships as a signed-locally `.app` bundle built by CI — no Python, no
`pip`, no terminal required.

1. Download **`TennisStats-<version>-arm64.dmg`** from
   [Releases](https://github.com/anthony-rm/tennis-stats/releases/latest).
2. Open the DMG and drag **Tennis Stats** onto the **Applications** folder shown
   beside it.
3. **First launch only:** in Finder, *right-click* (or Control-click)
   `Tennis Stats` → **Open** → **Open** again. macOS does this because the app
   is not notarized by Apple; every later launch is a normal double-click.
4. A 🎾 appears in the menu bar and the dashboard opens in your browser
   (<http://127.0.0.1:8000>).

Menu bar: **Open dashboard** · **Rescan now** · **Check for updates** ·
**Quit**. Nothing else to learn.

Requirements: **Google Chrome** (the scraper drives it) and internet access to
winamax.fr / tennisabstract.com. If Chrome is missing the app says so on first
launch and offers the download link.

Local state lives outside the app, so updates never touch it:
`~/Library/Application Support/TennisStats/` (`tennis_stats.db`, `cache/`,
`logs/launcher.log`).

### Updates

The app asks GitHub for the latest release at launch and every six hours. When
a newer version exists the menu entry turns into an installer —
**"Update to 1.0.3 available — click to install"** — and one click downloads the
matching archive, verifies it (published SHA256 checksum **and** the app's code
signature), replaces the bundle in `/Applications` and relaunches it. Open tabs
keep working; the DB and cache are untouched.

Every other answer of the update entry is explicit too: **"Checking for
updates…"** while the feed is read, **"✓ 1.0.2 is the latest version"** for five
seconds after a manual check that found nothing, and an alert with the reason
when the check or the install fails — a click never silently does nothing. The
swap is logged to `logs/update.log` next to `launcher.log`, and a failed copy
restores the previous app instead of leaving a half-replaced bundle.

To keep the friend's Mac and an Intel Mac both supported, each release publishes
one archive per architecture (`-arm64`, and `-x86_64` when that build is
enabled); the updater downloads the one matching the machine it runs on.

**Migration:** apps installed from **v1.0.1 or older** cannot self-update — that
release only changed the menu entry's title, so its click re-ran the check.
Replace such an install once by hand (download the DMG of v1.0.3 or newer);
every later update is one click again.

If an update never shows up, ask for the output of

```bash
"/Applications/Tennis Stats.app/Contents/MacOS/Tennis Stats" --check-updates
```

which prints the decision as JSON — `status` (`update` / `uptodate` /
`disabled` / `error`), the chosen archive and its checksum URL, or the error.
Adding `--update` runs the whole check + install + relaunch from the terminal.
The launcher log is at
`~/Library/Application Support/TennisStats/logs/launcher.log` (the swap log:
`logs/update.log` beside it).

Two environment hooks exist for tests: `TENNIS_STATS_RELEASE_API` points the
feed somewhere other than GitHub, `TS_SWAP_TRIES` shortens the script's 60 s
wait for the app to quit.

## Quick start (development)

```bash
cd ~/Projets/tennis_stats
python3 -m venv .venv                # already present in this checkout
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. The Winamax scan starts automatically on server
start; then press **Fetch odds** and **Fetch stats** (both run as background
jobs with live progress via SSE):

* **Fetch odds** — every match of the list that has no odds yet (the whole
  list, not only the selected lines; live matches skipped);
* **↻ Refresh odds** — re-fetch the odds of every match, *including the ones
  already shown*, because the odds move during the day;
* **Fetch stats** — every player of the list still missing Tennis Abstract
  data (already stored players are skipped, stored fetch errors are retried;
  three consecutive network failures stop the pass with an error instead of
  recording every remaining player as absent);
* **Clear data** — deletes every downloaded match / odd / player stat plus
  the cached player pages on disk (confirmed first, disabled while a job
  runs); the selection and copy format are kept, and the grid comes back
  with the next scan.

The grid fills in **live**: each stored match / player pushes a `data` event
on the SSE stream and the table reloads (throttled) while the job is still
running — no need to wait for the end of the pass. The version chip next to
the title shows the dashboard version.

**Selection = lines × columns (two filters):**

* the **line checkbox** of a match (and the **Select day** button of each day
  header, e.g. `TODAY (16/10/2026)`) decides which matches take part — today's
  lines are selected by default;
* the **Quick select** chips / column & group headers / cell checkboxes fill
  boxes **only for the selected lines** (untick a line and its boxes leave the
  copy without being lost);
* **⧉ Copy selection** copies the boxes of the selected lines; **⧉ Copy match
  list** copies those matches as `A vs B` lines (the fetch buttons are not
  line-scoped — they always cover the whole list, see above).

Requires Google Chrome installed (Playwright uses `channel="chrome"`, so no
browser download is needed) and network access to winamax.fr /
tennisabstract.com.

## How it works

```
[scan]   core/winamax.py  ─ headless Chrome, /paris-sportifs/sports/5
             │            injects core/winamax_core.js, scrolls the whole list
             │            filters: doubles out, live flagged (all days; the
             │            >6 h night-gap rule only if requested)
             ▼
[odds]   per match: click card → extractOddsJSON() → go_back()  (never deep-link:
         a full goto() of /match/<id> gets CloudFront 403 "Request blocked")
             ▼
[stats]  core/tennisabstract.py → jsfrags/<Id>.js + player page → 24 h cache
             ▼
[table]  app/columns.py computes all 127 columns → app/store.py (SQLite)
             ▼
[copy]   app/exporter.py → LLM / TSV / Markdown / Markdown-block / JSON → clipboard
```

### Layout

```
app/
  main.py        FastAPI: API, SSE stream, static page, auto-scan on start
  jobs.py        background jobs (scan / odds / stats) + progress events
  store.py       SQLite: matches, stats, selections (data/tennis_stats.db)
  columns.py     column catalogue (groups A–L) + value computation
  exporter.py    selection → LLM / TSV / Markdown / Markdown-block / JSON
core/
  winamax_core.js   ALL Winamax selectors + extraction (shared with userscript)
  winamax.py        Playwright driver (scan, click-through odds pass, WAF recovery)
  tennisabstract.py player id resolution + profile/tables fetch + cache
web/index.html   the dashboard (vanilla JS, no build step)
desktop/
  launcher.py       menu-bar app: uvicorn thread, browser, update entry, CLI
  updater.py        self-update: release feed, verification, swap, menu model
  TennisStats.spec  PyInstaller bundle definition
  build_dmg.sh      builds dist/ app + DMG (install) + ZIP (update)
  run_local.sh      launches dist/ app as a temporary instance (own port, no update)
  make_icon.py      generates the .icns at build time
Makefile            local build / run / deploy targets (make help)
tools/build_userscript.py   rebuilds ~/Projets/cotes/useful-tool/script.js
tests/           pytest suite + HTML/JSON fixtures (offline) + dashboard screenshot
```

## Building locally, testing, and deploying on demand

Nothing is ever published by accident: the laptop builds are fully local, and
GitHub is only touched by an explicit `make deploy`.

### 1. Offline build on this machine

```bash
make build-venv   # once, needs network: .venv-build (PyInstaller + PyObjC)
make build        # tests + app + DMG + ZIP + SHA256SUMS, all in dist/
```

After the one-time `build-venv`, `make build` needs **no network**: PyInstaller,
codesign and hdiutil are local. Artefacts land in `dist/` and are uploaded
nowhere.

### 2. Run the two (or three) versions side by side

| version | command | URL | state (SQLite) |
|---|---|---|---|
| published app (Applications) | `make open-installed` | `:8000` | `~/Library/Application Support/TennisStats` |
| local files, dev server | `make run` | `:8001` | `<repo>/data` |
| local files, frozen build | `make app-local` | `:8010` | `<repo>/data` |

All three can run at the same time — each binds its own port and the two local
ones share the repo's `data/`, so run only **one** local instance at a time.

* `make run` serves `app/` + `web/` straight from this folder and does **not**
  auto-scan (`AUTOSCAN=1 make run` restores that) — no second Winamax scrape
  next to the installed app. Restart it after editing Python files; the web UI
  reloads on refresh.
* `make app-local` launches `dist/Tennis Stats.app` with
  `TENNIS_STATS_PORT=8010`, `TENNIS_STATS_NO_UPDATE=1` (the temporary build
  never replaces itself from the GitHub release) and
  `TENNIS_STATS_NO_AUTOSCAN=1`. Log: `build/local-run.log`.
* The installed app is untouched: port 8000, its own state, updates enabled.
  Quit it from the menu bar when you're done comparing.

### 3. Deploy to GitHub — only when you choose to

```bash
# step 1 — local only: bump VERSION, commit, tag (nothing leaves the laptop)
make release VERSION=1.0.1
git show v1.0.1            # inspect, then:

# step 2 — THE deploy step: push HEAD + its tag → CI builds and publishes
make deploy
```

`.github/workflows/release.yml` runs only on a tag push (or a manual run) —
**pushes to `main` never build and never publish**. It runs the test suite,
builds `TennisStats-<version>-<arch>.dmg` (first install) and `.zip` (updater)
on a macOS runner, checks the tag matches `VERSION`, and attaches them to the
GitHub Release. Installed apps notice the new tag on their next launch (or
within six hours) and offer the one-click update.

To get a CI build **without** publishing: *Actions → release → Run workflow*
with the `publish` box unticked (`make ci-build` does the same via the `gh`
CLI) — the DMG/ZIP arrive as workflow artefacts instead of a release.

## The shared userscript core

`core/winamax_core.js` is the single source of truth for Winamax selectors.
It is consumed two ways:

* **dashboard**: injected into the Playwright page (`page.add_script_tag`);
* **Tampermonkey userscript**: baked into `~/Projets/cotes/useful-tool/script.js`
  by `tools/build_userscript.py` (the wrapper UI lives in
  `~/Projets/cotes/useful-tool/src/ui.js`).

```bash
.venv/bin/python tools/build_userscript.py          # rebuild script.js (v9.29)
.venv/bin/python tools/build_userscript.py --check  # CI/test: fail if stale
```

**Never edit `script.js` by hand** — it is generated (the pre-refactor v9.28 is
kept as `script.js.bak-v9.28`). Edit the core or `src/ui.js`, rebuild, and the
tests verify it parses (`node --check`) and is up to date.

## Column conventions (app/columns.py)

* **Hidden columns** (`Tournament`, `Round`, `Start time`, `Day`) are not drawn
  as grid columns — the row summary and the day header already show them — but
  they stay in the catalogue and in the copy (they are what the *Match info*
  chip selects).
* The grid shows **two rows per match** (top = first player, bottom = second):
  pair values are split instead of being joined with `P1 | P2`; the copy
  formats keep the `P1 | P2` convention for rectangular exports.
* `—` = unknown (stats not fetched, fetch failed, player unresolved, table
  absent on TA).
* **Time & day**: Winamax's cards hold **Europe/Paris** kick-offs and day
  sections (`demain`, `mar.`, …) — the official calendar of a match.
  `a.start` is converted to **the machine's time zone** for the row summary,
  the *Start time* copy field and sorting, while the day header keeps the
  **official match day** (`Today (19/10/2026)`), never the date in the
  viewer's zone: a 23:00 Paris match stays under its official day even in
  UTC+8. The official date is anchored to the Paris date of the scan that
  read the indicator (`scanned_at`), so stored rows keep the right day until
  the next scan.
* Group C averages over the last **N** matches (toolbar selector, default 10);
  `c.bps` is summed (`19/27`), `c.last_*` is the latest match.
* Group D uses the **inferred match surface**: the exact event row when the
  player already played this event (e.g. Shanghai → Hard), otherwise the
  surface of his latest event.
* Group F ratios/speeds are means over the table rows (mph for speeds); every
  mean over a Tennis Abstract log table **skips the trailing `Career (N
  matches)` total row** (it would otherwise drown out the per-match values).
* Group A carries the Winamax game markets from the stored odds pass:
  `a.games` (**Total games**) and `a.spread` (**Games spread**) show **one line
  per odd** — `Over 21.5: 1.44` / `Under 21.5: 1.92` (Over block first, blank
  line between the blocks) and `Zverev -6.5: 7.25` … (surname on every line,
  grouped per player, sorted by signed handicap). Every odd line gets a 🎾
  button that copies the fixed French template with
  `🎯 **<odd line> @<price>**` filled in (`Victoire <surname> @1.38`,
  `Over 21,5 jeux @1.44`, `Kartal -4,5 jeux @2.35` — French decimals).
  Values are newline-separated — in the grid both ladders are **folded to a
  `▸ N odds` summary by default and develop only while the cell is selected**
  (tick its box / click the cell; hover the summary to peek at the full list),
  while the copy formats flatten them. Only the exactly titled sections count
  (never `1er set - …` or `Nombre de jeux de <player>` variants).
* Group G (H2H) values come from **P1's** head-to-head table; `never` = both
  players are known and have not met.
* Tennis Abstract gaps are flagged in the row (`TA?` badge) — unresolved ids,
  players not fetched yet and stored fetch failures — instead of silently
  showing empty data.

### Export semantics

The first output column / line is always the match label (`P1 vs P2`) so a
pasted fragment keeps its rows identifiable. A column appears in the output iff
at least one of its cells is selected; a row appears iff at least one cell is
selected; unselected cells inside a kept row are empty in the rectangular
formats (TSV), keeping them pasteable into Excel.

**`llm` (default)** produces one block per match instead of a table:

```
## 1. Daniil Medvedev vs Tommy Paul
### A. Match (Winamax)
- Tournament: ATP - Shanghai
- Total games: Over 21.5: 1.54 Over 22.5: 1.78 Over 23.5: 1.98  Under 21.5: 2.00 Under 22.5: 1.78 Under 23.5: 1.58
### B. Player identity (Tennis Abstract)
- Current rank: Daniil Medvedev 6, Tommy Paul 18
```

Player pairs are written as `<player> <value>, <player> <value>` (no ambiguous
`a | b`), `—` marks missing data, and a
short reading guide opens the paste. The chosen format is remembered per
browser (`localStorage`), the LLM format is the initial default.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/version` | dashboard version (from the `VERSION` file) |
| GET | `/api/columns` | checkbox catalogue (groups A–L) |
| GET | `/api/matches?form_n=10` | rows + computed values + stats status (`form_n` defaults to 10) |
| POST | `/api/scan` | start list scan `{stop_at_night_gap=false, include_live}` |
| POST | `/api/odds` | start odds pass `{match_ids?, force?}` (default: all missing odds, live skipped; `force=true` re-fetches even stored odds) |
| POST | `/api/stats` | start stats fetch `{names?}` (default: all players **missing** stats) |
| GET | `/api/jobs` | job states (polling fallback) |
| GET | `/api/scan/stream` | SSE events: job progress + `{"type":"data"}` per stored row (live grid) |
| GET/POST | `/api/selections` | load/save the cell selection |
| POST | `/api/export` | `{cells, format, form_n?}` → `{text, chars, rows, columns}` — `format` ∈ `llm` (default) `tsv` `md` `md_block` `json` |
| POST | `/api/clear` | drop the downloaded rows (matches, odds, stats) + the player cache → `{matches, stats, cache_files, cache_bytes, failed}`; selections untouched, `409` while a job runs |

## Tests

```bash
.venv/bin/python -m pytest tests/ -q      # 38 tests, offline (fixtures)
```

* `tests/test_winamax_core.py` — selector regression against real DOM snapshots
  (`fixtures/list_page.html`, `fixtures/match_page.html`) run in Chrome, plus a
  "userscript build is up to date" check.
* `tests/test_app.py` — column computation on frozen Tennis Abstract payloads,
  exporter formats, SQLite semantics.
* `tests/test_jobs.py` — job targeting (missing vs force vs explicit ids),
  per-row `data` events, version lookup — Winamax / TA replaced by fakes.

Refresh a fixture with a live capture whenever Winamax changes its DOM:

```bash
.venv/bin/python tools/capture_fixture.py   # (see git history / re-run the odds pass with capture=...)
```

## Optional: daily auto-scan (launchd)

The server already scans on start; for an always-on dashboard, launch it at
login — or schedule a scan-only pass with a `launchd` job (example, adjust the
paths):

```xml
<!-- ~/Library/LaunchAgents/com.tennis.stats.scan.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.tennis.stats.scan</string>
  <key>ProgramArguments</key><array>
    <string>/Users/anthonyrm/Projets/tennis_stats/.venv/bin/python</string>
    <string>-c</string>
    <string>import asyncio; from core.winamax import WinamaxSession;
from app.store import Store
async def main():
    async with WinamaxSession() as wx:
        Store().upsert_matches(await wx.scan_today())
asyncio.run(main())</string>
  </array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>7</integer></dict>
  <key>WorkingDirectory</key>
  <string>/Users/anthonyrm/Projets/tennis_stats</string>
</dict></plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.tennis.stats.scan.plist
```

## Notes & limitations

* **Live cards**: in-play cards carry no clock, so the round is read from the
  text between the tournament and the first player (`ATP - Shanghai`, `1/4`,
  `<player>`) — score digits never qualify because the scan stops at the
  players. Live rows show `Tournament · Round · in play` with no empty segment.
* **Winamax WAF**: one long-lived page, click/back navigation only — deep links
  return 403. On a block the driver relaunches **headed** once and retries;
  if that fails too, the job reports the error (human-looking delays throughout).
* **All days**: the scan scrolls the whole page (tomorrow's matches get
  `+1d` / `Tomorrow`, later ones `+2d`, …), every match is listed and the table
  is grouped by day (`TODAY (16/10/2026)`, `TOMORROW (17/10/2026)`, …) with a
  per-day **Select day** button. `{"stop_at_night_gap": true}` restores the
  userscript rule (stop after a >6 h gap = current session + live only).
* **Live matches** are listed and flagged, but their odds are not fetched (they
  move too fast) — the odds-derived cells (`a.games`, `a.spread`, …) stay
  `—` for live matches.
* **Live grid**: the odds / stats jobs emit one SSE `data` event per stored
  row, and the dashboard reloads on it (at most every 1.5 s) — rows fill in
  during the pass, not at the end. The viewport (both scroll axes) and the
  selection survive those mid-pass reloads.
* **Tennis Abstract politeness**: 1.6 s between players, 24 h cache in
  `data/cache/<date>/`. Name → id resolution (`core/tennisabstract.py`)
  retries every probe and only trusts *answers*: candidate variants (accents,
  particles, `Jr`/`Sr` suffixes) that all come back 404 fall back to TA's own
  search list (`mwplayerlist.js`, at most one fetch per run), so an id that
  differs from the display name still resolves (`Thiago Tirante` →
  `ThiagoAgustinTirante`, `Martin Damm Jr` → `MartinDamm`). Only after TA
  answered and had nothing does a player become `unresolved` (e.g.
  `Zhang Zhizhen`), with the tried variants stored in the payload and the
  `TA?` badge on the row. A run where TA never answers (timeouts, 5xx,
  403/429) is a retriable `error` instead — never `unresolved` — and three
  consecutive such failures stop the pass with a job error, so a network
  outage reports itself instead of blanking every player for the day.
* Schema changes to `data/tennis_stats.db` need a migration — tables are
  created with `IF NOT EXISTS` only.

## Deviations from PLAN.md

* `stats.player_key` is the **player name**, not the TA id (matches reference
  players by name; unresolved players have no id). The TA id stays inside the
  payload.
* The userscript is assembled by `tools/build_userscript.py` (core inlined)
  rather than loaded via Tampermonkey `@require`, so `script.js` stays a single
  self-contained file with its existing update URL.
* An extra export format (`md_block`, one Markdown block per match) was added
  alongside the planned TSV/Markdown/JSON toggle.
