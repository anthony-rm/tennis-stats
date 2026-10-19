# Tennis Betting Dashboard — Implementation Plan

Project root: `/Users/anthonyrm/Projets/tennis_stats`
Status: plan v1 (2026-10-16). All feasibility claims below are backed by live probes run from this machine.

---

## 1. Goal

A local Python web dashboard that:

1. Retrieves today's tennis matches from Winamax automatically (background, no manual browsing).
2. Retrieves odds for those matches automatically.
3. Retrieves tennis-player statistics for every player found.
4. Shows one big table (match rows × data columns) with checkboxes to pick which data to extract.
5. Copies the selected data to the clipboard with one button.

---

## 2. What already exists (reuse inventory)

### 2.1 Winamax userscript — `~/Projets/cotes/useful-tool/script.js` (1662 lines, v9.28)
Two modes, pure DOM/XPath scraping, no network API calls anywhere:

| Mode | URL | What it does |
|---|---|---|
| `sports` | `winamax.fr/paris-sportifs/sports/5` (tennis) | Scrolls the match list, parses `match-card-*` cards into `{tournament, round, player1, player2, matchTime, dayIndicator}`, filters doubles, stops at a 6-hour "night gap", copies `A vs B (Tournament - Round)` lines. |
| `match` | `winamax.fr/paris-sportifs/match/<id>` | Finds section headings that own `odd-button-*`, expands them (`Plus de sélections`, grid/list toggle), extracts every market: standard sections, `Double chance`, player-prop sections (`bet-group-template`), `Score exact`. Special Over/Under and per-player filter buttons for `Nombre de jeux` / `Écart de jeux`. |

Reused as-is: **all selectors, expansion logic, heading filters, player-name heuristics, night-gap rule.**
Known gaps for automation (must be added):
- The sports mode does **not** capture the match id/URL — only text. Match id must be taken from the card's `data-testid="match-card-<id>"` (verified present).
- The match mode returns **formatted clipboard text**, not data. Needs a JSON variant: `{sections:[{title, rows:[{label, odds}]}]}`.
- Everything clipboard/UI-specific must be stripped out of the shared core.

### 2.2 Tennis player stats — `~/Projets/tennis_players/tennisabstract_scraper.py`
`requests` + `BeautifulSoup` + `pandas`, source = tennisabstract.com.
- `player_name_to_id("Novak Djokovic") -> NovakDjokovic` — **verified working**: `jsfrags/NovakDjokovic.js` → 200 (254 KB), `jsfrags/AlexDeMinaur.js` → 200, wrong forms (`DjokovicNovak`) → 404. So the `.title()`-strip-spaces rule is correct, 404 detection is the safety net.
- `player_frag` contains **19 tables**, currently only 4 are parsed:
  `recent-results, tour-years, chall-years, recent-finals, year-end-rankings, recent-events, career-splits, last52-splits, winners-errors, serve-speed, pbp-points, pbp-games, pbp-stats, mcp-serve, mcp-return, mcp-rally, mcp-tactics, head-to-heads`
- **Bug found:** `profile.json` comes back `{}` for all 4 cached players. Cause: the `#bio` span is filled client-side (`<span id="bio">&nbsp;</span>` in raw HTML). The values do exist as inline JS vars in the raw page: `rank`, `peakrank`, `elo_rank`, `elo_rating`, `hand`, `country`, `birthdate`/`bd`. Fix = regex those vars instead of parsing rendered text.
- **Bug found:** `major_recent_events` (`major-events` table id) does not exist in the fragment → always empty. Drop it, use `recent-events` instead.

### 2.3 Dashboard precedent — `~/Projets/cotes/interface` and `~/Projets/cotes/paris_analysis`
- `interface/`: `http.server` + one self-contained `dashboard.html` (vanilla JS, Chart.js) + `bets.json`. Proven pattern for a local single-page tool.
- `paris_analysis/app.py`: Streamlit analytics on `bets.json`.

---

## 3. Feasibility assessment

**Verdict: feasible.** Everything on the critical path was tested live today.

