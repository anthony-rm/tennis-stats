"""Tennis Abstract player stats (refactored from ~/Projets/tennis_players/tennisabstract_scraper.py).

Fixes vs. the original scraper (PLAN.md §2.2):
  * profile comes from the raw player page's inline JS vars (`currentrank`,
    `peakrank`, `elo_rank`, `elo_rating`, `hand`, `country`, `dob`, `ht`,
    `fullname`) — the visible `#bio` span is filled client-side, so parsing it
    always returned {};
  * `major-events` does not exist in the fragment — dropped (use `recent-events`);
  * ALL tables in the fragment are parsed (18 real tables + the id-less nav table
    is skipped), not just 4;
  * name -> id resolution with 404 fallback variants (accents, hyphens,
    particles) and an explicit unresolved result instead of silent empty data;
  * per-player, per-day JSON cache (24 h TTL) — players repeat day to day.

Data shapes
  profile : {"name", "current_rank", "peak_rank", "elo_rank", "elo_rating",
             "hand", "country", "birthdate", "age", "height"}
  table   : {"columns": [str, ...], "rows": [[value, ...], ...]}   (column order
            matches the site; duplicate header names are pandas-mangled, e.g.
            "W", "W.1", and extractors index them positionally)
  payload : {"player_name", "player_id", "fetched_at", "profile", "tables"}
"""
from __future__ import annotations

import html as html_lib
import json
import re
import time
import unicodedata
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd
import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

PLAYER_PAGE = "https://www.tennisabstract.com/cgi-bin/player.cgi?p={player_id}"
JS_FRAGS = "https://www.tennisabstract.com/jsfrags/{player_id}.js"

TABLE_IDS = [
    "recent-results", "tour-years", "chall-years", "recent-finals",
    "year-end-rankings", "recent-events", "career-splits", "last52-splits",
    "winners-errors", "serve-speed", "pbp-points", "pbp-games", "pbp-stats",
    "mcp-serve", "mcp-return", "mcp-rally", "mcp-tactics", "head-to-heads",
]

PARTICLE_TOKENS = {
    "de", "del", "della", "der", "den", "di", "du", "da", "la", "le", "les",
    "van", "von", "ter", "ten", "bin", "al", "el", "st", "st.",
}

CACHE_TTL_HOURS = 24.0


class UnresolvedPlayer(Exception):
    """No Tennis Abstract id could be resolved for a name."""

    def __init__(self, name: str, tried: list[str]) -> None:
        super().__init__(f"unresolved Tennis Abstract id for {name!r} (tried: {tried})")
        self.name = name
        self.tried = tried


# ---------------------------------------------------------------- names ----
def _strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )


def player_name_to_id(name: str) -> str:
    """'Novak Djokovic' -> 'NovakDjokovic' (accent-free, no spaces)."""
    return re.sub(r"\s+", "", _strip_accents(name).title())


def id_candidates(name: str) -> list[str]:
    """Id variants to try, most literal first (each is probed with a 404 check)."""
    candidates: list[str] = []

    def add(value: str) -> None:
        if value and value not in candidates:
            candidates.append(value)

    add(player_name_to_id(name))                                   # AlexDeMinaur
    add(re.sub(r"[^A-Za-z0-9]", "", player_name_to_id(name)))      # drop - ' etc.
    tokens = [t for t in _strip_accents(name).split() if t.lower() not in PARTICLE_TOKENS]
    if tokens:
        add(re.sub(r"\s+", "", " ".join(tokens).title()))          # drop particles
        add(re.sub(r"[^A-Za-z0-9]", "", " ".join(tokens).title()))
    add(re.sub(r"\s+", "", _strip_accents(name.split(",")[0]).title()))  # "Last, First"
    return candidates


def resolve_player(name: str, session: Optional[requests.Session] = None) -> tuple[Optional[str], list[str]]:
    """Return (player_id | None, candidates_tried). 404-safe: wrong forms cost one GET."""
    sess = session or requests.Session()
    tried: list[str] = []
    for candidate in id_candidates(name):
        tried.append(candidate)
        try:
            resp = sess.get(JS_FRAGS.format(player_id=candidate), headers=HEADERS, timeout=20)
        except requests.RequestException:
            continue
        if resp.status_code == 200 and "player_frag" in resp.text:
            return candidate, tried
    return None, tried


