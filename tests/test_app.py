"""Unit tests for the data layer: column computation, exporter, store.

Uses frozen real payloads (tests/fixtures/player_*.json) — no network.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from app import columns as col
from app import paths
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
    assert len(ids) == 127
    assert len(set(ids)) == len(ids), "column ids must be unique"
    assert [g.id for g in col.GROUPS] == list("ABCDEFGHJKL")
    # removed on request: no URL / market-count / implied % / main-line odds,
    # no "Events & finals" group, no derived comparison group
    for gone in ("a.url", "a.market_count", "a.pct", "a.games_ladder", "a.spread_ladder"):
        assert gone not in col.COLUMNS, gone
    assert not any("Events" in g.label for g in col.GROUPS)
    assert not any(cid.startswith("l.ev") for cid in col.COLUMNS)
    assert not any(cid.startswith("i.") for cid in col.COLUMNS)
    assert not any("Derived" in g.label for g in col.GROUPS)
    assert not any("gap" in c.label.lower() for c in col.COLUMNS.values())
    assert not any(c.hidden for g in col.GROUPS if g.id != "A" for c in g.columns)
    for g in col.GROUPS:
        assert g.columns, g.id
    # every group from PLAN.md §6 exists
    labels = {g.label for g in col.GROUPS}
    assert any("Match" in l for l in labels)
    assert any("Head-to-head" in l for l in labels)
    # hidden columns are still copyable but are not drawn as grid columns
    hidden = {c.id for c in col.COLUMNS.values() if c.hidden}
    assert hidden == {"a.tournament", "a.round", "a.start", "a.day"}, hidden


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
    # kick-off: Paris 12:00 of the official match day, on this machine's clock
    paris_noon = datetime.combine(col.match_date(MATCH), time(12, 0),
                                  tzinfo=col.OFFICIAL_TZ)
    assert v["a.start"] == paris_noon.astimezone().strftime("%H:%M")
    # day header: official date (Paris today here — MATCH is a same-day row)
    assert v["a.day"] == f"Today ({col.official_today().strftime('%d/%m/%Y')})"
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


def test_start_time_runs_on_the_local_clock():
    """Europe/Paris kick-off converted to the viewer's zone; 'in play' while live."""
    paris_noon = datetime.combine(col.match_date(MATCH), time(12, 0),
                                  tzinfo=col.OFFICIAL_TZ)
    assert col.start_display(MATCH) == paris_noon.astimezone().strftime("%H:%M")
    assert col.start_display(dict(MATCH, live=1)) == "in play"
    # unreadable / missing clock text is never invented
    assert col.start_display(dict(MATCH, start_local=None)) == "—"
    assert col.start_display(dict(MATCH, start_local="14:75")) == "14:75"
    assert col.start_display(dict(MATCH, start_local="in progress")) == "in progress"


def test_day_groups_by_official_day_not_by_local_date():
    """The day indicator is read on the scan day (Paris), so a stored row keeps
    its official date whatever the machine's clock or time zone says."""
    scan = dict(MATCH, scanned_at="2026-10-18T08:00:00")
    assert col.match_date(scan) == date(2026, 10, 18)                # same day
    assert col.match_date(dict(scan, day_indicator="demain")) == date(2026, 10, 19)
    # Sunday 18/10 + "mar." = Tuesday 20/10 (weekday maths anchored on the scan)
    assert col.match_date(dict(scan, day_indicator="mar.")) == date(2026, 10, 20)
    assert col.match_date(dict(scan, day_offset=3)) == date(2026, 10, 21)
    # the printed date is the official one, the name follows the official clock
    label = col.day_label(dict(scan, day_indicator="demain"))
    assert label.endswith("(19/10/2026)")
    delta = (date(2026, 10, 19) - col.official_today()).days
    assert label.split(" (")[0] == ("Today" if delta == 0 else
                                    "Tomorrow" if delta == 1 else f"{delta}d")