| # | Question | Probe result |
|---|---|---|
| 1 | Does Winamax render in headless Chrome? | **Yes.** `channel="chrome"` headless → tennis list loads, 9 `match-card-*` elements, real content (`ATP - Shanghai1/4 \| Rafael Jodar \| Casper Ruud \| … \| 1,60 \| 2,00`). Plain `requests` gets a 30 KB shell with zero `data-testid` (SPA) → browser required. |
| 2 | Are match ids / deep links usable? | Match id = `data-testid` suffix (`match-card-75614834` → `/paris-sportifs/match/75614834`). **But a full `page.goto()` of a match URL returns CloudFront 403 "Request blocked".** |
| 3 | Is there a way around the 403? | **Yes — verified:** list → `card.click()` → match page loads (53 then 69 `odd-button-*`), `history.go_back()` → list again. 3 consecutive cycles, zero blocks. Never full-reload a deep link; navigate inside the SPA. |
| 4 | How much odds depth per match? | 53–69 odds buttons on a single match: `Résultat`, `Score exact`, `Vainqueur`, per-set/per-game markets, `Paris combinés`, Flash bets — far more than the list page shows (list only carries 1X2 + percentages). |
| 5 | Does tennisabstract.com respond? | Yes: `jsfrags/<id>.js` 200 with all 19 tables; player page 200. |
| 6 | Environment | macOS, Google Chrome installed (no Playwright browser download needed), Node 24, Python 3.12/3.14, `tennis_stats/.venv` created with `playwright` installed. |

### Risks

| Risk | Level | Mitigation |
|---|---|---|
| CloudFront WAF blocks deep-link loads | **High (known)** | Proven rule: one long-lived page, click/back navigation only. Fallback: headed Chrome (`headless=False`) on the user's desktop, persistent profile. |
| Winamax DOM changes break selectors | High (ongoing) | Single shared `winamax_core.js` used by both the userscript and the dashboard (one source of truth); selector snapshot tests; userscript version bump = dashboard update. |
| Headless fingerprint detected later | Medium | Real Chrome channel, `locale=fr-FR`, `timezone=Europe/Paris`, realistic UA, 1–2 s delays, headed fallback. |
| List coverage vs. reality | Medium | Acceptance check: count must match what the userscript finds in a real browser on the same day; keep night-gap + doubles filters. |
| Name → Tennis Abstract id failures (particles, hyphens, accents) | Medium | 404 → retry variants (drop `de/le/la/von`, strip accents) → unresolved flag in UI instead of silent empty data. |
| Tennis Abstract politeness | Low | 1.5–2 s delay, fetch only players in today's matches, 24 h cache (players repeat daily). |
| Clipboard API | Low | `navigator.clipboard.writeText` works on `http://127.0.0.1` (trustworthy origin); fallback `execCommand('copy')` (same technique as the userscript). |
| ToS / automated access | Note | Winamax publishes no API and restricts automated access. This stays a local, personal, low-rate tool: no login automation, no bets placed, human-like delays, results never redistributed. |

---

## 4. Technical choices (decisions)

| Decision | Choice | Rejected alternative & why |
|---|---|---|
| Winamax retrieval | **Playwright (Python) + `channel="chrome"`**, background job | HTTP/API scraping: site is an SPA fed by socket.io (`sports-eu-west-3.winamax.fr`) — reverse-engineering that protocol is undocumented and brittle; paid aggregators (OddsPapi, OddStream) cost money and throw away the userscript investment. Socket.io stays a possible phase-2 optimisation. |
| Code reuse strategy | **Extract `winamax_core.js`** from `script.js`: pure functions (`listMatches()`, `extractOddsJSON()`), no UI/clipboard. Injected with `page.add_script_tag`; the Tampermonkey script becomes a thin wrapper over the same file. | Porting ~800 lines of XPath to Python: doubles the maintenance and drifts from the userscript. |
| Navigation | Single page, click into matches, `go_back()` | Deep-link `goto()` → 403 (proven). |
| Player stats | Refactor `tennisabstract_scraper.py` into a module; fix profile parser; expose all 19 tables | Re-scraping another site: Tennis Abstract already gives rankings, Elo, form, surface splits, H2H, serve stats. |
| Web framework | **FastAPI + uvicorn + one static HTML/JS page** (the `interface/` pattern) | **Streamlit** (known from `paris_analysis`): the clipboard needs a browser user gesture inside a click handler, background scraping needs job state + SSE, and a 20×40 checkbox matrix is painful in Streamlit's rerun model. Dash: heavier, no gain. |
| Background jobs | `asyncio` task + job status object; progress streamed by **SSE** (`/api/scan/stream`) | Celery/RQ: overkill for one local process. |
| Persistence | SQLite (`data/tennis_stats.db`) + JSON cache for raw payloads | Pure JSON files: no query/update semantics for odds + selections. |
| Clipboard output format | **TSV** default (pastes straight into Excel/Google Sheets — fits the existing `excel/paris.xlsm` workflow), toggle to Markdown or plain text | — |
| Scheduling | Auto-scan on server start + "Refresh" button; optional `launchd` daily job | Only manual: violates "run automatically". |

