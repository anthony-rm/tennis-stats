"""Background jobs: Winamax scan, Winamax odds pass, Tennis Abstract stats.

One asyncio task per job kind; a lock guarantees a single browser job at a time
(Winamax WAF friendliness + one page at a time by design). Progress is pushed
to every SSE subscriber through an asyncio.Queue (see /api/scan/stream) and is
also available by polling GET /api/jobs.
"""
from __future__ import annotations

import asyncio
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import requests

from core import tennisabstract as ta
from core.winamax import WinamaxBlocked, WinamaxSession

KINDS = ("scan", "odds", "stats")


class JobBusy(RuntimeError):
    pass


class JobManager:
    def __init__(self, store, cache_dir: Path) -> None:
        self.store = store
        self.cache_dir = Path(cache_dir)
        self.jobs: dict[str, dict] = {
            k: self._blank(k) for k in KINDS
        }
        self._tasks: dict[str, asyncio.Task] = {}
        self._subscribers: list[asyncio.Queue] = []
        self._winamax_lock = asyncio.Lock()

    # ------------------------------------------------------------ events --
    @staticmethod
    def _blank(kind: str) -> dict:
        return {
            "kind": kind, "status": "idle", "message": "", "step": 0, "total": 0,
            "started_at": None, "finished_at": None, "result": None,
        }

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def _emit(self, payload: dict) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    def snapshot(self) -> dict:
        return {k: dict(v) for k, v in self.jobs.items()}

    def _update(self, kind: str, **fields: Any) -> None:
        self.jobs[kind].update(fields)
        self._emit({"type": "job", "job": dict(self.jobs[kind])})

    def is_running(self, kind: str) -> bool:
        task = self._tasks.get(kind)
        return task is not None and not task.done()

    def _assert_idle(self, kind: str) -> None:
        if self.is_running(kind):
            raise JobBusy(f"{kind} is already running")
        if kind in ("scan", "odds"):
            other = "odds" if kind == "scan" else "scan"
            if self.is_running(other) or self._winamax_lock.locked():
                raise JobBusy("a browser job is already running")

    # ----------------------------------------------------------- starts ---
    def start_scan(self, stop_at_night_gap: bool = True, include_live: bool = True) -> dict:
        self._assert_idle("scan")
        self._update("scan", status="running", message="queued", started_at=_now())
        task = asyncio.create_task(
            self._run_scan(stop_at_night_gap, include_live), name="job-scan"
        )
        self._tasks["scan"] = task
        return self.snapshot()["scan"]

    def start_odds(self, match_ids: Optional[list[str]] = None) -> dict:
        self._assert_idle("odds")
        self._update("odds", status="running", message="queued", started_at=_now())
        task = asyncio.create_task(self._run_odds(match_ids), name="job-odds")
        self._tasks["odds"] = task
        return self.snapshot()["odds"]

    def start_stats(self, names: Optional[list[str]] = None) -> dict:
        self._assert_idle("stats")
        self._update("stats", status="running", message="queued", started_at=_now())
        task = asyncio.create_task(self._run_stats(names), name="job-stats")
        self._tasks["stats"] = task
        return self.snapshot()["stats"]

    # ------------------------------------------------------------- scan ---
    async def _run_scan(self, stop_at_night_gap: bool, include_live: bool) -> None:
        kind = "scan"
        self._update(kind, status="running", message="starting browser",
                     step=0, total=0, started_at=_now(), finished_at=None, result=None)
        try:
            async with self._winamax_lock:
                session = WinamaxSession(on_event=lambda m: self._update(kind, message=m))
                try:
                    await session.start()
                    matches = await session.scan_today(
                        include_live=include_live,
                        stop_at_night_gap=stop_at_night_gap,
                    )
                finally:
                    await session.close()
            n = self.store.upsert_matches(matches)
            self._update(kind, status="done",
                         message=f"{n} matches stored", step=n, total=n,
                         finished_at=_now(), result={"matches": n})
        except Exception as exc:
            self._fail(kind, exc)

    # ------------------------------------------------------------- odds ---
    async def _run_odds(self, match_ids: Optional[list[str]]) -> None:
        kind = "odds"
        if match_ids:
            targets = [str(m) for m in match_ids]
        else:
            targets = [str(m["match_id"]) for m in self.store.matches_needing_odds()]
        self._update(kind, status="running", message="starting browser",
                     step=0, total=len(targets), started_at=_now(),
                     finished_at=None, result=None)
        if not targets:
            self._update(kind, status="done", message="nothing to fetch",
                         finished_at=_now(), result={"fetched": 0, "failed": []})
            return

        fetched, failed = 0, []
        try:
            async with self._winamax_lock:
                session = WinamaxSession(on_event=lambda m: self._update(kind, message=m))
                try:
                    await session.start()
                    for i, mid in enumerate(targets, 1):
                        match = self.store.get_match(mid) or {}
                        label = f"{match.get('player1', '?')} vs {match.get('player2', '?')}"
                        self._update(kind, message=f"odds: {label}  [{i}/{len(targets)}]",
                                     step=i - 1, total=len(targets))
                        if match.get("live"):
                            failed.append({"match_id": mid, "error": "live (skipped)"})
                            continue
                        try:
                            odds = await session.collect_odds(mid)
                        except LookupError as exc:
                            failed.append({"match_id": mid, "error": str(exc)})
                            continue
                        except Exception as exc:
                            failed.append({"match_id": mid, "error": str(exc)})
                            if isinstance(exc, WinamaxBlocked):
                                break      # repeated WAF block: stop the pass
                            continue
                        self.store.set_odds(mid, odds)
                        fetched += 1
                        self._update(kind, step=i, total=len(targets),
                                     message=f"odds: {label} — {odds.get('oddCount', 0)} odds")
                finally:
                    await session.close()
            self._update(kind, status="done",
                         message=f"{fetched}/{len(targets)} matches fetched",
                         finished_at=_now(),
                         result={"fetched": fetched, "failed": failed})
        except Exception as exc:
            self._fail(kind, exc, partial={"fetched": fetched, "failed": failed})

    # ------------------------------------------------------------ stats ---
    async def _run_stats(self, names: Optional[list[str]]) -> None:
        kind = "stats"
        if not names:
            matches = self.store.list_matches()
            names = list(dict.fromkeys(
                [m["player1"] for m in matches if m.get("player1")] +
                [m["player2"] for m in matches if m.get("player2")]
            ))
        names = [n for n in names if n]
        self._update(kind, status="running", message="fetching player stats",
                     step=0, total=len(names), started_at=_now(),
                     finished_at=None, result=None)
        if not names:
            self._update(kind, status="done", message="no players",
                         finished_at=_now(), result={"stored": 0})
            return

        loop = asyncio.get_running_loop()

        def progress(done: int, name: str) -> None:
            loop.call_soon_threadsafe(
                lambda: self._update(kind, step=done, total=len(names),
                                     message=f"stats: {name}")
            )

        try:
            payloads = await asyncio.to_thread(
                self._fetch_all, names, progress
            )
            stored = 0
            unresolved = []
            for name, payload in payloads.items():
                self.store.upsert_stats(name, payload)
                stored += 1
                if payload.get("unresolved"):
                    unresolved.append(name)
            self._update(kind, status="done",
                         message=f"{stored} players stored"
                                 + (f" ({len(unresolved)} unresolved)" if unresolved else ""),
                         finished_at=_now(),
                         result={"stored": stored, "unresolved": unresolved})
        except Exception as exc:
            self._fail(kind, exc)

    def _fetch_all(self, names: list[str], progress) -> dict[str, dict]:
        out: dict[str, dict] = {}
        session = requests.Session()
        for i, name in enumerate(names):
            try:
                payload = ta.get_player(name, self.cache_dir, session=session)
            except Exception as exc:  # network blip: record, keep going
                payload = {
                    "player_name": name, "player_id": None,
                    "fetched_at": datetime.now().isoformat(timespec="seconds"),
                    "profile": {}, "tables": {}, "error": str(exc),
                }
            out[name] = payload
            progress(i + 1, name)
            if i < len(names) - 1:
                time.sleep(1.6)
        return out

    # ------------------------------------------------------------ utils ---
    def _fail(self, kind: str, exc: Exception, partial: Optional[dict] = None) -> None:
        message = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
        self._update(kind, status="error", message=message, finished_at=_now(),
                     result=partial or {"error": message})

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        for task in list(self._tasks.values()):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