# ------------------------------------------------------------ fetching -----
def _get(url: str, session: Optional[requests.Session] = None, timeout: int = 30) -> requests.Response:
    """GET with polite retry/backoff (3 attempts, 1s/3s) on network errors and
    5xx. 4xx (e.g. a 404 for an unknown player id) is never retried."""
    sess = session or requests.Session()
    last_exc: Exception | None = None
    for attempt in range(3):
        resp = None
        try:
            resp = sess.get(url, headers=HEADERS, timeout=timeout)
        except requests.RequestException as exc:
            last_exc = exc
        else:
            if resp.status_code < 500:
                resp.raise_for_status()      # 4xx raises here, outside the retry
                return resp
            last_exc = requests.HTTPError(f"HTTP {resp.status_code} for {url}", response=resp)
        if attempt < 2:
            time.sleep(1.0 * (3 ** attempt))
    raise last_exc  # type: ignore[misc]


def extract_player_frag(js_text: str) -> str:
    """`var player_frag = `...html...`;` -> unescaped HTML."""
    m = re.search(r"var\s+player_frag\s*=\s*`(.*?)`", js_text, re.DOTALL)
    if not m:
        raise ValueError("player_frag variable not found in jsfrags file")
    return html_lib.unescape(m.group(1))


_PROFILE_VARS = {
    "name": r"var\s+fullname\s*=\s*'([^']*)'",
    "current_rank": r"var\s+currentrank\s*=\s*(\d+)",
    "peak_rank": r"var\s+peakrank\s*=\s*(\d+)",
    "elo_rank": r"var\s+elo_rank\s*=\s*'?(\d+)'?",
    "elo_rating": r"var\s+elo_rating\s*=\s*'?(\d+)'?",
    "hand": r"var\s+hand\s*=\s*'([^']*)'",
    "country": r"var\s+country\s*=\s*'([^']*)'",
    "height": r"var\s+ht\s*=\s*(\d+)",
    "dob": r"var\s+(?:dob|bd)\s*=\s*(\d{8})",
}


def parse_profile(page_html: str) -> dict:
    """Profile from inline JS vars — the rendered #bio span is client-side only."""
    profile: dict[str, Any] = {}
    for key, pattern in _PROFILE_VARS.items():
        m = re.search(pattern, page_html)
        if not m:
            continue
        raw = m.group(1)
        if key in {"current_rank", "peak_rank", "elo_rank", "elo_rating", "height"}:
            profile[key] = int(raw)
        elif key == "dob":
            try:
                dob = datetime.strptime(raw, "%Y%m%d").date()
            except ValueError:
                continue
            profile["birthdate"] = dob.isoformat()
            today = date.today()
            profile["age"] = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
        else:
            profile[key] = raw
    if profile.get("name") == "":
        profile.pop("name")
    # live page variables sometimes disagree in spelling with the report vars
    for alias, canonical in (("rank", "current_rank"), ("peakrank", "peak_rank")):
        if canonical not in profile:
            m = re.search(rf"var\s+{alias}\s*=\s*(\d+)", page_html)
            if m:
                profile[canonical] = int(m.group(1))
    return profile


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and pd.isna(value):
            return None
        return value
    if value is pd.NaT or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return text if text and text.lower() != "nan" else None


def parse_table(fragment_html: str, table_id: str) -> Optional[dict]:
    """One table -> {"columns": [...], "rows": [[...]]} (None when absent)."""
    soup = BeautifulSoup(fragment_html, "lxml")
    table = soup.find("table", id=table_id)
    if table is None:
        return None
    try:
        dfs = pd.read_html(StringIO(str(table)))
    except ValueError:
        return None
    if not dfs:
        return None
    df = pd.concat(dfs, ignore_index=True) if len(dfs) > 1 else dfs[0]
    if df.empty and len(df.columns) == 0:
        return None
    columns = [str(c).replace("\xa0", " ") for c in df.columns]
    rows = [[_jsonable(v) for v in row] for row in df.itertuples(index=False, name=None)]
    return {"columns": columns, "rows": rows}