---

## 5. Architecture

```
tennis_stats/
├── .venv/                      # Python env (created; playwright installed)
├── PLAN.md                     # this file
├── app/
│   ├── main.py                 # FastAPI app, static mount, SSE, job control
│   ├── jobs.py                 # scan / odds / stats background jobs + progress
│   ├── store.py                # SQLite: matches, players, stats, selections
│   └── exporter.py             # selection → TSV / Markdown / JSON text
├── core/
│   ├── winamax_core.js         # ← extracted from useful-tool/script.js (shared with Tampermonkey)
│   ├── winamax.py              # Playwright driver: list scan, click-through odds pass
│   └── tennisabstract.py       # ← refactored tennisabstract_scraper.py (module + fixes)
├── web/
│   └── index.html              # single-page dashboard (vanilla JS)
├── data/
│   ├── tennis_stats.db
│   └── cache/                  # per-player raw HTML fragments, keyed by date
└── tests/
    ├── fixtures/               # saved HTML snapshots (list + match pages)
    └── test_winamax_core.py    # selector regression tests against fixtures
```

### Data flow

```
[Scan]  Playwright (bg) ─ sports/5 ─ click-free list parse ─► matches table
              │  filter: doubles out, night-gap, day = today
              ▼
[Odds]  per match: click card → inject winamax_core.js → extractOddsJSON() → go_back()
              ▼
[Stats] unique players → resolve TA id (404 fallback) → jsfrags + player page → cache 24 h
              ▼
[UI]    table: rows = matches, columns = data items, checkboxes per cell
              ▼
[Copy]  selected cells → TSV/Markdown → navigator.clipboard.writeText()
```

### Match record (SQLite `matches`)
`match_id PK, url, tournament, round, player1, player2, start_local (Europe/Paris), day_offset, live, odd_p1, odd_p2, pct_p1, pct_p2, scanned_at, odds_json`

### Player record (SQLite `stats`)
`player_key (TA id), fetched_at, payload_json` — raw per-table DataFrames serialized once, then the UI picks fields without refetching.

---

## 6. Checkbox catalogue (columns of the big table)

Groups with select-all headers; each column = one checkbox per match row.

**A. Match (from Winamax list)**
`Tournament | Round | Start time | Day | 1X2 odds (P1 / P2) | Implied % (P1 / P2) | Match URL | Market count`
(last two dropped in §10.1, game-market columns added in §10)

**B. Player identity (Tennis Abstract page vars — fixes §2.2)**
`Current rank | Peak rank | Elo rank | Elo rating | Hand | Age | Country`

**C. Form (table `recent-results`, last N configurable)**
`Last 5/10 record (W-L) | Last match date | Opponent + score | Surface | DR | 1st serve in % | 1st serve won % | 2nd serve won % | Aces % | Double faults % | Break points saved | Match duration`

**D. Surface / conditions (table `career-splits`, row = match surface)**
`Surface W-L | Set win % | Game win % | Tiebreak win % | Hold % | Break % | Aces % | DF % | DR`

**E. Season (table `tour-years`, current year)**
`Season W-L | Win % | DR | Hld % | Brk % | Best result`

