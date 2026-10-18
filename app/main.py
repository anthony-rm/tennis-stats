"""FastAPI app — Tennis Betting Dashboard API + static page.

Run:
    .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
(auto-scan of today's Winamax list starts on server start, per PLAN.md §4.)

Endpoints
    GET  /                     dashboard (web/index.html)
    GET  /api/version          dashboard version (VERSION file)
    GET  /api/columns          checkbox catalogue (groups A-L)
    GET  /api/matches?form_n=N match rows + computed values per column
    GET  /api/jobs             job states (polling fallback)
    GET  /api/scan/stream      SSE: job progress + live data events
    POST /api/scan             start the Winamax list scan
    POST /api/odds             start the odds pass (all pending / force / given ids)
    POST /api/stats            start the Tennis Abstract fetch (all missing / given names)
    GET  /api/selections       saved selection (cells + format)
    POST /api/selections       save selection
    POST /api/export           selection -> text (llm | tsv | md | md_block | json)
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from app import columns as columns_mod
from app.exporter import FORMATS, export_text
from app.jobs import JobBusy, JobManager
from app.paths import app_version, asset_dir, cache_dir
from app.store import Store

WEB_INDEX = asset_dir() / "web" / "index.html"
CACHE_DIR = cache_dir()

store = Store()
jobs = JobManager(store, CACHE_DIR)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # auto-scan on startup (PLAN.md §4/§10: list EVERY match on the page —
    # the UI selects today's by default via its Day filter).
    # TENNIS_STATS_NO_AUTOSCAN=1 skips it: useful when a second instance runs
    # next to the installed app (two concurrent Winamax scrapes = WAF risk).
    if not os.environ.get("TENNIS_STATS_NO_AUTOSCAN"):
        try:
            jobs.start_scan(stop_at_night_gap=False, include_live=True)
        except JobBusy:
            pass
    yield
    await jobs.shutdown()
    store.close()


app = FastAPI(title="Tennis Betting Dashboard", lifespan=lifespan)


@app.get("/")
async def index() -> FileResponse:
    if not WEB_INDEX.exists():
        raise HTTPException(404, "web/index.html missing")
    # always revalidate: the page is edited in place while the dashboard is open
    return FileResponse(WEB_INDEX, media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-cache"})


# ------------------------------------------------------------------ data ---
@app.get("/api/version")
async def get_version() -> dict:
    return {"version": app_version()}


@app.get("/api/columns")
async def get_columns() -> dict:
    return {"groups": columns_mod.catalogue()}


@app.get("/api/matches")
async def get_matches(form_n: int = Query(10, ge=2, le=50)) -> dict:
    matches = store.list_matches()
    stats = store.get_stats()
    rows = columns_mod.build_rows(matches, stats, form_n=form_n)
    return {
        "matches": rows,
        "form_n": form_n,
        "stats_present": sorted(stats.keys()),
        "raw": [
            {k: m.get(k) for k in (
                "match_id", "tournament", "round", "player1", "player2",
                "start_local", "day_offset", "day_indicator", "live",
                "odd_p1", "odd_p2", "pct_p1", "pct_p2", "url",
                "odds_fetched_at", "section_count", "odd_count", "market_count",
            )}
            for m in matches
        ],
    }


# ------------------------------------------------------------------ jobs ---
@app.get("/api/jobs")
async def get_jobs() -> dict:
    return {"jobs": jobs.snapshot()}


@app.get("/api/scan/stream")
async def scan_stream() -> StreamingResponse:
    queue = jobs.subscribe()

    async def gen():
        try:
            # first frame: full snapshot so a late subscriber syncs immediately
            yield _sse({"type": "snapshot", "jobs": jobs.snapshot()})
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield _sse(event)
        finally:
            jobs.unsubscribe(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.post("/api/scan")
async def post_scan(body: Optional[dict] = Body(default=None)) -> dict:
    body = body or {}
    try:
        job = jobs.start_scan(
            stop_at_night_gap=bool(body.get("stop_at_night_gap", False)),
            include_live=bool(body.get("include_live", True)),
        )
    except JobBusy as exc:
        raise HTTPException(409, str(exc))
    return {"job": job}


@app.post("/api/odds")
async def post_odds(body: Optional[dict] = Body(default=None)) -> dict:
    body = body or {}
    ids = body.get("match_ids")
    if isinstance(ids, str):
        ids = [ids]
    try:
        job = jobs.start_odds(
            [str(i) for i in ids] if ids else None,
            force=bool(body.get("force")),
        )
    except JobBusy as exc:
        raise HTTPException(409, str(exc))
    return {"job": job}


@app.post("/api/stats")
async def post_stats(body: Optional[dict] = Body(default=None)) -> dict:
    body = body or {}
    names = body.get("names")
    try:
        job = jobs.start_stats([str(n) for n in names] if names else None)
    except JobBusy as exc:
        raise HTTPException(409, str(exc))
    return {"job": job}


# ------------------------------------------------------------- selection ---
@app.get("/api/selections")
async def get_selection(name: str = "default") -> dict:
    return {"selection": store.get_selection(name) or {"name": name, "cells": {}, "format": "llm"}}


@app.post("/api/selections")
async def post_selection(body: dict = Body(...)) -> dict:
    cells = body.get("cells") or {}
    fmt = body.get("format") or "llm"
    if fmt not in FORMATS:
        raise HTTPException(422, f"format must be one of {FORMATS}")
    store.save_selection(cells, fmt, name=str(body.get("name") or "default"))
    return {"ok": True}


@app.post("/api/export")
async def post_export(body: dict = Body(...)) -> dict:
    cells = body.get("cells") or {}
    fmt = body.get("format") or "llm"
    if fmt not in FORMATS:
        raise HTTPException(422, f"format must be one of {FORMATS}")
    form_n = int(body.get("form_n") or 10)
    matches = store.list_matches()
    stats = store.get_stats()
    rows = columns_mod.build_rows(matches, stats, form_n=form_n)
    text = export_text(rows, cells, fmt)
    return {"text": text, "chars": len(text),
            "columns": len({c for v in cells.values() for c in v}),
            "rows": sum(1 for v in cells.values() if v)}
