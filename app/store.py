"""SQLite persistence: matches (Winamax), stats (Tennis Abstract), selections.

Schema notes (PLAN.md §5):
  * matches matches the plan's record exactly (odds_json holds the full
    OddsJSON from the odds pass);
  * stats.player_key is the *player name* (not the TA id) because match rows
    reference players by name and unresolved players have no TA id — the
    payload_json still carries player_id / unresolved / tried / error
    (documented deviation, see README).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from app.paths import data_dir

DB_PATH = data_dir() / "tennis_stats.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    match_id      TEXT PRIMARY KEY,
    url           TEXT,
    tournament    TEXT,
    round         TEXT,
    player1       TEXT,
    player2       TEXT,
    start_local   TEXT,
    day_offset    INTEGER DEFAULT 0,
    day_indicator TEXT,
    live          INTEGER DEFAULT 0,
    odd_p1        REAL,
    odd_p2        REAL,
    pct_p1        REAL,
    pct_p2        REAL,
    scanned_at    TEXT,
    odds_json     TEXT,
    odds_fetched_at TEXT,
    section_count INTEGER,
    odd_count     INTEGER,
    market_count  INTEGER
);
CREATE TABLE IF NOT EXISTS stats (
    player_key    TEXT PRIMARY KEY,
    fetched_at    TEXT,
    payload_json  TEXT
);
CREATE TABLE IF NOT EXISTS selections (
    name        TEXT PRIMARY KEY,
    cells_json  TEXT,
    format      TEXT DEFAULT 'tsv',
    updated_at  TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Store:
    def __init__(self, db_path: Path | str = DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---------------------------------------------------------- matches ---
    def upsert_matches(self, rows: Iterable[dict]) -> int:
        """Insert/refresh the scan result. Existing odds are preserved; matches
        absent from the new scan are removed (the scan is authoritative).

        An EMPTY scan never deletes: a blank result means the list did not
        render (slow SPA / WAF hiccup), and dropping the rows would also throw
        away every stored odds pass.
        """
        rows = list(rows)
        if not rows:
            return 0
        now = datetime.now().isoformat(timespec="seconds")
        ids = []
        with self._conn:
            for m in rows:
                # accepts both the core's camelCase scan keys and snake_case DB keys
                def pick(camel: str, snake: str, default=None):
                    value = m.get(camel)
                    if value is None:
                        value = m.get(snake)
                    return default if value is None else value
                mid = str(m.get("matchId") or m.get("match_id") or "")
                if not mid:
                    continue
                ids.append(mid)
                self._conn.execute(
                    """
                    INSERT INTO matches (match_id, url, tournament, round, player1, player2,
                        start_local, day_offset, day_indicator, live, odd_p1, odd_p2,
                        pct_p1, pct_p2, scanned_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(match_id) DO UPDATE SET
                        url=excluded.url, tournament=excluded.tournament, round=excluded.round,
                        player1=excluded.player1, player2=excluded.player2,
                        start_local=excluded.start_local, day_offset=excluded.day_offset,
                        day_indicator=excluded.day_indicator, live=excluded.live,
                        odd_p1=excluded.odd_p1, odd_p2=excluded.odd_p2,
                        pct_p1=excluded.pct_p1, pct_p2=excluded.pct_p2,
                        scanned_at=excluded.scanned_at
                    """,
                    (
                        mid, m.get("url") or "", m.get("tournament") or "", m.get("round") or "",
                        m.get("player1") or "", m.get("player2") or "",
                        pick("matchTime", "start_local") or None,
                        int(pick("dayOffset", "day_offset", 0) or 0),
                        pick("dayIndicator", "day_indicator") or "",
                        1 if m.get("live") else 0,
                        pick("oddP1", "odd_p1"), pick("oddP2", "odd_p2"),
                        pick("pctP1", "pct_p1"), pick("pctP2", "pct_p2"),
                        now,
                    ),
                )
            if ids:
                placeholders = ",".join("?" * len(ids))
                self._conn.execute(
                    f"DELETE FROM matches WHERE match_id NOT IN ({placeholders})", ids
                )
            else:
                self._conn.execute("DELETE FROM matches")
        return len(ids)

    def list_matches(self) -> list[dict]:
        cur = self._conn.execute("SELECT * FROM matches ORDER BY live DESC, day_offset, start_local, tournament")
        return [dict(r) for r in cur.fetchall()]

    def get_match(self, match_id: str) -> Optional[dict]:
        cur = self._conn.execute("SELECT * FROM matches WHERE match_id=?", (match_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def matches_needing_odds(self) -> list[dict]:
        return [m for m in self.list_matches() if not m.get("odds_json") and not m.get("live")]

    def set_odds(self, match_id: str, odds: dict) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._conn:
            self._conn.execute(
                """
                UPDATE matches SET odds_json=?, odds_fetched_at=?,
                    section_count=?, odd_count=?, market_count=?
                WHERE match_id=?
                """,
                (
                    json.dumps(odds, ensure_ascii=False),
                    now,
                    int(odds.get("sectionCount") or 0),
                    int(odds.get("oddCount") or 0),
                    int(odds.get("buttonCount") or 0),
                    match_id,
                ),
            )

    # ----------------------------------------------------------- stats ---
    def upsert_stats(self, player_key: str, payload: dict) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO stats (player_key, fetched_at, payload_json)
                VALUES (?,?,?)
                ON CONFLICT(player_key) DO UPDATE SET
                    fetched_at=excluded.fetched_at, payload_json=excluded.payload_json
                """,
                (
                    player_key,
                    payload.get("fetched_at") or datetime.now().isoformat(timespec="seconds"),
                    json.dumps(payload, ensure_ascii=False),
                ),
            )

    def get_stats(self) -> dict[str, dict]:
        cur = self._conn.execute("SELECT player_key, payload_json FROM stats")
        out: dict[str, dict] = {}
        for r in cur.fetchall():
            try:
                out[r["player_key"]] = json.loads(r["payload_json"])
            except (ValueError, TypeError):
                continue
        return out

    # ----------------------------------------------------------- clear ----
    @staticmethod
    def _chunks(values: list[str], size: int = 500) -> Iterable[list[str]]:
        """Batches for an IN (...) clause (well under SQLite's 999-variable cap)."""
        for i in range(0, len(values), size):
            yield values[i:i + size]

    def clear_downloaded(self, match_ids: Optional[Iterable[str]] = None) -> dict:
        """Drop downloaded rows — every one of them, or only the given matches.

        With ``match_ids`` this is the line-scoped "Clear data": those matches
        go (row + odds) together with the stats of their players **that no
        remaining match still needs**, so a player shared with an unselected
        line keeps his data and the scoped clear never blanks a line it was
        not asked to touch. Unknown ids clear nothing (an empty scope is a
        no-op, never "clear everything"). Without ``match_ids`` every
        downloaded row goes, as before.

        Selections are the user's own choices, never downloads, so they are
        kept untouched in both cases. Returns the removed row counts and the
        names of the players that lost their stats (``{"matches", "stats",
        "players"}``) — the caller drops their cached pages by name too, or a
        re-fetch would silently restore them from the 24 h cache.
        """
        with self._conn:
            if match_ids is None:
                matches = self._conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
                rows = self._conn.execute("SELECT player_key, payload_json FROM stats").fetchall()
                stats = len(rows)
                players = [r["player_key"] for r in rows]
                self._conn.execute("DELETE FROM matches")
                self._conn.execute("DELETE FROM stats")
            else:
                wanted = list(dict.fromkeys(str(m).strip() for m in match_ids if str(m).strip()))
                if not wanted:
                    return {"matches": 0, "stats": 0, "players": []}
                all_rows = self.list_matches()
                stored = {str(m["match_id"]) for m in all_rows}
                targets = [m for m in wanted if m in stored]
                if not targets:
                    return {"matches": 0, "stats": 0, "players": []}
                target_set = set(targets)
                scoped = {p for m in all_rows if str(m["match_id"]) in target_set
                          for p in (m.get("player1"), m.get("player2")) if p}
                still = {p for m in all_rows if str(m["match_id"]) not in target_set
                         for p in (m.get("player1"), m.get("player2")) if p}
                # players still listed elsewhere keep their stats (their other
                # line was not selected); only the rest goes
                players = sorted(scoped - still)
                stats = 0
                for chunk in self._chunks(targets):
                    q = ",".join("?" * len(chunk))
                    self._conn.execute(
                        f"DELETE FROM matches WHERE match_id IN ({q})", chunk)
                for chunk in self._chunks(players):
                    q = ",".join("?" * len(chunk))
                    cur = self._conn.execute(
                        f"DELETE FROM stats WHERE player_key IN ({q})", chunk)
                    stats += cur.rowcount
                matches = len(targets)
        try:
            self._conn.execute("VACUUM")   # hand the file space back
        except sqlite3.OperationalError:
            pass
        return {"matches": matches, "stats": stats, "players": players}

    # ------------------------------------------------------ settings ----
    # Cooldown between scrapes, per source, in seconds. These feed the job
    # loops (Winamax odds delay_range, Tennis Abstract inter-player delay) so
    # the user can slow a scrape down to stay under a WAF / polite-website
    # threshold without editing code.
    DEFAULT_SETTINGS = {
        "cooldown_winamax": 1.5,
        "cooldown_ta": 1.6,
    }

    def get_settings(self) -> dict:
        out = dict(self.DEFAULT_SETTINGS)
        cur = self._conn.execute("SELECT key, value FROM settings")
        for key, value in cur.fetchall():
            if key in out:
                try:
                    out[key] = float(value)
                except (TypeError, ValueError):
                    continue
        return out

    def set_settings(self, settings: dict) -> dict:
        """Persist the settings the caller sent (unknown keys are ignored).

        Returns the full settings map after the write, so the UI can echo the
        canonical (clamped) values back.
        """
        with self._conn:
            for key in self.DEFAULT_SETTINGS:
                if key not in settings:
                    continue
                try:
                    value = float(settings[key])
                except (TypeError, ValueError):
                    continue
                if value < 0.1 or value > 120:
                    continue
                self._conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, f"{value:.3g}"),
                )
        return self.get_settings()

    # ------------------------------------------------------ selections ---
    def get_selection(self, name: str = "default") -> Optional[dict]:
        cur = self._conn.execute(
            "SELECT cells_json, format FROM selections WHERE name=?", (name,)
        )
        row = cur.fetchone()
        if not row:
            return None
        try:
            cells = json.loads(row["cells_json"])
        except (ValueError, TypeError):
            cells = {}
        return {"name": name, "cells": cells, "format": row["format"] or "tsv"}

    def save_selection(self, cells: dict, fmt: str = "tsv", name: str = "default") -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO selections (name, cells_json, format, updated_at)
                VALUES (?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET
                    cells_json=excluded.cells_json, format=excluded.format,
                    updated_at=excluded.updated_at
                """,
                (name, json.dumps(cells, ensure_ascii=False), fmt,
                 datetime.now().isoformat(timespec="seconds")),
            )
