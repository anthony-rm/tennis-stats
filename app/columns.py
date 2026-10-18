"""Column catalogue (PLAN.md §6/§10, groups A-L) and per-match value computation.

Every column id is stable: it is the API contract, the checkbox identity in the
UI, and the key used by app/exporter.py when copying the selection.

Cell conventions:
  * per-player stat columns (`pair=True`) render both players as "P1 | P2"
    (row order = the match row's player1 | player2);
  * "—" means unknown (stats not fetched / player unresolved / table absent);
  * derived gap columns say who leads: "P1 +5", "P2 +0.15", "even", "never";
  * Winamax game-market cells (a.games / a.spread and their ladders) are parsed
    from the stored odds pass (`odds_json`) — see _games_cells().
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable, Optional

MISSING = "—"


# ------------------------------------------------------------- catalogue ---
@dataclass(frozen=True)
class Column:
    id: str
    label: str
    hint: str = ""
    pair: bool = False      # value is "P1 value | P2 value" (a player pair)
    hidden: bool = False    # shown in the row summary / copy, not as a grid column


@dataclass(frozen=True)
class Group:
    id: str
    label: str
    columns: tuple[Column, ...]


GROUPS: tuple[Group, ...] = (
    Group("A", "Match (Winamax)", (
        # hidden: already shown in the row summary / day header, kept for the copy
        Column("a.tournament", "Tournament", hidden=True),
        Column("a.round", "Round", hidden=True),
        Column("a.start", "Start time", "Europe/Paris; 'in play' for live matches",
               hidden=True),
        Column("a.day", "Day", "Today (16/10/2026) / Tomorrow / +Nd", hidden=True),
        Column("a.odds", "1X2 odds", "P1 | P2", pair=True),
        Column("a.games", "Total games",
               "Winamax 'Nombre de jeux' — every line, Over and Under grouped"),
        Column("a.spread", "Games spread",
               "Winamax 'Écart de jeux' — every line, one group per player (surname)"),
    )),
    Group("B", "Player identity (Tennis Abstract)", (
        Column("b.rank", "Current rank", "P1 | P2", pair=True),
        Column("b.peak", "Peak rank", "P1 | P2", pair=True),
        Column("b.elo_rank", "Elo rank", "P1 | P2", pair=True),
        Column("b.elo", "Elo rating", "P1 | P2", pair=True),
        Column("b.hand", "Hand", "P1 | P2 — R/L", pair=True),
        Column("b.age", "Age", "P1 | P2", pair=True),
        Column("b.country", "Country", "P1 | P2 — NOC code", pair=True),
        Column("b.height", "Height", "P1 | P2 — cm", pair=True),
        Column("b.dob", "Birthdate", "P1 | P2 — date of birth", pair=True),
    )),
    Group("C", "Form — recent results (last N)", (
        Column("c.record", "Last N record", "W-L over the last N tour matches", pair=True),
        Column("c.last_date", "Last match date", pair=True),
        Column("c.last_opp", "Last match", "result + opponent + score", pair=True),
        Column("c.surface", "Surface", "surface of the last played event", pair=True),
        Column("c.dr", "DR (avg)", "dominance ratio, mean over last N", pair=True),
        Column("c.s1_in", "1st serve in % (avg)", pair=True),
        Column("c.s1_won", "1st serve won % (avg)", pair=True),
        Column("c.s2_won", "2nd serve won % (avg)", pair=True),
        Column("c.aces", "Aces % (avg)", pair=True),
        Column("c.dfs", "Double faults % (avg)", pair=True),
        Column("c.bps", "Break pts saved (Σ)", "saved/total over last N", pair=True),
        Column("c.duration", "Match duration (avg)", pair=True),
        Column("c.opp_rank", "Opp. rank (avg)", "mean opponent rank over the last N", pair=True),
    )),
    Group("D", "Surface / conditions (career splits)", (
        Column("d.record", "Surface W-L", "career record on the match surface (inferred)", pair=True),
        Column("d.set_pct", "Set win %", pair=True),
        Column("d.game_pct", "Game win %", pair=True),
        Column("d.tb_pct", "Tiebreak win %", pair=True),
        Column("d.hold_pct", "Hold %", pair=True),
        Column("d.brk_pct", "Break %", pair=True),
        Column("d.aces", "Aces %", pair=True),
        Column("d.dfs", "DF %", pair=True),
        Column("d.dr", "DR", pair=True),
        Column("d.matches", "Matches", "on the match surface", pair=True),
        Column("d.sets", "Sets W-L", "on the match surface", pair=True),
        Column("d.games", "Games W-L", "on the match surface", pair=True),
        Column("d.tb", "Tiebreaks W-L", "on the match surface", pair=True),
        Column("d.s1_in", "1st serve in %", pair=True),
        Column("d.s1_won", "1st serve won %", pair=True),
        Column("d.s2_won", "2nd serve won %", pair=True),
        Column("d.spw", "Serve pts won %", pair=True),
        Column("d.rpw", "Return pts won %", pair=True),
        Column("d.tpw", "Total pts won %", pair=True),
    )),
    Group("E", "Season (tour-level)", (
        Column("e.record", "Season W-L", "current calendar year", pair=True),
        Column("e.win_pct", "Win %", pair=True),
        Column("e.dr", "DR", pair=True),
        Column("e.hld", "Hld %", pair=True),
        Column("e.brk", "Brk %", pair=True),
        Column("e.best", "Best result", pair=True),
        Column("e.matches", "Matches played", pair=True),
        Column("e.sets", "Sets W-L", pair=True),
        Column("e.set_pct", "Set win %", pair=True),
        Column("e.game_pct", "Game win %", pair=True),
        Column("e.tb_pct", "Tiebreak win %", pair=True),
        Column("e.s1_in", "1st serve in %", pair=True),
        Column("e.s1_won", "1st serve won %", pair=True),
        Column("e.s2_won", "2nd serve won %", pair=True),
        Column("e.spw", "Serve pts won %", pair=True),
        Column("e.rpw", "Return pts won %", pair=True),
        Column("e.tpw", "Total pts won %", pair=True),
        Column("e.aces", "Aces %", pair=True),
        Column("e.dfs", "Double faults %", pair=True),
    )),
    Group("F", "Extended", (
        Column("f.last52", "Last-52w record", "W-L across surface splits, last 52 weeks", pair=True),
        Column("f.we_ratio", "Winner/error ratio", "mean of winners/UFEs ratio, recent matches", pair=True),
        Column("f.s1_speed", "1st serve speed", "mean 1st-serve speed (mph)", pair=True),
        Column("f.s2_speed", "2nd serve speed", "mean 2nd-serve speed (mph)", pair=True),
        Column("f.winners", "Winners (avg)", "mean per match", pair=True),
        Column("f.ufes", "Unforced errors (avg)", "mean per match", pair=True),
        Column("f.rally_ratio", "Rally winner/error ratio", pair=True),
        Column("f.vs_ratio", "Winner/error vs opponent",
               "his ratio against the opponent's own winners/UFEs", pair=True),
        Column("f.avg_speed", "Avg serve speed", "mean of per-match averages (mph)", pair=True),
        Column("f.s1_max", "Max 1st serve", "mean of per-match maxima (mph)", pair=True),
        Column("f.s2_max", "Max 2nd serve", "mean of per-match maxima (mph)", pair=True),
    )),
    Group("G", "Head-to-head", (
        Column("g.record", "H2H record", "vs this opponent, from P1's table", pair=True),
        Column("g.last", "H2H last meeting", "year of last meeting", pair=True),
        Column("g.meetings", "Meetings", "total matches vs this opponent", pair=True),
        Column("g.win_pct", "H2H win %", "from P1's table", pair=True),
        Column("g.tb", "H2H tiebreaks", "TB W-L vs this opponent", pair=True),
        Column("g.first", "First meeting", "year of the first meeting", pair=True),
        Column("g.dr", "H2H DR", "dominance ratio in these meetings", pair=True),
        Column("g.s1_won", "1st serve % vs opp", pair=True),
        Column("g.s2_won", "2nd serve % vs opp", pair=True),
        Column("g.bp_saved", "BP saved % vs opp", pair=True),
        Column("g.bp_conv", "BP converted % vs opp", pair=True),
    )),
    Group("H", "Advanced (MCP)", (
        Column("h.serve", "Serve patterns", "mean unreturned-serve %", pair=True),
        Column("h.return", "Return patterns", "mean return points won % in return", pair=True),
        Column("h.rally", "Rally length", "mean rally length (shots)", pair=True),
        Column("h.tactics", "Tactical notes", "mean net-point frequency", pair=True),
        Column("h.s1_unret", "1st serve unreturned %", pair=True),
        Column("h.s2_unret", "2nd serve unreturned %", pair=True),
        Column("h.sv_impact", "Serve impact", "SvImpact index", pair=True),
        Column("h.ret_rip", "Return in play %", pair=True),
        Column("h.ret_wpn", "Return winners %", pair=True),
        Column("h.rally_1_3", "Rally 1-3 W%", pair=True),
        Column("h.rally_4_6", "Rally 4-6 W%", pair=True),
        Column("h.rally_7_9", "Rally 7-9 W%", pair=True),
        Column("h.rally_10p", "Rally 10+ W%", pair=True),
        Column("h.net_won", "Net points won %", pair=True),
        Column("h.snv_won", "Serve & volley won %", pair=True),
        Column("h.drop", "Drop shot freq %", pair=True),
    )),
    Group("I", "Derived comparisons", (
        Column("i.rank_gap", "Rank gap", "who is ranked better, by how much"),
        Column("i.elo_gap", "Elo gap"),
        Column("i.surface_gap", "Surface win-% gap", "percentage points on the match surface"),
        Column("i.dr_gap", "DR gap", "dominance ratio, last-52w surface splits"),
        Column("i.form_gap", "Form gap", "wins over last N"),
        Column("i.h2h", "H2H", "record from P1's perspective"),
    )),
    Group("J", "Point-by-point (last N)", (
        Column("j.gp_conv", "Game pts converted %", "pbp-points, mean over last N", pair=True),
        Column("j.bp_conv", "Break pts converted %", "pbp-points, mean over last N", pair=True),
        Column("j.bp_saved", "Break pts saved %", "pbp-points, mean over last N", pair=True),
        Column("j.breakback", "Break-back won %", "pbp-games", pair=True),
        Column("j.deuce_spw", "Deuce serve pts won %", "pbp-stats", pair=True),
        Column("j.ad_spw", "Ad serve pts won %", "pbp-stats", pair=True),
        Column("j.deuce_rpw", "Deuce return pts won %", "pbp-stats", pair=True),
        Column("j.ad_rpw", "Ad return pts won %", "pbp-stats", pair=True),
        Column("j.dr_plus", "DR+", "points-played dominance (pbp-stats)", pair=True),
        Column("j.ei", "EI (clutch)", "expected-improvement index (pbp-stats)", pair=True),
        Column("j.blr", "BLR", "big-point leverage (pbp-stats)", pair=True),
    )),
    Group("K", "Rankings & Elo history", (
        Column("k.points", "Ranking points", "current", pair=True),
        Column("k.h_elo", "Hard Elo", "rating #rank", pair=True),
        Column("k.c_elo", "Clay Elo", "rating #rank", pair=True),
        Column("k.g_elo", "Grass Elo", "rating #rank", pair=True),
        Column("k.prev_rank", "Prev-year rank", "ranking at the end of the previous year", pair=True),
        Column("k.chall", "Challenger record", "career challenger W-L", pair=True),
    )),
    Group("L", "Big matches (career splits)", (
        Column("l.slams", "Grand Slams W-L", "career-splits 'Grand Slams'", pair=True),
        Column("l.masters", "Masters/Premiers W-L", "'Masters' (ATP) or 'Premiers' (WTA)", pair=True),
        Column("l.top10", "vs Top 10 W-L", "career-splits 'vs Top 10'", pair=True),
        Column("l.finals", "Finals W-L", "career-splits 'Finals' row", pair=True),
        Column("l.b5", "Best of 5 W-L", pair=True),
    )),
)

COLUMNS: dict[str, Column] = {c.id: c for g in GROUPS for c in g.columns}
COLUMN_ORDER: list[str] = [c.id for g in GROUPS for c in g.columns]


def catalogue() -> list[dict]:
    return [
        {"id": g.id, "label": g.label,
         "columns": [{"id": c.id, "label": c.label, "hint": c.hint,
                      "pair": c.pair, "hidden": c.hidden} for c in g.columns]}
        for g in GROUPS
    ]


# ------------------------------------------------------------- helpers -----
def _norm(text) -> str:
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.casefold()


def _f(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(value))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except ValueError:
        return None


def _mean(values: Iterable[Any]) -> Optional[float]:
    nums = [v for v in (_f(v) for v in values) if v is not None]
    if not nums:
        return None
    return sum(nums) / len(nums)


def _mean_pct(values: Iterable[Any]) -> str:
    m = _mean(values)
    return f"{m:.1f}%" if m is not None else MISSING


def _mean_num(values: Iterable[Any], fmt: str = "{:.2f}") -> str:
    m = _mean(values)
    return fmt.format(m) if m is not None else MISSING


def _pair(v1: Any, v2: Any) -> str:
    def cell(v: Any) -> str:
        if v is None or v == "":
            return MISSING
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return str(v)
    return f"{cell(v1)} | {cell(v2)}"


def _gap(v1: Optional[float], v2: Optional[float], higher_better: bool,
         fmt: str = "{:.0f}", unit: str = "") -> str:
    if v1 is None or v2 is None:
        return MISSING
    if abs(v1 - v2) < 1e-9:
        return "even"
    p1_leads = (v1 > v2) if higher_better else (v1 < v2)
    who = "P1" if p1_leads else "P2"
    return f"{who} +{fmt.format(abs(v1 - v2))}{unit}"


def _num_txt(value: Any) -> Optional[str]:
    """2028.0 -> '2028', 1.31 -> '1.31', '71.4%' -> '71.4'."""
    if value is None or value == "":
        return None
    f = _f(value)
    if f is None:
        return str(value)
    return str(int(f)) if f.is_integer() else f"{f:g}"


# --------------------------------------------------------- player access ----
class PlayerView:
    """Typed accessors over one Tennis Abstract payload."""

    def __init__(self, payload: Optional[dict]) -> None:
        self.payload = payload or {}
        self.unresolved = bool(self.payload.get("unresolved")) or not self.payload
        self.profile: dict = self.payload.get("profile") or {}
        self.tables: dict = dict(self.payload.get("tables") or {})

    @property
    def available(self) -> bool:
        return not self.unresolved and bool(self.tables or self.profile)

    def table(self, table_id: str) -> Optional[dict]:
        return self.tables.get(table_id)

    def rows(self, table_id: str) -> list[list]:
        t = self.tables.get(table_id)
        return list(t.get("rows") or []) if t else []

    def idx(self, table_id: str, *names: str, fallback: Optional[int] = None) -> Optional[int]:
        t = self.tables.get(table_id)
        if not t:
            return None
        cols = [str(c) for c in t.get("columns") or []]
        for name in names:
            if name in cols:
                return cols.index(name)
        return fallback

    def col(self, table_id: str, row: list, *names: str, fallback: Optional[int] = None) -> Any:
        i = self.idx(table_id, *names, fallback=fallback)
        if i is None or i >= len(row):
            return None
        return row[i]

    def split_row(self, table_id: str, split: str) -> Optional[list]:
        key = _norm(split)
        for row in self.rows(table_id):
            if row and _norm(row[0]) == key:
                return row
        return None


_TOTAL_ROW = re.compile(r"^\s*(career|total|average|mean)\b", re.IGNORECASE)


def _data_rows(view: PlayerView, table_id: str, limit: Optional[int] = None) -> list[list]:
    """Rows of a per-match log table WITHOUT its trailing 'Career (N)' total row."""
    rows = [r for r in view.rows(table_id)
            if not (r and isinstance(r[0], str) and _TOTAL_ROW.match(str(r[0])))]
    return rows[:max(1, limit)] if limit else rows


def _recent_rows(view: PlayerView, n: int) -> list[list]:
    return _data_rows(view, "recent-results")[:max(1, n)]


def _outcome(row: list, view: PlayerView, player_name: str,
             table: str = "recent-results") -> Optional[str]:
    """'W' / 'L' from the opponent cell: '<winner> d. <loser>'."""
    opp = view.col(table, row, "Unnamed: 6", fallback=6)
    if not opp or " d. " not in str(opp):
        return None
    left, right = str(opp).split(" d. ", 1)
    last_token = _norm(player_name.split()[-1])
    in_left = last_token in _norm(left)
    in_right = last_token in _norm(right)
    if in_left == in_right:          # ambiguous (or name absent both sides)
        return None
    return "W" if in_left else "L"


def _bps_total(view: PlayerView, rows: list[list]) -> Optional[str]:
    i = view.idx("recent-results", "BPSvd", fallback=14)
    if i is None:
        return None
    made = att = 0
    seen = False
    for row in rows:
        if i >= len(row):
            continue
        m = re.match(r"\s*(\d+)\s*/\s*(\d+)", str(row[i]))
        if m:
            made += int(m.group(1))
            att += int(m.group(2))
            seen = True
    return f"{made}/{att}" if seen else None


def _mean_duration(view: PlayerView, rows: list[list]) -> Optional[str]:
    i = view.idx("recent-results", "Time", fallback=15)
    if i is None:
        return None
    minutes = []
    for row in rows:
        if i >= len(row):
            continue
        m = re.match(r"\s*(\d+):(\d{2})", str(row[i]))
        if m:
            minutes.append(int(m.group(1)) * 60 + int(m.group(2)))
    if not minutes:
        return None
    avg = round(sum(minutes) / len(minutes))
    return f"{avg // 60}:{avg % 60:02d}"


def _opp_rank(view: PlayerView, rows: list[list]) -> Optional[int]:
    """Mean opponent rank over `rows` (recent-results 'vRk')."""
    i = view.idx("recent-results", "vRk", fallback=5)
    if i is None:
        return None
    nums = [v for v in (_f(r[i]) for r in rows if i < len(r)) if v]
    if not nums:
        return None
    return int(round(sum(nums) / len(nums)))


_WEEKDAYS = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]


def day_delta(match: dict) -> int:
    """Days between today and the match (0 = today)."""
    ind = (match.get("day_indicator") or "").strip().lower().rstrip(".")
    if ind == "demain":
        return 1
    if ind in _WEEKDAYS:
        return (_WEEKDAYS.index(ind) - date.today().weekday()) % 7
    return max(0, int(match.get("day_offset") or 0))


def day_label(match: dict) -> str:
    """'Today (16/10/2026)' — the day header of the table and a copy field."""
    delta = day_delta(match)
    name = "Today" if delta == 0 else ("Tomorrow" if delta == 1 else f"+{delta}d")
    return f"{name} ({(date.today() + timedelta(days=delta)).strftime('%d/%m/%Y')})"


def _surface_for_match(view: PlayerView, tournament: str) -> Optional[str]:
    """Surface of the match: exact event row when the player already played it,
    otherwise the surface of his latest event (tour surfaces come in swings)."""
    rows = view.rows("recent-results")
    if not rows:
        return None
    key = _norm(tournament.split(" - ", 1)[-1].strip()) if tournament else ""
    surf_i = view.idx("recent-results", "Surface", fallback=2)
    tour_i = view.idx("recent-results", "Tournament", fallback=1)
    if key and surf_i is not None and tour_i is not None:
        for row in rows:
            if tour_i < len(row) and surf_i < len(row) and _norm(row[tour_i]) == key:
                return str(row[surf_i]) if row[surf_i] else None
    if surf_i is not None and len(rows[0]) > surf_i and rows[0][surf_i]:
        return str(rows[0][surf_i])
    return None


def _h2h_row(view: PlayerView, opponent_name: str) -> Optional[list]:
    tokens = [t for t in _norm(opponent_name).split() if t and t not in {"de", "van", "von", "del", "la", "le"}]
    if not tokens:
        return None
    keys = [" ".join(tokens[-2:]), tokens[-1]]
    for row in view.rows("head-to-heads"):
        opp = view.col("head-to-heads", row, "Opponent", fallback=1)
        if not opp:
            continue
        text = _norm(re.sub(r"\[[A-Z]{3}\]", "", str(opp)))
        if any(k in text for k in keys):
            return row
    return None


def _surface_totals(view: PlayerView, table_id: str) -> Optional[tuple[int, int]]:
    w = l = 0
    seen = False
    for split in ("Hard", "Clay", "Grass", "Carpet"):
        row = view.split_row(table_id, split)
        if not row:
            continue
        seen = True
        w += int(_f(view.col(table_id, row, "W", fallback=3)) or 0)
        l += int(_f(view.col(table_id, row, "L", fallback=4)) or 0)
    return (w, l) if seen else None


def _mean_col(view: PlayerView, table_id: str, *names: str,
              fallback: Optional[int] = None, fmt: str = "{:.1f}",
              limit: Optional[int] = None) -> str:
    i = view.idx(table_id, *names, fallback=fallback)
    if i is None:
        return MISSING
    vals = [row[i] for row in _data_rows(view, table_id, limit) if i < len(row)]
    return _mean_num(vals, fmt)


def _pct_value(value: Any) -> Optional[float]:
    """'75%' / '75.0' / '40% (2/5)' / '2/4' -> percent on a 0-100 scale."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in {"-", "—"}:
        return None
    m = re.fullmatch(r"(\d+)\s*/\s*(\d+)", text)
    if m:
        total = int(m.group(2))
        return 100.0 * int(m.group(1)) / total if total else None
    if re.search(r"\(\s*0\s*/\s*0\s*\)", text):
        return None                      # "… (0/0)" — the situation never arose
    return _f(text)