def parse_all_tables(fragment_html: str) -> dict[str, dict]:
    soup = BeautifulSoup(fragment_html, "lxml")
    found = {t.get("id") for t in soup.find_all("table") if t.get("id")}
    tables: dict[str, dict] = {}
    for table_id in TABLE_IDS:
        if table_id not in found:
            continue
        parsed = parse_table(fragment_html, table_id)
        if parsed is not None:
            tables[table_id] = parsed
    return tables


def fetch_player(
    name: str,
    session: Optional[requests.Session] = None,
) -> dict:
    """Resolve `name` and fetch profile + every table. Raises UnresolvedPlayer."""
    player_id, tried = resolve_player(name, session=session)
    if player_id is None:
        raise UnresolvedPlayer(name, tried)

    page_html = _get(PLAYER_PAGE.format(player_id=player_id), session=session).text
    profile = parse_profile(page_html)

    js_text = _get(JS_FRAGS.format(player_id=player_id), session=session).text
    fragment = extract_player_frag(js_text)
    tables = parse_all_tables(fragment)

    return {
        "player_name": name,
        "player_id": player_id,
        "tried": tried,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "profile": profile,
        "tables": tables,
    }


# --------------------------------------------------------------- cache -----
def _cache_file(cache_dir: Path, player_id: str) -> Path:
    return cache_dir / f"{player_id}.json"


def load_cached(cache_dir: Path, player_id: str, ttl_hours: float = CACHE_TTL_HOURS) -> Optional[dict]:
    """Newest cached payload for player_id within ttl (searches today/yesterday dirs)."""
    path = _cache_file(cache_dir, player_id)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(payload["fetched_at"])
    except (ValueError, KeyError, OSError):
        return None
    if (datetime.now() - fetched).total_seconds() > ttl_hours * 3600:
        return None
    return payload


def save_cached(cache_dir: Path, payload: dict) -> Path:
    day_dir = cache_dir / date.today().isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    # unresolved payloads get a name-derived file so they can't clobber each other
    key = payload.get("player_id") or "unresolved-" + re.sub(
        r"[^A-Za-z0-9]+", "_", payload.get("player_name", "unknown")
    ).strip("_")
    path = _cache_file(day_dir, key)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def get_player(
    name: str,
    cache_dir: Path,
    ttl_hours: float = CACHE_TTL_HOURS,
    session: Optional[requests.Session] = None,
) -> dict:
    """Cache-first fetch. Returns payload with `"unresolved": True` when no id fits."""
    cached = load_cached_by_name(cache_dir, name, ttl_hours)
    if cached:
        return cached

    try:
        payload = fetch_player(name, session=session)
    except UnresolvedPlayer as exc:
        payload = {
            "player_name": name,
            "player_id": None,
            "tried": exc.tried,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "unresolved": True,
            "profile": {},
            "tables": {},
        }
    save_cached(cache_dir, payload)
    return payload


def get_players(
    names: Iterable[str],
    cache_dir: Path,
    ttl_hours: float = CACHE_TTL_HOURS,
    delay: float = 1.6,
    log=None,
) -> dict[str, dict]:
    """Fetch several players with a politeness delay. Returns {name: payload}."""
    log = log or (lambda msg: None)
    seen: dict[str, dict] = {}
    session = requests.Session()
    for name in dict.fromkeys(n for n in names if n):
        cached = load_cached_by_name(cache_dir, name, ttl_hours)
        if cached:
            log(f"cached: {name}")
            seen[name] = cached
            continue
        log(f"fetching: {name}")
        seen[name] = get_player(name, cache_dir, ttl_hours=ttl_hours, session=session)
        time.sleep(delay)
    return seen


def load_cached_by_name(cache_dir: Path, name: str, ttl_hours: float = CACHE_TTL_HOURS) -> Optional[dict]:
    for day in (date.today(), date.today() - timedelta(days=1)):
        day_dir = cache_dir / day.isoformat()
        if not day_dir.exists():
            continue
        for path in sorted(day_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if payload.get("player_name", "").casefold() == name.casefold():
                fetched = datetime.fromisoformat(payload.get("fetched_at", "2000-01-01"))
                if (datetime.now() - fetched).total_seconds() <= ttl_hours * 3600:
                    return payload
    return None