def test_h2h_against_known_opponent(stats):
    m = dict(MATCH, player2="Rafael Nadal")
    v = col.compute_values(m, stats)["values"]
    assert v["g.record"] == "31-29"
    assert v["g.last"] == "2024"
    assert v["g.meetings"] != "—"


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


def test_fetch_fault_is_error_status_not_unresolved():
    """A stored fetch failure (network) is retriable: it renders as `error`,
    never as `unresolved`, which is reserved for names TA does not have."""
    failed = {"player_name": "Marin Cilic", "player_id": None,
              "error": "ConnectTimeout: dns down", "profile": {}, "tables": {}}
    stats = {"Novak Djokovic": DJOKOVIC, "Marin Cilic": failed}
    m = dict(MATCH, player2="Marin Cilic")
    out = col.compute_values(m, stats)
    assert out["stats_status"] == {"p1": "ok", "p2": "error"}
    assert out["values"]["b.rank"] == "8 | —"


def test_build_rows_isolation_and_error_map(monkeypatch):
    """A crashing compute_values must not take the whole row set down: the
    bad match degrades to "—" + `error` status, the rest stays intact, and
    the error is reported in the map."""
    monkeypatch.setattr(col, "compute_values",
                        lambda m, stats, form_n=10: (_ for _ in ()).throw(
                            RuntimeError("boom")) if m.get("match_id") == "9"
                        else {"values": {"c.record": "1-0"}, "stats_status": {"p1": "ok", "p2": "ok"}})
    rows, errors = col.build_rows([MATCH, dict(MATCH, match_id="9",
                                              player1="A", player2="B")], {})
    assert errors == {"9": "RuntimeError: boom"}
    assert rows[0]["values"]["c.record"] == "1-0"
    assert rows[1]["stats_status"] == {"p1": "error", "p2": "error"}
    assert rows[1]["values"] == {}


def test_compute_values_isolates_a_crashing_player_payload():
    """A payload that crashes inside compute_values (e.g. a non-dict tables
    field) degrades that player to `parse` and keeps the rest of the row —
    the crash is caught by build_rows, which reports it in the error map."""
    good = {"player_name": "Novak Djokovic", "player_id": "NovakDjokovic",
            "profile": {"current_rank": 8}, "tables": {}}
    bad = {"player_name": "Iga Swiatek", "player_id": "IgaSwiatek",
           "profile": {}, "tables": "ab"}          # dict("ab") raises ValueError
    rows, errors = col.build_rows(
        [MATCH], {"Novak Djokovic": good, "Iga Swiatek": bad})
    assert set(errors) == {MATCH["match_id"]}
    assert rows[0]["stats_status"] == {"p1": "error", "p2": "error"}
    assert rows[0]["values"] == {}


def test_build_rows_returns_errors_tuple():
    rows, errors = col.build_rows([MATCH], {})
    assert isinstance(rows, list) and isinstance(errors, dict)


def test_live_and_day_labels():
    m = dict(MATCH, live=1, start_local=None)
    v = col.compute_values(m, {})["values"]
    assert v["a.start"] == "in play"
    fmt = "%d/%m/%Y"
    today = col.official_today()
    assert col.day_label({"day_indicator": "demain"}) == \
        f"Tomorrow ({(today + timedelta(days=1)).strftime(fmt)})"
    assert col.day_label({"day_offset": 0}) == f"Today ({today.strftime(fmt)})"
    assert col.day_label({"day_offset": 3}) == \
        f"+3d ({(today + timedelta(days=3)).strftime(fmt)})"


def test_outcome_detection():
    view = col.PlayerView(DJOKOVIC)
    rows = view.rows("recent-results")
    assert col._outcome(rows[0], view, "Novak Djokovic") == "L"   # Hurkacz d. Djokovic
    assert col._outcome(rows[1], view, "Novak Djokovic") == "W"   # Djokovic d. De Minaur