def _mean_pct_col(view: PlayerView, table_id: str, *names: str,
                  fallback: Optional[int] = None, limit: Optional[int] = None) -> str:
    """Mean of percent-ish cells ('75%', '2/4', '40% (2/5)') -> '75.0%'."""
    i = view.idx(table_id, *names, fallback=fallback)
    if i is None:
        return MISSING
    vals = [row[i] for row in _data_rows(view, table_id, limit) if i < len(row)]
    nums = [v for v in (_pct_value(v) for v in vals) if v is not None]
    if not nums:
        return MISSING
    return f"{sum(nums) / len(nums):.1f}%"


# --------------------------------------------------- Winamax odds markets ---
GAME_SECTIONS = ("Nombre de jeux", "Écart de jeux")


def _odds_sections(match: dict) -> dict[str, list[dict]]:
    """Match-page odds sections keyed by exact title (first occurrence only —
    the odds pass can extract the same section several times while scrolling)."""
    raw = match.get("odds_json")
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[dict]] = {}
    for sec in raw.get("sections") or []:
        title = str(sec.get("title") or "").strip()
        if title in GAME_SECTIONS and title not in out:
            out[title] = list(sec.get("rows") or [])
    return out


def _games_cells(match: dict) -> dict[str, str]:
    """Winamax 'Nombre de jeux' (total games) + 'Écart de jeux' (games spread).

    Every line the match page offered is kept, grouped for readability:
      * total games  -> "Over" block then "Under" block (lines ascending);
      * games spread -> one block per player (surname only), lines sorted by
        signed handicap.
    Rows are separated by newlines; the grid renders them pre-wrapped and the
    copy formats flatten them back to a single line.
    """
    out = {"a.games": MISSING, "a.spread": MISSING}
    sections = _odds_sections(match)

    # ---- total games ------------------------------------------------------
    over: dict[str, float] = {}
    under: dict[str, float] = {}
    for row in sections.get("Nombre de jeux") or []:
        label = str(row.get("label") or "").strip()
        value = _f(row.get("value"))
        m = re.match(r"(Over|Under)\s+([\d.,]+)", label)
        if not m or value is None:
            continue
        line = m.group(2).replace(",", ".")
        (over if m.group(1) == "Over" else under)[line] = value

    lines = [f"Over {float(ln):g}: {over[ln]:.2f}" for ln in sorted(over, key=float)]
    if under:
        if lines:
            lines.append("")            # blank separator between the two blocks
        lines += [f"Under {float(ln):g}: {under[ln]:.2f}"
                  for ln in sorted(under, key=float)]
    if lines:
        out["a.games"] = "\n".join(lines)

    # ---- games spread: one block per player, surname only -----------------
    by_player: dict[str, list[tuple[float, float]]] = {}
    order: list[str] = []
    for row in sections.get("\u00c9cart de jeux") or []:
        label = str(row.get("label") or "").strip()
        value = _f(row.get("value"))
        m = re.match(r"^(.*?)\s*([+-]\d+(?:[.,]\d+)?)$", label)
        if not m or value is None:
            continue
        name = m.group(1).strip()
        surname = name.split()[-1] if name else ""
        key = surname or name
        if not key:
            continue
        if key not in by_player:
            by_player[key] = []
            order.append(key)
        by_player[key].append((float(m.group(2).replace(",", ".")), value))

    if by_player:
        preferred = [p.split()[-1] for p in (match.get("player1"),
                                             match.get("player2")) if p]

        def rank(key: str) -> tuple[int, int]:
            for i, name in enumerate(preferred):
                if _norm(name) == _norm(key):
                    return (0, i)
            return (1, order.index(key))

        lines = []
        for key in sorted(by_player, key=rank):
            for h, v in sorted(by_player[key]):     # by signed handicap
                lines.append(f"{key} {h:+g}: {v:.2f}")
        if lines:
            out["a.spread"] = "\n".join(lines)
    return out