**F. Extended (tables `last52-splits`, `winners-errors`, `serve-speed`)**
`Last-52w record | Winner/error ratio | 1st serve speed | 2nd serve speed`

**G. Head-to-head (table `head-to-heads`)**
`H2H record | H2H last meeting`

**H. Advanced (tables `mcp-serve`, `mcp-return`, `mcp-rally`, `mcp-tactics` — optional)**
`Serve patterns | Return patterns | Rally length | Tactical notes`

**I. Derived comparisons** — *removed on request (2026-10-19, §11): the six
computed gap columns (Rank / Elo / Surface win-% / DR / Form / H2H) and their
*Derived* quick-select chip are gone; the raw values they compared live in
B / C / D / E / G. The letter I stays unused so every other column id keeps
its identity.*

Winamax's own match page also renders `Forme actuelle`, `Rang`, `Surface`, `Météo`, `Parcours` panels — captured in the same odds pass as a free fallback when Tennis Abstract resolution fails.

---

## 7. Implementation phases

**Phase 0 — Extract the shared core (½ day)**
Split `script.js` → `winamax_core.js` (pure) + userscript wrapper (UI/clipboard). Add `extractOddsJSON()` returning structured sections, and `matchId` capture on cards.
*Accept:* userscript still works on Winamax; core loads in a Playwright page without errors.

**Phase 1 — Winamax list scan (½ day)**
`winamax.py: scan_tennis_today()` — headless Chrome, click-free list parse, doubles/night-gap filters, ISO times in `Europe/Paris`, SQLite insert.
*Accept:* matches found ≥ what the userscript finds in a real browser the same day; no 403.

**Phase 2 — Winamax odds pass (½–1 day)**
`collect_odds(match_id)` — click → inject core → JSON → `go_back()`; loop with 1–2 s delays; live/in-play matches skipped or flagged.
*Accept:* ≥ 50 odds per non-live match, ≥ 3 matches in a row with no block; graceful resume on 403 (switch to headed mode and retry once).

**Phase 3 — Tennis Abstract module (½ day)**
Turn the scraper into a module: fix profile regex, drop `major-events`, add the 15 missing tables, id resolution with 404 fallbacks, per-day cache, only today's players.
*Accept:* `profile.json` no longer `{}`; all 19 tables (where present) parsed for a sample of 5 players, including one tricky name.

**Phase 4 — API + jobs (½ day)**
FastAPI: `POST /api/scan` (auto on startup), `GET /api/matches`, `POST /api/odds/{id}`, `POST /api/stats`, `GET /api/scan/stream` (SSE), `GET/POST /api/selections`.
*Accept:* scan runs from `uvicorn` with no manual browser step; progress visible live.

**Phase 5 — Dashboard UI (1–1.5 days)**
Single `index.html`: status bar + progress; match table with grouped checkbox columns, select-all per group/column, sticky headers, filters (hide finished/doubles), search by player; "Fetch stats" and "Fetch all odds" buttons; **Copy selection** button (TSV/Markdown/JSON toggle) with `navigator.clipboard` + fallback.
*Accept:* end-to-end — start server → matches appear → tick columns → one click → data pastes into Excel.

**Phase 6 — Hardening (½ day)**
SQLite migrations, cache invalidation, retry/backoff, 403 → headed fallback, fixture-based selector tests, optional `launchd` daily scan, README.

Total: **≈ 4–5 days** of focused work.

---

## 8. Open questions (defaults chosen, will confirm while building)

1. **Scope of odds:** all markets (userscript behaviour, slower) vs. only main markets (`Résultat`, `Vainqueur`, games O/U)? *Default: all markets — the extraction already exists.*
2. **Clipboard granularity:** whole table as TSV vs. one block per match vs. one block per player? *Default: TSV of the whole table + per-match Markdown option.*
3. **Live matches:** include with a `LIVE` flag or skip? *Default: flag, don't fetch odds for in-play (they move too fast).*
4. **Auto-refresh:** on startup only, or every N minutes? *Default: on startup + manual Refresh; periodic scan optional setting.*

---

## 9. Implementation status (2026-10-16) — all phases accepted