# ------------------------------------------------------------- exporter -----
def _rows(stats=None):
    matches = [MATCH, dict(MATCH, match_id="2", player1="Ann Li",
                           player2="Iga Swiatek")]
    rows, _ = col.build_rows(matches, stats if stats is not None else {}, form_n=10)
    return rows


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
    rows, _ = col.build_rows([dict(MATCH, odds_json=ODDS_JSON)], stats, form_n=10)
    cells = {"75620252": ["a.tournament", "a.games", "b.rank"]}
    text = export_text(rows, cells, "llm")
    lines = text.splitlines()

    assert lines[0].startswith("# Tennis matches — ")
    assert "Reading guide:" in text
    assert "## 1. Novak Djokovic vs Iga Swiatek" in lines
    # fields grouped under the catalogue groups, in catalogue order
    groups = [ln for ln in lines if ln.startswith("### ")]
    assert groups == ["### A. Match (Winamax)",
                      "### B. Player identity (Tennis Abstract)"], groups
    # one labelled line per field; pairs expanded with real player names
    assert "- Tournament: ATP - Shanghai" in lines
    # newlines are flattened by _clean(): one "Over X: odd" run, then Under
    assert ("- Total games: Over 21.5: 1.54 Over 22.5: 1.78 Over 23.5: 1.98  "
            "Under 21.5: 2.00 Under 22.5: 1.78 Under 23.5: 1.58") in lines
    assert "- Current rank: Novak Djokovic 8, Iga Swiatek 9" in lines
    # the removed "Derived comparisons" group never leaks into an export
    assert "Rank gap" not in text and "Elo gap" not in text
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


def test_store_clear_downloaded_keeps_selection(tmp_path):
    """'Clear data' removes what was fetched, never the user's own choices."""
    s = Store(tmp_path / "t.db")
    s.upsert_matches([MATCH, dict(MATCH, match_id="9", player1="A", player2="B")])
    s.set_odds(MATCH["match_id"], {"sectionCount": 3, "oddCount": 42,
                                   "buttonCount": 44, "sections": []})
    s.upsert_stats("Novak Djokovic", DJOKOVIC)
    s.save_selection({"75620252": ["a.tournament"]}, "llm")

    assert s.clear_downloaded() == {"matches": 2, "stats": 1,
                                    "players": ["Novak Djokovic"]}
    assert s.list_matches() == []
    assert s.get_stats() == {}
    assert s.matches_needing_odds() == []
    sel = s.get_selection()
    assert sel["cells"] == {"75620252": ["a.tournament"]}
    assert sel["format"] == "llm"
    # idempotent: a second clear reports nothing left
    assert s.clear_downloaded() == {"matches": 0, "stats": 0, "players": []}
    s.close()