# ------------------------------------------------------------ compute -------
def compute_values(match: dict, stats: dict[str, dict], form_n: int = 10) -> dict:
    """values: {column_id: text} for one match row, plus a stats status block."""
    v1 = PlayerView(stats.get(match.get("player1") or ""))
    v2 = PlayerView(stats.get(match.get("player2") or ""))
    values: dict[str, str] = {}

    # ---- A: match ----
    values["a.tournament"] = match.get("tournament") or MISSING
    values["a.round"] = match.get("round") or "—"
    if match.get("live"):
        values["a.start"] = "in play"
    else:
        values["a.start"] = match.get("start_local") or MISSING
    values["a.day"] = day_label(match)
    o1, o2 = match.get("odd_p1"), match.get("odd_p2")
    values["a.odds"] = _pair(o1, o2) if (o1 or o2) else MISSING
    values.update(_games_cells(match))

    # ---- B: identity ----
    prof1, prof2 = v1.profile, v2.profile
    values["b.rank"] = _pair(prof1.get("current_rank"), prof2.get("current_rank"))
    values["b.peak"] = _pair(prof1.get("peak_rank"), prof2.get("peak_rank"))
    values["b.elo_rank"] = _pair(prof1.get("elo_rank"), prof2.get("elo_rank"))
    values["b.elo"] = _pair(prof1.get("elo_rating"), prof2.get("elo_rating"))
    values["b.hand"] = _pair(prof1.get("hand"), prof2.get("hand"))
    values["b.age"] = _pair(prof1.get("age"), prof2.get("age"))
    values["b.country"] = _pair(prof1.get("country"), prof2.get("country"))
    values["b.height"] = _pair(prof1.get("height"), prof2.get("height"))
    values["b.dob"] = _pair(prof1.get("birthdate"), prof2.get("birthdate"))

    # ---- C: form ----
    def form(view: PlayerView, name: str) -> dict:
        rows = _recent_rows(view, form_n)
        if not rows:
            return {}
        outcomes = [_outcome(r, view, name) for r in rows]
        wins = sum(1 for o in outcomes if o == "W")
        losses = sum(1 for o in outcomes if o == "L")
        latest = rows[0]
        opp = view.col("recent-results", latest, "Unnamed: 6", fallback=6) or ""
        score = view.col("recent-results", latest, "Score", fallback=7) or ""
        clean = re.sub(r"\s*\[[A-Z]{3}\]", "", str(opp))
        clean = re.sub(r"\(\d+\)", "", clean).strip()
        last_res = outcomes[0] or "?"
        return {
            "record": f"{wins}-{losses}" if (wins or losses) else MISSING,
            "last_date": view.col("recent-results", latest, "Date", fallback=0),
            "last_opp": f"{last_res} {clean} {score}".strip() if clean else MISSING,
            "surface": view.col("recent-results", latest, "Surface", fallback=2),
            "dr": _mean_num([view.col("recent-results", r, "DR", fallback=8) for r in rows], "{:.2f}"),
            "s1_in": _mean_pct([view.col("recent-results", r, "1stIn", fallback=11) for r in rows]),
            "s1_won": _mean_pct([view.col("recent-results", r, "1st%", fallback=12) for r in rows]),
            "s2_won": _mean_pct([view.col("recent-results", r, "2nd%", fallback=13) for r in rows]),
            "aces": _mean_pct([view.col("recent-results", r, "A%", fallback=9) for r in rows]),
            "dfs": _mean_pct([view.col("recent-results", r, "DF%", fallback=10) for r in rows]),
            "bps": _bps_total(view, rows) or MISSING,
            "duration": _mean_duration(view, rows) or MISSING,
            "opp_rank": _opp_rank(view, rows),
        }

    f1, f2 = form(v1, match.get("player1") or ""), form(v2, match.get("player2") or "")
    for key, col_id in (("record", "c.record"), ("last_date", "c.last_date"),
                        ("last_opp", "c.last_opp"), ("surface", "c.surface"),
                        ("dr", "c.dr"), ("s1_in", "c.s1_in"), ("s1_won", "c.s1_won"),
                        ("s2_won", "c.s2_won"), ("aces", "c.aces"), ("dfs", "c.dfs"),
                        ("bps", "c.bps"), ("duration", "c.duration"),
                        ("opp_rank", "c.opp_rank")):
        values[col_id] = _pair(f1.get(key), f2.get(key))

    # ---- D: surface ----
    surface1 = _surface_for_match(v1, match.get("tournament") or "")
    surface2 = _surface_for_match(v2, match.get("tournament") or "")
    surface = surface1 or surface2

    def surface_cells(view: PlayerView, surf: Optional[str]) -> dict:
        if not surf:
            return {}
        row = view.split_row("career-splits", surf)
        if not row:
            return {}
        g = lambda *names, fb=None: view.col("career-splits", row, *names, fallback=fb)
        return {
            "record": f"{int(_f(g('W', fb=3)) or 0)}-{int(_f(g('L', fb=4)) or 0)}",
            "set_pct": g("Set%", fb=6) or MISSING,
            "game_pct": g("Game%", fb=8) or MISSING,
            "tb_pct": g("TB%", fb=10) or MISSING,
            "hold_pct": g("Hld%", fb=12) or MISSING,
            "brk_pct": g("Brk%", fb=13) or MISSING,
            "aces": g("A%", fb=14) or MISSING,
            "dfs": g("DF%", fb=15) or MISSING,
            "dr": g("DR", fb=22) or MISSING,
            "matches": g("M", fb=1) or MISSING,
            "sets": g("Set W-L", fb=5) or MISSING,
            "games": g("Game W-L", fb=7) or MISSING,
            "tb": g("TB W-L", fb=9) or MISSING,
            "s1_in": g("1stIn", fb=16) or MISSING,
            "s1_won": g("1st%", fb=17) or MISSING,
            "s2_won": g("2nd%", fb=18) or MISSING,
            "spw": g("SPW", fb=19) or MISSING,
            "rpw": g("RPW", fb=20) or MISSING,
            "tpw": g("TPW", fb=21) or MISSING,
        }

    d1 = surface_cells(v1, surface1)
    d2 = surface_cells(v2, surface2)
    for key, col_id in (("record", "d.record"), ("set_pct", "d.set_pct"),
                        ("game_pct", "d.game_pct"), ("tb_pct", "d.tb_pct"),
                        ("hold_pct", "d.hold_pct"), ("brk_pct", "d.brk_pct"),
                        ("aces", "d.aces"), ("dfs", "d.dfs"), ("dr", "d.dr"),
                        ("matches", "d.matches"), ("sets", "d.sets"),
                        ("games", "d.games"), ("tb", "d.tb"),
                        ("s1_in", "d.s1_in"), ("s1_won", "d.s1_won"),
                        ("s2_won", "d.s2_won"), ("spw", "d.spw"),
                        ("rpw", "d.rpw"), ("tpw", "d.tpw")):
        values[col_id] = _pair(d1.get(key), d2.get(key))

    # ---- E: season ----
    def season(view: PlayerView) -> dict:
        year = str(date.today().year)
        rows = view.rows("tour-years")
        row = None
        for r in rows:
            if r and str(r[0]).strip() == year:
                row = r
                break
        if row is None and rows:
            row = rows[0]
        if not row:
            return {}
        g = lambda *names, fb=None: view.col("tour-years", row, *names, fallback=fb)
        return {
            "record": f"{int(_f(g('W', fb=2)) or 0)}-{int(_f(g('L', fb=3)) or 0)}",
            "win_pct": g("Win%", fb=4) or MISSING,
            "dr": g("DR", fb=22) or MISSING,
            "hld": g("Hld%", fb=12) or MISSING,
            "brk": g("Brk%", fb=13) or MISSING,
            "best": g("Best", fb=23) or MISSING,
            "matches": g("M", fb=1) or MISSING,
            "sets": g("Set W-L", fb=5) or MISSING,
            "set_pct": g("Set%", fb=6) or MISSING,
            "game_pct": g("Game%", fb=8) or MISSING,
            "tb_pct": g("TB%", fb=10) or MISSING,
            "s1_in": g("1stIn", fb=16) or MISSING,
            "s1_won": g("1st%", fb=17) or MISSING,
            "s2_won": g("2nd%", fb=18) or MISSING,
            "spw": g("SPW", fb=19) or MISSING,
            "rpw": g("RPW", fb=20) or MISSING,
            "tpw": g("TPW", fb=21) or MISSING,
            "aces": g("A%", fb=14) or MISSING,
            "dfs": g("DF%", fb=15) or MISSING,
        }

    s1, s2 = season(v1), season(v2)
    for key, col_id in (("record", "e.record"), ("win_pct", "e.win_pct"),
                        ("dr", "e.dr"), ("hld", "e.hld"), ("brk", "e.brk"),
                        ("best", "e.best"), ("matches", "e.matches"),
                        ("sets", "e.sets"), ("set_pct", "e.set_pct"),
                        ("game_pct", "e.game_pct"), ("tb_pct", "e.tb_pct"),
                        ("s1_in", "e.s1_in"), ("s1_won", "e.s1_won"),
                        ("s2_won", "e.s2_won"), ("spw", "e.spw"),
                        ("rpw", "e.rpw"), ("tpw", "e.tpw"),
                        ("aces", "e.aces"), ("dfs", "e.dfs")):
        values[col_id] = _pair(s1.get(key), s2.get(key))

    # ---- F: extended ----
    def extended(view: PlayerView) -> dict:
        out = {}
        tot = _surface_totals(view, "last52-splits")
        out["last52"] = f"{tot[0]}-{tot[1]}" if tot else MISSING
        ratio_i = view.idx("winners-errors", "Ratio", fallback=4)
        if ratio_i is not None:
            vals = [r[ratio_i] for r in _data_rows(view, "winners-errors")
                    if ratio_i < len(r)]
            out["we_ratio"] = _mean_num(vals, "{:.1f}")
        else:
            out["we_ratio"] = MISSING
        out["s1_speed"] = _mean_col(view, "serve-speed", "1st Avg", fallback=3, fmt="{:.0f} mph")
        out["s2_speed"] = _mean_col(view, "serve-speed", "2nd Avg", fallback=9, fmt="{:.0f} mph")
        out["winners"] = _mean_col(view, "winners-errors", "Winners", fallback=2, fmt="{:.1f}")
        out["ufes"] = _mean_col(view, "winners-errors", "UFEs", fallback=3, fmt="{:.1f}")
        out["rally_ratio"] = _mean_col(view, "winners-errors", "RallyRatio", fallback=9, fmt="{:.2f}")
        out["vs_ratio"] = _mean_col(view, "winners-errors", "vs Ratio", fallback=14, fmt="{:.2f}")
        out["avg_speed"] = _mean_col(view, "serve-speed", "Avg Speed", fallback=2, fmt="{:.0f} mph")
        out["s1_max"] = _mean_col(view, "serve-speed", "Max 1st", fallback=7, fmt="{:.0f} mph")
        out["s2_max"] = _mean_col(view, "serve-speed", "Max 2nd", fallback=13, fmt="{:.0f} mph")
        return out

    x1, x2 = extended(v1), extended(v2)
    for key, col_id in (("last52", "f.last52"), ("we_ratio", "f.we_ratio"),
                        ("s1_speed", "f.s1_speed"), ("s2_speed", "f.s2_speed"),
                        ("winners", "f.winners"), ("ufes", "f.ufes"),
                        ("rally_ratio", "f.rally_ratio"), ("vs_ratio", "f.vs_ratio"),
                        ("avg_speed", "f.avg_speed"), ("s1_max", "f.s1_max"),
                        ("s2_max", "f.s2_max")):
        values[col_id] = _pair(x1.get(key), x2.get(key))

    # ---- G: head-to-head (P1's table) ----
    h2h = _h2h_row(v1, match.get("player2") or "")
    if h2h is None:
        known = v1.available and v2.available
        for col_id in ("g.record", "g.last", "g.meetings", "g.win_pct", "g.tb",
                       "g.first", "g.dr", "g.s1_won", "g.s2_won",
                       "g.bp_saved", "g.bp_conv"):
            values[col_id] = "never" if known else MISSING
    else:
        g = lambda *names, fb=None: v1.col("head-to-heads", h2h, *names, fallback=fb)
        w = int(_f(g("W", fb=2)) or 0)
        l = int(_f(g("L", fb=3)) or 0)
        tb_w, tb_l = g("W.1", fb=6), g("L.1", fb=7)
        values["g.record"] = f"{w}-{l}"
        last = g("Last", fb=10)
        values["g.last"] = str(last) if last is not None else MISSING
        values["g.meetings"] = _num_txt(g("Mtgs", fb=0)) or MISSING
        values["g.win_pct"] = g("Win%", fb=4) or MISSING
        values["g.tb"] = (
            f"{_num_txt(tb_w)}-{_num_txt(tb_l)}"
            if tb_w is not None and tb_l is not None else MISSING
        )
        first = g("First", fb=9)
        values["g.first"] = str(first) if first is not None else MISSING
        values["g.dr"] = _num_txt(g("DR", fb=12)) or MISSING
        values["g.s1_won"] = g("1st%", fb=16) or MISSING
        values["g.s2_won"] = g("2nd%", fb=17) or MISSING
        values["g.bp_saved"] = g("BPSvd%", fb=20) or MISSING
        values["g.bp_conv"] = g("BPCnv%", fb=21) or MISSING

    # ---- H: advanced (means over the whole MCP tables) ----
    def mcp(table: str, name: str, fallback: Optional[int], fmt: str) -> tuple[str, str]:
        return (_mean_col(v1, table, name, fallback=fallback, fmt=fmt),
                _mean_col(v2, table, name, fallback=fallback, fmt=fmt))

    values["h.serve"] = _pair(*mcp("mcp-serve", "Unret%", 2, "{:.1f}%"))
    values["h.return"] = _pair(*mcp("mcp-return", "RiP W%", 3, "{:.1f}%"))
    values["h.rally"] = _pair(*mcp("mcp-rally", "RallyLen", 2, "{:.1f}"))
    values["h.tactics"] = _pair(*mcp("mcp-tactics", "Net Freq", 4, "{:.1f}%"))
    values["h.s1_unret"] = _pair(*mcp("mcp-serve", "1st: Unret%", None, "{:.1f}%"))
    values["h.s2_unret"] = _pair(*mcp("mcp-serve", "2nd: Unret%", None, "{:.1f}%"))
    values["h.sv_impact"] = _pair(*mcp("mcp-serve", "SvImpact", None, "{:.1f}"))
    values["h.ret_rip"] = _pair(*mcp("mcp-return", "RiP%", None, "{:.1f}%"))
    values["h.ret_wpn"] = _pair(*mcp("mcp-return", "RetWnr%", None, "{:.1f}%"))
    values["h.rally_1_3"] = _pair(*mcp("mcp-rally", "1-3 W%", None, "{:.1f}%"))
    values["h.rally_4_6"] = _pair(*mcp("mcp-rally", "4-6 W%", None, "{:.1f}%"))
    values["h.rally_7_9"] = _pair(*mcp("mcp-rally", "7-9 W%", None, "{:.1f}%"))
    values["h.rally_10p"] = _pair(*mcp("mcp-rally", "10+ W%", None, "{:.1f}%"))
    values["h.net_won"] = _pair(*mcp("mcp-tactics", "Net W%", None, "{:.1f}%"))
    values["h.snv_won"] = _pair(*mcp("mcp-tactics", "SnV W%", None, "{:.1f}%"))
    values["h.drop"] = _pair(*mcp("mcp-tactics", "Drop: Freq", None, "{:.1f}%"))

    # ---- J: point-by-point (last N) ----
    for col_id, table, name, fallback, kind, fmt in (
        ("j.gp_conv", "pbp-points", "GP Conv", 2, "pct", None),
        ("j.bp_conv", "pbp-points", "BP Conv", 3, "pct", None),
        ("j.bp_saved", "pbp-points", "BP Saved", 6, "pct", None),
        ("j.breakback", "pbp-games", "BreakBack%", 4, "pct", None),
        ("j.deuce_spw", "pbp-stats", "Deuce SPW%", 7, "pct", None),
        ("j.ad_spw", "pbp-stats", "Ad SPW%", 9, "pct", None),
        ("j.deuce_rpw", "pbp-stats", "Deuce RPW%", 10, "pct", None),
        ("j.ad_rpw", "pbp-stats", "Ad RPW%", 11, "pct", None),
        ("j.dr_plus", "pbp-stats", "DR+", 3, "num", "{:.2f}"),
        ("j.ei", "pbp-stats", "EI", 4, "num", "{:.1f}"),
        ("j.blr", "pbp-stats", "BLR", 2, "num", "{:.2f}"),
    ):
        if kind == "pct":
            pair = (_mean_pct_col(v1, table, name, fallback=fallback, limit=form_n),
                    _mean_pct_col(v2, table, name, fallback=fallback, limit=form_n))
        else:
            pair = (_mean_col(v1, table, name, fallback=fallback, fmt=fmt, limit=form_n),
                    _mean_col(v2, table, name, fallback=fallback, fmt=fmt, limit=form_n))
        values[col_id] = _pair(*pair)

    # ---- K: rankings & Elo history ----
    def rankings(view: PlayerView) -> dict:
        out: dict[str, Any] = {"points": None, "h_elo": None, "c_elo": None,
                               "g_elo": None, "prev_rank": None, "chall": None}
        rows = view.rows("year-end-rankings")
        if rows:
            cur = rows[0]
            g = lambda *names, fallback=None: view.col("year-end-rankings", cur, *names,
                                                       fallback=fallback)
            out["points"] = g("Points", fallback=2)

            def elo(val_name: str, rank_name: str, vfb: int, rfb: int) -> Optional[str]:
                v = _num_txt(g(val_name, fallback=vfb))
                if v is None:
                    return None
                r = _num_txt(g(rank_name, fallback=rfb))
                return f"{v} #{r}" if r else v

            out["h_elo"] = elo("hElo", "hElo Rank", 6, 5)
            out["c_elo"] = elo("cElo", "cElo Rank", 8, 7)
            out["g_elo"] = elo("gElo", "gElo Rank", 10, 9)

            prev_year = str(date.today().year - 1)
            for r in rows:
                if r and str(r[0]).strip() == prev_year:
                    out["prev_rank"] = view.col("year-end-rankings", r,
                                                "ATP Rank", "WTA Rank", fallback=1)
                    break

        w = l = 0
        seen = False
        for r in view.rows("chall-years"):
            if not r or not r[0]:
                continue
            if _norm(r[0]) == "career":
                w = int(_f(view.col("chall-years", r, "W", fallback=2)) or 0)
                l = int(_f(view.col("chall-years", r, "L", fallback=3)) or 0)
                seen = True
                break
            if str(r[0]).isdigit():
                w += int(_f(view.col("chall-years", r, "W", fallback=2)) or 0)
                l += int(_f(view.col("chall-years", r, "L", fallback=3)) or 0)
                seen = True
        out["chall"] = f"{w}-{l}" if seen else None
        return out

    k1, k2 = rankings(v1), rankings(v2)
    for key, col_id in (("points", "k.points"), ("h_elo", "k.h_elo"),
                        ("c_elo", "k.c_elo"), ("g_elo", "k.g_elo"),
                        ("prev_rank", "k.prev_rank"), ("chall", "k.chall")):
        values[col_id] = _pair(k1.get(key), k2.get(key))

    # ---- L: big-match splits ----
    def split_record(view: PlayerView, *splits: str) -> Optional[str]:
        for split in splits:
            row = view.split_row("career-splits", split)
            if row:
                w = int(_f(view.col("career-splits", row, "W", fallback=3)) or 0)
                l = int(_f(view.col("career-splits", row, "L", fallback=4)) or 0)
                return f"{w}-{l}"
        return None

    values["l.slams"] = _pair(split_record(v1, "Grand Slams"),
                              split_record(v2, "Grand Slams"))
    values["l.masters"] = _pair(split_record(v1, "Masters", "Premiers"),
                                split_record(v2, "Masters", "Premiers"))
    values["l.top10"] = _pair(split_record(v1, "vs Top 10"),
                              split_record(v2, "vs Top 10"))
    values["l.finals"] = _pair(split_record(v1, "Finals"),
                               split_record(v2, "Finals"))
    values["l.b5"] = _pair(split_record(v1, "Best of 5"),
                           split_record(v2, "Best of 5"))

    # ---- I: derived ----
    def prof_num(view: PlayerView, key: str) -> Optional[float]:
        return _f(view.profile.get(key))

    values["i.rank_gap"] = _gap(prof_num(v1, "current_rank"), prof_num(v2, "current_rank"),
                                higher_better=False)
    values["i.elo_gap"] = _gap(prof_num(v1, "elo_rating"), prof_num(v2, "elo_rating"),
                               higher_better=True)

    def surface_winpct(view: PlayerView, surf: Optional[str]) -> Optional[float]:
        if not surf:
            return None
        row = view.split_row("career-splits", surf)
        if not row:
            return None
        return _f(view.col("career-splits", row, "Win%", fallback=5))

    values["i.surface_gap"] = _gap(
        surface_winpct(v1, surface1), surface_winpct(v2, surface2),
        higher_better=True, fmt="{:.1f}", unit="pp")

    def dr52(view: PlayerView) -> Optional[float]:
        row = view.split_row("last52-splits", surface or "Hard")
        if row is None:
            rows = view.rows("last52-splits")
            row = rows[0] if rows else None
        return _f(view.col("last52-splits", row, "DR", fallback=22)) if row else None

    values["i.dr_gap"] = _gap(dr52(v1), dr52(v2), higher_better=True, fmt="{:.2f}")
    values["i.form_gap"] = _gap(
        _f(f1.get("record", "").split("-")[0]) if f1.get("record") not in (None, MISSING) else None,
        _f(f2.get("record", "").split("-")[0]) if f2.get("record") not in (None, MISSING) else None,
        higher_better=True, fmt="{:.0f}", unit=" wins")
    values["i.h2h"] = values["g.record"]

    status = {}
    for side, view in (("p1", v1), ("p2", v2)):
        if not view.payload:
            status[side] = "missing"
        elif view.unresolved:
            status[side] = "unresolved"
        elif not view.available:
            status[side] = "empty"
        else:
            status[side] = "ok"
    return {"values": values, "stats_status": status,
            "surface": surface or None}


def build_rows(matches: list[dict], stats: dict[str, dict], form_n: int = 10) -> list[dict]:
    rows = []
    for m in matches:
        extra = compute_values(m, stats, form_n=form_n)
        rows.append({
            "match_id": str(m.get("match_id")),
            "player1": m.get("player1"),
            "player2": m.get("player2"),
            "live": bool(m.get("live")),
            "has_odds": bool(m.get("odds_json")),
            "values": extra["values"],
            "stats_status": extra["stats_status"],
        })
    return rows