Built and verified live from this machine (`.venv`, Python 3.14, system Chrome):

| Phase | Result |
|---|---|
| 0 — shared core | `core/winamax_core.js` extracted; `tools/build_userscript.py` bakes it into `~/Projets/cotes/useful-tool/script.js` (v9.29, wrapper in `src/ui.js`, v9.28 backup kept). Acceptance: injected into the live Winamax list — UI appears, `Extract Matches` copied `Daniil Medvedev vs Tommy Paul (ATP - Shanghai - 1/4)` + `Ann Li vs Iga Swiatek (…)`; `node --check` clean; build freshness is a pytest. |
| 1 — list scan | `WinamaxSession.scan_today()`: headless Chrome + render-wait + empty-scan retry. Acceptance: scan stored 3–5 matches (live flagged, doubles out, odds/percentages captured), no 403; matches equal what the userscript found the same day. |
| 2 — odds pass | click → `extractOddsJSON()` → `go_back()`. Acceptance: **3 consecutive matches in one session: 145 / 135 / 106 odds** (≥ 50 required), 19–32 sections each, 15–31 s per match, zero blocks; 2 further passes earlier the same day. |
| 3 — TA module | `core/tennisabstract.py`: profile from inline vars (fixes the `{}` bug), `major-events` dropped, all **18 tables** parsed, 404-safe id resolution (accents/hyphens/particles), 24 h cache, unresolved flag. Acceptance: 5 players × 18 tables + `profile.json` populated (Djokovic rank 8, Elo 2074 …); tricky names resolved first try (`AlexDeMinaur`, `GaelMonfils`, `AlejandroDavidovichFokina`); `Zhang Zhizhen` → unresolved with tried variants. |
| 4 — API + jobs | FastAPI + SSE. Acceptance: uvicorn start → auto-scan stored matches with no manual browser step; SSE frames show `queued → running → done` for all three job kinds; busy jobs return 409. |
| 5 — dashboard | `web/index.html`. Acceptance (Playwright E2E): 3 rows × 59 header cells render, search filter 3→1, hide-live 3→2, header checkbox selects 3 cells, **Copy → clipboard TSV** `Match\tTournament\tCurrent rank` + values, selection survives reload (6 cells), 0 page errors. |
| 6 — hardening | `pytest tests/ -q` → **26 passed** (selector regression vs. DOM fixtures, column/exporter/store units, build freshness); retry/backoff on TA fetches; 403 → headed retry once; README (+ launchd example). |

Deviations (details in README): `stats.player_key` = player name (not TA id);
userscript assembled by build script instead of `@require`; extra `md_block`
export format; catalogues/labels for groups D/F/H documented (surface inference,
mean ratios/speeds).

---

## 10. Update (2026-10-16, second pass) — user-requested features

§6 stays the original spec; the catalogue was extended and the UI grew the
controls below. All verified live on `127.0.0.1:8000` + `pytest tests/ -q` →
**30 passed**.