def test_store_clear_downloaded_scoped_keeps_the_other_lines(tmp_path):
    """'Clear data' on selected lines: those rows go, the rest is untouched —
    including the stats of a player an unselected match still needs."""
    s = Store(tmp_path / "t.db")
    selected = dict(MATCH, match_id="75620252")              # Djokovic / Swiatek
    shared = dict(MATCH, match_id="9", player1="Novak Djokovic",
                  player2="Alexander Zverev")                 # keeps Djokovic
    lone = dict(MATCH, match_id="11", player1="Daniil Medvedev",
                player2="Tommy Paul")
    s.upsert_matches([selected, shared, lone])
    odds = {"sectionCount": 3, "oddCount": 42, "buttonCount": 44, "sections": []}
    s.set_odds("75620252", odds)
    s.set_odds("11", odds)
    s.upsert_stats("Novak Djokovic", DJOKOVIC)
    s.upsert_stats("Iga Swiatek", SWIATEK)
    s.upsert_stats("Daniil Medvedev", {"player_name": "Daniil Medvedev",
                                       "player_id": "DaniilMedvedev",
                                       "profile": {}, "tables": {}})
    s.save_selection({"75620252": ["a.tournament"]}, "llm")

    out = s.clear_downloaded(["75620252", "unknown-id"])

    # one row gone (the unknown id clears nothing), one stats row gone
    assert out["matches"] == 1
    assert out["stats"] == 1
    assert out["players"] == ["Iga Swiatek"]     # Djokovic still plays match 9
    assert {m["match_id"] for m in s.list_matches()} == {"9", "11"}
    assert s.get_match("75620252") is None
    kept = s.get_match("11")
    assert kept and kept["odds_json"], "an unselected match keeps its odds"
    assert set(s.get_stats()) == {"Novak Djokovic", "Daniil Medvedev"}
    # the user's own state never goes with the downloads
    sel = s.get_selection() or {}
    assert sel.get("cells") == {"75620252": ["a.tournament"]}
    assert sel.get("format") == "llm"
    # an empty / unknown scope is a clean no-op, never "clear everything"
    assert s.clear_downloaded([]) == {"matches": 0, "stats": 0, "players": []}
    assert s.clear_downloaded(["nope"]) == {"matches": 0, "stats": 0, "players": []}
    assert len(s.list_matches()) == 2
    s.close()


def test_clear_cache_removes_the_tree_but_keeps_the_directory(tmp_path):
    day = tmp_path / "2026-10-19"
    day.mkdir()
    (day / "djokovic.json").write_text("{}", encoding="utf-8")
    (tmp_path / "swiatek.json").write_text("0123456789", encoding="utf-8")

    out = paths.clear_cache(tmp_path)
    assert out == {"files": 2, "bytes": 12, "failed": 0}
    # the cache root survives (the job manager holds its path), the day
    # subdirectories are gone with their payloads
    assert tmp_path.is_dir()
    assert list(tmp_path.rglob("*")) == []
    assert paths.clear_cache(tmp_path) == {"files": 0, "bytes": 0, "failed": 0}


def test_clear_cache_players_drops_only_the_named_pages(tmp_path):
    """Scoped 'Clear data': the cached pages of the cleared players go, the
    pages of the players still on the grid stay. Files are matched on the
    payload's ``player_name`` — the same key ``load_cached_by_name`` reads
    back — so an id-derived and an ``unresolved-`` file are both found."""
    day, older = tmp_path / "2026-10-19", tmp_path / "2026-10-16"
    day.mkdir(), older.mkdir()

    def page(name: str, player_id) -> str:
        return json.dumps({"player_name": name, "player_id": player_id,
                           "fetched_at": "2026-10-19T10:00:00"})

    (day / "NovakDjokovic.json").write_text(page("Novak Djokovic", "NovakDjokovic"),
                                            encoding="utf-8")
    (day / "IgaSwiatek.json").write_text(page("Iga Swiatek", "IgaSwiatek"),
                                         encoding="utf-8")
    (older / "unresolved-Zhang_Zhizhen.json").write_text(page("Zhang Zhizhen", None),
                                                         encoding="utf-8")
    (day / "junk.json").write_text("not json", encoding="utf-8")

    out = paths.clear_cache_players(tmp_path, ["igA Swiatek", "Zhang Zhizhen"])  # case-folded

    assert out["failed"] == 0
    assert out["bytes"] == len(page("Iga Swiatek", "IgaSwiatek")) + \
        len(page("Zhang Zhizhen", None))
    assert out["files"] == 2
    # the untouched player and the unreadable payload survive; the day dir
    # left empty by the clear is dropped, the one still in use is not
    assert sorted(p.name for p in tmp_path.rglob("*") if p.is_file()) == \
        ["NovakDjokovic.json", "junk.json"]
    assert day.is_dir()
    assert not older.is_dir()
    # an unknown / empty scope is a clean no-op
    assert paths.clear_cache_players(tmp_path, ["Nobody Here", ""]) == \
        {"files": 0, "bytes": 0, "failed": 0}
