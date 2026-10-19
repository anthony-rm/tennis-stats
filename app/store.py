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
    def clear_downloaded(self) -> dict:
        """Drop every downloaded row (scan result, odds, stored player stats).

        The store half of the dashboard's "Clear data": selections are the
        user's own choices, never downloads, so they are kept untouched.
        Returns the removed row counts (``{"matches", "stats"}``).
        """
        with self._conn:
            matches = self._conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
            stats = self._conn.execute("SELECT COUNT(*) FROM stats").fetchone()[0]
            self._conn.execute("DELETE FROM matches")
            self._conn.execute("DELETE FROM stats")
        try:
            self._conn.execute("VACUUM")   # hand the file space back
        except sqlite3.OperationalError:
            pass
        return {"matches": matches, "stats": stats}

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