| # | Request | Implementation |
|---|---|---|
| 1 | Copy output readable by an LLM | New `llm` export format (default): one block per match, `### <group>` headings, one `- Label: value` line per field, player pairs expanded to real names (`Current rank: Daniil Medvedev 6, Tommy Paul 18`), `P1 +x` gaps rewritten with names, reading guide on top. Format remembered in `localStorage`, initial default `llm`. |
| 2 | Easier "select all boxes" (`Odds`, `Player statistics`, off by default) | Second toolbar row of quick-select chips — *Match info* / *Odds* / *Player statistics* / *Derived* — toggling whole column sets for visible rows, with live pressed/partial states; header group & column checkboxes now also reflect checked/indeterminate state. Empty selection ⇒ default = *Match info* on today's matches, Odds/Player statistics off (`maybeApplyDefaultSelection`, `ts.touched` marker in localStorage). |
| 3 | More Tennis Abstract data | 58 → **136 columns** (groups A–L): +height/birthdate, opponent rank, full surface & season splits (matches/sets/games/TBs/serve-return-point %), winners/UFEs/serve-speed extras, H2H meetings/TB/first/DR/BP %, MCP serve/rally/tactics detail, new group J point-by-point (BP/DP conversion, deuce/ad, DR+/EI/BLR), K rankings & surface Elo (`2028 #4`), prev-year rank, challenger record, L Grand Slam/Masters/TOP10/Finals/Best-of-5 splits. Means now skip Tennis Abstract's trailing `Career (N matches)` total row (pre-existing flaw, also fixed for the old F/H columns). |
| 4 | `Nombre de jeux` / `Écart de jeux` odds | 4 new group-A columns parsed from the stored odds pass (`_games_cells`): main line `Over 22.5 1.78 | Under 22.5 1.78`, main handicap `D. Medvedev -3.5 1.78 | T. Paul +3.5 1.68`, plus full ladders. Exact-title match only (never `1er set - …` / `… de <player>`). |
| 5 | List all Winamax matches, today selected by default | Scan now runs `stop_at_night_gap: false` (auto-scan + Refresh) so every day is stored; new **Day** filter (Today default, Tomorrow/+Nd, All days) drives the view and the default cell selection. **Fetch odds** covers every pending match of the whole list (not the view/lines), and **Refresh odds** re-fetches even the odds already shown. |
| 6 | Button copying only the match list | **⧉ Copy match list** → `A vs B
C vs D` lines of the visible rows. |
| 7 | Boxes drift under the wrong title when sections are hidden | Root cause: `renderBody()` skipped collapsed groups while `renderHead()` still emitted their 1-cell `<th>`. Fix: a placeholder `<td class="collapsed-col">` per collapsed group — header/body cell counts and per-index `data-col` alignment verified for baseline, A, A+C and all-but-one collapsed. |

Also fixed while there: cells belonging to matches pruned by a re-scan were
kept in the selection (they inflated the counter and blocked the default
preset) — `pruneSelection()` drops them on load.

### 10.1 same day — second pass (review round)

| Request | Implementation |
|---|---|
| Round missing on live matches | `core/winamax_core.js`: a card without a clock now takes its round from between the tournament and the first player (`'ATP - Shanghai'`, `'1/4'`, `'<player>'`), stopping at player names so score digits can't be mistaken for a round; `test_list_scan_extracts_match_ids` asserts `round == "1/4"` for every live fixture card; userscript rebuilt from the shared core. |
| Copy a single line | Row checkbox in the sticky Match cell: selects / clears **every column for that match only** (indeterminate when partial), kept in sync by `refreshCells()`. |
| Remove *Match URL* + *Market count* | Dropped from the catalogue and the computation → 145 → **136 columns**; quick sets updated (`Match info` = tournament/round/start/day). |
| Remove the subtitle | `h1` is now just *Tennis Betting Dashboard*. |
| *Events & finals* always empty | Group removed (`recent-events` rows rarely match the Winamax tournament name) → **12 groups, A–L**; the former group M (big-match splits) becomes L, its columns `m.*` → `l.*`. |
| Unify the UI | Every toolbar control is exactly 32 px; sticky header offsets and table height follow the measured header (`--hdr` / `--gh` custom properties, re-synced on render/resize); checked & partially-checked headers highlight in the accent colour; live rows show `Tournament · Round · in play` with no empty `·`; the collapsed-group spacer is empty; a notice row appears when the Day filter/search hides everything; no page-level overflow. |

### 10.2 same day — third pass

