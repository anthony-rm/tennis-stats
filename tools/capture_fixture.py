#!/usr/bin/env python3
"""Refresh tests/fixtures snapshots from the live Winamax site.

Runs a full-page scan (no night gap), captures:
    tests/fixtures/list_page.html     — after the scan
    tests/fixtures/match_page.html    — top of the first upcoming match page
and prints the odds counts of up to `--odds N` matches (also a handy live
smoke test of the odds pass).

Usage:  .venv/bin/python tools/capture_fixture.py [--odds 3]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.winamax import WinamaxSession  # noqa: E402

FIXTURES = PROJECT_ROOT / "tests" / "fixtures"


async def main(odds_count: int) -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    async with WinamaxSession(on_event=lambda m: print("[wx]", m, flush=True)) as wx:
        matches = await wx.scan_today(include_live=True, stop_at_night_gap=False)
        print(f"[scan] full-page: {len(matches)} matches")
        (FIXTURES / "list_page.html").write_text(await wx.page.content(), encoding="utf-8")
        for m in matches:
            print("  ", "LIVE " if m["live"] else "     ",
                  f"+{m['dayOffset']}d", m["matchTime"] or "--:--",
                  m["dayIndicator"] or "-", m["tournament"],
                  "|", m["player1"], "vs", m["player2"])

        upcoming = [m for m in matches if not m["live"] and m["matchId"]]
        results = []
        for i, m in enumerate(upcoming[:odds_count]):
            capture = FIXTURES / "match_page.html" if i == 0 else None
            try:
                odds = await wx.collect_odds(m["matchId"], capture=capture)
            except Exception as exc:
                results.append({"id": m["matchId"], "error": repr(exc)})
                print("[odds] FAILED", m["player1"], repr(exc))
                continue
            results.append({
                "id": m["matchId"], "match": f"{m['player1']} vs {m['player2']}",
                "sections": odds["sectionCount"], "odds": odds["oddCount"],
                "buttons": odds["buttonCount"],
            })
        print(json.dumps(results, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--odds", type=int, default=3, help="how many match pages to open")
    args = ap.parse_args()
    asyncio.run(main(args.odds))
