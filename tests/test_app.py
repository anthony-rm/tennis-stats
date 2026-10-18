"""Unit tests for the data layer: column computation, exporter, store.

Uses frozen real payloads (tests/fixtures/player_*.json) — no network.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from app import columns as col
from app.exporter import export_text
from app.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


def _payload(name: str) -> dict:
    return json.loads((FIXTURES / f"player_{name}.json").read_text(encoding="utf-8"))


DJOKOVIC = _payload("djokovic")
SWIATEK = _payload("swiatek")

MATCH = {
    "match_id": "75620252",
    "url": "https://www.winamax.fr/paris-sportifs/match/75620252",
    "tournament": "ATP - Shanghai",
    "round": "1/4",
    "player1": "Novak Djokovic",
    "player2": "Iga Swiatek",
    "start_local": "12:00",
    "day_offset": 0,
    "day_indicator": "",
    "live": 0,
    "odd_p1": 1.5,
    "odd_p2": 2.5,
    "pct_p1": 65.0,
    "pct_p2": 35.0,
    "market_count": 145,
}


@pytest.fixture()
def stats() -> dict:
    return {"Novak Djokovic": DJOKOVIC, "Iga Swiatek": SWIATEK}


# ------------------------------------------------------------- catalogue ---
def test_catalogue_shape():
    ids = col.COLUMN_ORDER
    assert len(ids) == 133
    assert len(set(ids)) == len(ids), "column ids must be unique"
    assert [g.id for g in col.GROUPS] == list("ABCDEFGHIJKL")
    # removed on request: no URL / market-count / implied % / main-line odds,
    # no "Events & finals" group
    for gone in ("a.url", "a.market_count", "a.pct", "a.games_ladder", "a.spread_ladder"):
        assert gone not in col.COLUMNS, gone
    assert not any("Events" in g.label for g in col.GROUPS)
    assert not any(cid.startswith("l.ev") for cid in col.COLUMNS)
    # hidden columns are still copyable but are not drawn as grid columns
    hidden = {c.id for c in col.COLUMNS.values() if c.hidden}
    assert hidden == {"a.tournament", "a.round", "a.start", "a.day"}, hidden
    assert not any(c.hidden for g in col.GROUPS if g.id != "A" for c in g.columns)
    for g in col.GROUPS:
        assert g.columns, g.id
    # every group from PLAN.md §6 exists
    labels = {g.label for g in col.GROUPS}
    assert any("Match" in l for l in labels)
    assert any("Derived" in l for l in labels)


# -------------------------------------------------------- compute_values ----
def test_every_column_is_computed(stats):
    """The catalogue is the contract: compute_values must fill every id."""
    v = col.compute_values(MATCH, stats, form_n=10)["values"]
    assert set(v) == set(col.COLUMN_ORDER)
    # ... and nothing beyond it
    assert set(col.COLUMNS) == set(col.COLUMN_ORDER)


# Winamax 'Nombre de jeux' / 'Écart de jeux' sections (trimmed real payload)
ODDS_JSON = json.dumps({
    "url": "https://www.winamax.fr/paris-sportifs/match/75620252",
    "players": ["Medvedev", "Paul"],
    "sections": [
        {"title": "Vainqueur", "rows": [
            {"t": "row", "label": "D. Medvedev", "value": "1.41"},
            {"t": "row", "label": "T. Paul", "value": "2.90"}]},
        {"title": "Nombre de jeux", "rows": [
            {"t": "row", "label": "Over 21,5 jeux", "value": "1.54"},
            {"t": "row", "label": "Over 22,5 jeux", "value": "1.78"},
            {"t": "row", "label": "Over 23,5 jeux", "value": "1.98"},
            {"t": "row", "label": "Under 23,5 jeux", "value": "1.58"},
            {"t": "row", "label": "Under 22,5 jeux", "value": "1.78"},
            {"t": "row", "label": "Under 21,5 jeux", "value": "2.00"}]},
        {"title": "1er set - Nombre de jeux", "rows": [
            {"t": "row", "label": "Over 9,5 jeux", "value": "1.60"}]},
        {"title": "Écart de jeux", "rows": [
            {"t": "row", "label": "D. Medvedev -2.5", "value": "1.52"},
            {"t": "row", "label": "D. Medvedev -3.5", "value": "1.78"},
            {"t": "row", "label": "D. Medvedev -4.5", "value": "2.25"},
            {"t": "row", "label": "T. Paul +4.5", "value": "1.38"},
            {"t": "row", "label": "T. Paul +3.5", "value": "1.68"},
            {"t": "row", "label": "T. Paul +2.5", "value": "1.96"}]},
    ],
    "sectionCount": 4, "oddCount": 20, "buttonCount": 20,
}, ensure_ascii=False)


def test_games_odds_columns():
    m = dict(MATCH, odds_json=ODDS_JSON)
    v = col.compute_values(m, {})["values"]
    # one line per odd: Over block first, then Under (blank line between them)
    assert v["a.games"] == (
        "Over 21.5: 1.54\n"
        "Over 22.5: 1.78\n"
        "Over 23.5: 1.98\n"
        "\n"
        "Under 21.5: 2.00\n"
        "Under 22.5: 1.78\n"
        "Under 23.5: 1.58"
    )
    # one line per odd, surname on every line, grouped by player
    assert v["a.spread"] == (
        "Medvedev -4.5: 2.25\n"
        "Medvedev -3.5: 1.78\n"
        "Medvedev -2.5: 1.52\n"
        "Paul +2.5: 1.96\n"
        "Paul +3.5: 1.68\n"
        "Paul +4.5: 1.38"
    )
    # sub-page sections ('1er set - …') must not leak into the match cells
    assert "9.5" not in v["a.games"]


def test_games_odds_missing_without_odds():
    v = col.compute_values(MATCH, {})["values"]
    assert v["a.games"] == "—"
    assert v["a.spread"] == "—"


def test_match_columns(stats):
    v = col.compute_values(MATCH, stats)["values"]
    assert v["a.tournament"] == "ATP - Shanghai"
    assert v["a.round"] == "1/4"
    assert v["a.start"] == "12:00"
    assert v["a.day"] == f"Today ({date.today().strftime('%d/%m/%Y')})"
    assert v["a.odds"] == "1.5 | 2.5"
    assert "a.pct" not in v
    # a.games / a.spread stay empty until the odds pass ran
    assert v["a.games"] == "—"
    assert v["a.spread"] == "—"


def test_identity_columns(stats):
    v = col.compute_values(MATCH, stats)["values"]
    assert v["b.rank"] == "8 | 9"
    assert v["b.peak"] == "1 | 1"
    assert v["b.elo"] == "2074 | 2099"
    assert v["b.hand"] == "R | R"
    assert v["b.country"] == "SRB | POL"
    assert v["b.age"] == "39 | 25"


def test_form_and_season_columns(stats):
    out = col.compute_values(MATCH, stats, form_n=10)
    v = out["values"]
    assert out["stats_status"] == {"p1": "ok", "p2": "ok"}
    for cid in ("c.record", "c.last_date", "c.last_opp", "c.surface", "c.dr",
                "c.s1_in", "c.s1_won", "c.s2_won", "c.aces", "c.dfs",
                "c.bps", "c.duration"):
        assert v[cid] not in ("", None), cid
    # "W-L | W-L" shape
    w1, l1 = v["c.record"].split(" | ")[0].split("-")
    assert int(w1) >= 0 and int(l1) >= 0
    assert v["c.last_date"].startswith("07-Oct-2026 | ")   # Djokovic's latest match
    assert v["c.last_opp"].startswith(("W ", "L ", "? "))
    # last-match outcome must match the opponent cell ("Hurkacz d. Djokovic" = L)
    assert v["c.last_opp"].startswith("L ")
    # season from the current-year tour-years row
    expected = []
    for payload in (DJOKOVIC, SWIATEK):
        row = next(r for r in payload["tables"]["tour-years"]["rows"]
                   if str(r[0]) == "2026")
        expected.append(f"{int(row[2])}-{int(row[3])}")
    assert v["e.record"] == " | ".join(expected)


def test_surface_inference_and_splits(stats):
    v = col.compute_values(MATCH, stats)["values"]
    # Djokovic already played Shanghai this month -> exact event row = Hard
    assert col._surface_for_match(col.PlayerView(DJOKOVIC), "ATP - Shanghai") == "Hard"
    # career split on the inferred surface renders "W-L | W-L"
    for side in v["d.record"].split(" | "):
        assert side.count("-") == 1
        w, l = side.split("-")
        assert w.isdigit() and l.isdigit()
    assert v["i.surface_gap"].split(" ")[0] in {"P1", "P2", "even"}


def test_derived_gaps(stats):
    v = col.compute_values(MATCH, stats)["values"]
    assert v["i.rank_gap"] == "P1 +1"        # rank 8 vs 9, lower is better
    assert v["i.elo_gap"] == "P2 +25"        # 2074 vs 2099, higher is better
    assert v["i.form_gap"].endswith("wins") or v["i.form_gap"] == "even"
    assert v["i.h2h"] in {"never"} or "-" in v["i.h2h"]


def test_h2h_against_known_opponent(stats):
    m = dict(MATCH, player2="Rafael Nadal")
    v = col.compute_values(m, stats)["values"]
    assert v["g.record"] == "31-29"
    assert v["g.last"] == "2024"
    assert v["i.h2h"] == "31-29"


def test_unresolved_and_missing_players():
    unresolved = {"player_name": "Zhang Zhizhen", "player_id": None,
                  "unresolved": True, "profile": {}, "tables": {}}
    stats = {"Novak Djokovic": DJOKOVIC, "Zhang Zhizhen": unresolved}
    m = dict(MATCH, player2="Zhang Zhizhen")
    out = col.compute_values(m, stats)
    assert out["stats_status"] == {"p1": "ok", "p2": "unresolved"}
    assert out["values"]["b.rank"] == "8 | —"
    assert out["values"]["c.record"].endswith(" | —")

    out2 = col.compute_values(MATCH, {})
    assert out2["stats_status"] == {"p1": "missing", "p2": "missing"}
    assert out2["values"]["b.rank"] == "— | —"
    # match columns still work without stats
    assert out2["values"]["a.odds"] == "1.5 | 2.5"


def test_live_and_day_labels():
    m = dict(MATCH, live=1, start_local=None)
    v = col.compute_values(m, {})["values"]
    assert v["a.start"] == "in play"
    fmt = "%d/%m/%Y"
    assert col.day_label({"day_indicator": "demain"}) == \
        f"Tomorrow ({(date.today() + timedelta(days=1)).strftime(fmt)})"
    assert col.day_label({"day_offset": 0}) == f"Today ({date.today().strftime(fmt)})"
    assert col.day_label({"day_offset": 3}) == \
        f"+3d ({(date.today() + timedelta(days=3)).strftime(fmt)})"


def test_outcome_detection():
    view = col.PlayerView(DJOKOVIC)
    rows = view.rows("recent-results")
    assert col._outcome(rows[0], view, "Novak Djokovic") == "L"   # Hurkacz d. Djokovic
    assert col._outcome(rows[1], view, "Novak Djokovic") == "W"   # Djokovic d. De Minaur


# ------------------------------------------------------------- exporter -----
def _rows(stats=None):
    matches = [MATCH, dict(MATCH, match_id="2", player1="Ann Li",
                           player2="Iga Swiatek")]
    return col.build_rows(matches, stats if stats is not None else {}, form_n=10)


def test_export_tsv():
    rows = _rows()
    cells = {"75620252": ["a.tournament", "b.rank"], "2": ["a.tournament"]}
    text = export_text(rows, cells, "tsv")
    lines = text.rstrip("\n").split("\n")
    # column order follows the catalogue, not the selection order
    assert lines[0] == "Match\tTournament\tCurrent rank"
    assert lines[1].startswith("Novak Djokovic vs Iga Swiatek\tATP - Shanghai\t")
    # second row selected only Tournament -> empty cell for Current rank
    assert lines[2] == "Ann Li vs Iga Swiatek\tATP - Shanghai\t"
    assert len(lines) == 3


def test_export_skips_unselected_rows_and_columns():
    rows = _rows()
    text = export_text(rows, {"2": ["a.round"]}, "tsv")
    lines = text.rstrip("\n").split("\n")
    assert lines[0] == "Match\tRound"
    assert len(lines) == 2
    assert lines[1] == "Ann Li vs Iga Swiatek\t1/4"


def test_export_markdown_and_blocks():
    rows = _rows()
    cells = {"75620252": ["b.rank", "c.record"]}
    md = export_text(rows, cells, "md")
    assert md.startswith("| Match | Current rank | Last N record |")
    assert "|---|" in md
    block = export_text(rows, cells, "md_block")
    assert "## Novak Djokovic vs Iga Swiatek" in block
    assert "| Current rank |" in block


def test_export_llm_format(stats):
    rows = col.build_rows([dict(MATCH, odds_json=ODDS_JSON)], stats, form_n=10)
    cells = {"75620252": ["a.tournament", "a.games", "b.rank", "i.rank_gap"]}
    text = export_text(rows, cells, "llm")
    lines = text.splitlines()

    assert lines[0].startswith("# Tennis matches — ")
    assert "Reading guide:" in text
    assert "## 1. Novak Djokovic vs Iga Swiatek" in lines
    # fields grouped under the catalogue groups, in catalogue order
    groups = [ln for ln in lines if ln.startswith("### ")]
    assert groups == ["### A. Match (Winamax)",
                      "### B. Player identity (Tennis Abstract)",
                      "### I. Derived comparisons"], groups
    # one labelled line per field; pairs expanded with real player names
    assert "- Tournament: ATP - Shanghai" in lines
    # newlines are flattened by _clean(): one "Over X: odd" run, then Under
    assert ("- Total games: Over 21.5: 1.54 Over 22.5: 1.78 Over 23.5: 1.98  "
            "Under 21.5: 2.00 Under 22.5: 1.78 Under 23.5: 1.58") in lines
    assert "- Current rank: Novak Djokovic 8, Iga Swiatek 9" in lines
    # gap columns name the leading player instead of "P1"/"P2"
    assert "- Rank gap: Novak Djokovic +1" in lines
    # only selected fields are exported (no tables, no unselected data)
    assert "Season W-L" not in text
    assert "| Current rank |" not in text
    # llm is the default format
    assert export_text(rows, cells) == text
    assert export_text(rows, cells, "llm") == text


def test_export_json_and_empty():
    rows = _rows()
    data = json.loads(export_text(rows, {"75620252": ["a.round"]}, "json"))
    assert data[0]["match_id"] == "75620252"
    assert data[0]["values"]["a.round"] == "1/4"
    assert export_text(rows, {}, "tsv") == ""
    assert export_text([], {"x": ["a.round"]}, "tsv") == ""


# ---------------------------------------------------------------- store -----
def test_store_upsert_preserves_odds(tmp_path):
    db = tmp_path / "t.db"
    s = Store(db)
    s.upsert_matches([MATCH])
    s.set_odds(MATCH["match_id"], {"sectionCount": 3, "oddCount": 42,
                                   "buttonCount": 44, "sections": []})
    # re-scan (same match, odds changed on the list) must keep stored odds
    s.upsert_matches([dict(MATCH, odd_p1=1.6, odd_p2=2.4),
                      dict(MATCH, match_id="9", player1="A", player2="B")])
    rows = {m["match_id"]: m for m in s.list_matches()}
    assert rows["75620252"]["odds_json"], "odds must survive a re-scan"
    assert rows["75620252"]["odd_p1"] == 1.6
    assert rows["75620252"]["market_count"] == 44
    assert set(rows) == {"75620252", "9"}, "matches absent from the scan are pruned"
    # matches needing odds: only ones without odds_json and not live
    need = {m["match_id"] for m in s.matches_needing_odds()}
    assert need == {"9"}
    s.close()


def test_store_empty_scan_keeps_matches(tmp_path):
    """A scan that returns nothing must not wipe rows (nor their odds)."""
    s = Store(tmp_path / "t.db")
    s.upsert_matches([MATCH])
    s.set_odds(MATCH["match_id"], {"sectionCount": 3, "oddCount": 42,
                                   "buttonCount": 44, "sections": []})
    assert s.upsert_matches([]) == 0
    rows = s.list_matches()
    assert [r["match_id"] for r in rows] == [MATCH["match_id"]]
    assert rows[0]["odds_json"], "stored odds must survive an empty scan"
    s.close()


def test_store_stats_and_selection(tmp_path):
    s = Store(tmp_path / "t.db")
    s.upsert_stats("Novak Djokovic", DJOKOVIC)
    stats = s.get_stats()
    assert stats["Novak Djokovic"]["profile"]["current_rank"] == 8

    assert s.get_selection() is None
    s.save_selection({"m1": ["a.round"]}, "md")
    sel = s.get_selection()
    assert sel["cells"] == {"m1": ["a.round"]}
    assert sel["format"] == "md"
    s.save_selection({"m2": ["b.rank"]}, "tsv")   # upsert by name
    assert s.get_selection()["cells"] == {"m2": ["b.rank"]}
    s.close()