| Request | Implementation |
|---|---|
| Hide *Tournament* / *Round* / *Start time* (still copied) | New `Column.hidden` flag: those fields **plus `Day`** stay in the catalogue and the export but are no longer drawn as grid columns — the row summary and the day header already show them. |
| Group matches by day, drop the *Day* column, per-day select | `day_label()` now returns `Today (16/10/2026)`; the body renders a `day-row` per day with a **Select day / Clear day** button + counter; rows are sorted day → kick-off (live first); the Day filter `<select>` is gone. |
| Line & column checkboxes + chips as filters | New `state.lines` (selected matches; `localStorage`, defaults to today): chips, group/column headers and the copy act on **selected lines only**. Boxes on unticked lines stay stored but render disabled/unchecked and drop out of the copy (`exportCells()`); re-ticking restores them. Day buttons toggle a whole day. |
| Horizontal-scroll glitch in the Match header | Root cause: `thead tr.groups/cols th` (specificity 0,1,3) beat `th.sticky-col`, so the frozen header shared z-index 12/11 with the scrolling headers and later siblings painted over it. Fix: `thead tr th.sticky-col { z-index: 30 }` (body sticky = 14). |
| *Total games* / *Games spread* columns | Main-line columns dropped; the ladders became **Total games** / **Games spread**, newline-grouped (`Over
15.5 → 1.06 · …
Under
…`; one block per player **surname**, sorted by signed handicap) and rendered `pre-wrap` so the whole content is visible. Data check: `rows == buttonCount` for every game section (18/18, 14/14) — the earlier impression of missing lines came from the pairing logic dropping 4-row handicap lines (Kartal–Ostapenko), which player-based grouping no longer needs. |
| Two rows per match instead of `P1 \| P2` | Each match renders `tr.p1` / `tr.p2`: pair columns are split per player, non-pair columns span both rows (`rowspan=2`), the summary cell spans both. Column alignment verified per index in expanded and collapsed states (0 mismatches). |
| Less dense | Cell padding 7/10, no vertical grid lines, thicker separator between matches, day separator rows, non-selected lines dimmed (45 %), one value per cell. |
| Remove *Select visible* | Button removed (the header "all columns" checkbox does that for the selected lines). |
| All days by default | Day filter removed — every match the scan finds is listed, grouped by day. |
| Remove *Implied %* | `a.pct` dropped: 136 → **133 columns**. |
| Housekeeping | Count reads `selected/shown matches · cells · cols`; page-level vertical overflow removed (table height = `100vh − header − 34px`). |

## 11. Update (2026-10-19) — local clock, official day, *Derived* removed

`pytest tests/ -q` → **38 passed**; behaviour verified live on `127.0.0.1:8001`
(in Chrome: day-label box x = 17 and text x = 27 for every `scrollLeft` from 0
to 900).

| Request | Implementation |
|---|---|
| Kick-off times on the viewer's clock | Winamax cards carry **Europe/Paris** times (`OFFICIAL_TZ`, browser context `timezone_id="Europe/Paris"`). New `start_display(match)` converts the Paris `HH:MM` of `match_date()` with `datetime.astimezone()` → the machine's zone: it feeds `a.start`, the row summary (`start_display` next to `start_local` in `/api/matches` `raw`) and the sort key `timeKey()`. Live stays `in play`, non-clock text is passed through, never invented. |
| Group by the **official** day, not the local date | `day_label()` anchored on `date.today()` (machine clock) while the `demain` / `mar.` indicator is Paris-relative — between 00:00 and 05:59 local the header date was off by one. New `match_date()` = Paris date of the scan (`scanned_at`) + indicator / day-offset delta, and `official_today()` = `datetime.now(OFFICIAL_TZ).date()` for the Today / Tomorrow / +Nd name, so stored rows keep the official day until the next scan (the 22 rows read as *Demain* on 18/10 now show `Today (19/10/2026)` on 19/10). |
| *Today / Tomorrow* still moved on horizontal scroll | The label was sticky at its pin position while its natural spot was 10 px right (the day `<td>` kept `padding-left: 10px`), so it slid during the first 10 px of scroll and then sat 10 px left of the frozen Match column. Fix: `tbody tr.day-row td { padding: 6px 0 }` and the inset moved onto `.day-cell` (`padding: 0 18px 0 10px`) — box pinned at x = 17, text at x = 27 (the Match column's text x) for every scroll offset. |
| Remove *Derived* | Group I dropped from the catalogue (the `_gap` helper and the whole `i.*` block go with it) → 133 → **127 columns**, 11 groups (the letter I stays unused so other ids keep their identity); the *Derived* chip is gone; `QUICK.stats` = every non-A group; `pruneSelection()` also drops boxes on columns the catalogue no longer lists (stored selections keep working); the exporter's reading guide lost its gap-fields line. H2H itself stays in group G. |
