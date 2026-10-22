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
    particles, generational suffixes) and an explicit unresolved result
    instead of silent empty data;
  * resolution only trusts *answers*: probes are retried like `_get` does, and
    a name whose probes all died raises `ResolutionNetworkError` instead of
    being recorded as "not on the site" — the 2026-10-19 network blip (broken
    VPN/DoH path) cost 8 famous players their stats that way;
  * last-resort lookup through TA's own autocomplete list (`mwplayerlist.js`),
    which resolves ids spelled differently than the display name
    ("Thiago Tirante" -> `ThiagoAgustinTirante`);
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
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor as _TPE
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

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

# generational suffixes: "Martin Damm Jr" is "Martin Damm" on TA
SUFFIX_TOKENS = {"jr", "sr", "ii", "iii", "iv"}

# probes retry exactly like _get() so one dead request never decides a name
PROBE_ATTEMPTS = 3
PROBE_BACKOFF = (1.0, 3.0)
PROBE_TIMEOUT = 20

# throttling/blocking is silence, not an answer about the player
THROTTLED_STATUSES = {403, 429}

# how many consecutive Tennis Abstract/network failures end a batch pass —
# every remaining player would fail the same way, so the job reports the fault
# instead of spending a timeout per player (and nothing is recorded as
# "player absent")
NETWORK_ABORT_AFTER = 3

# TA's own autocomplete source (~1.7 MB, both tours), reached only when id
# probing found nothing and memoised in-process for the length of a run
PLAYERLIST_URL = "https://www.tennisabstract.com/mwplayerlist.js"
PLAYERLIST_TTL_HOURS = 24.0

CACHE_TTL_HOURS = 24.0
PLAYERLIST_CACHE_FILE = "playerlist.json"
PLAYERLIST_CACHE_TTL_SECONDS = PLAYERLIST_TTL_HOURS * 3600

# Parallelism for the batch fetch (see fetch_player_batch). jsfrags tolerates
# high concurrency (48-way: 46/404s, 0 blocks); player.cgi does not (2-way:
# 43/429, even 1.5 s spacing), so jsfrags is resolved by a parallel pool and
# the profile page is fetched through one lock at a 2.5 s spacing.
JSFRAGS_WORKERS = 10
PLAYERPAGE_DELAY_S = 2.5


class UnresolvedPlayer(Exception):
    """No Tennis Abstract id could be resolved for a name."""

    def __init__(self, name: str, tried: list[str]) -> None:
        super().__init__(f"unresolved Tennis Abstract id for {name!r} (tried: {tried})")
        self.name = name
        self.tried = tried


class ResolutionNetworkError(Exception):
    """Tennis Abstract gave no usable answer while resolving a name.

    Deliberately distinct from `UnresolvedPlayer`: this says nothing about the
    player — the request died (DNS/VPN outage, 5xx) or TA's own player list
    could not be read. Callers must store it as a retriable `error`, never as
    a permanent `unresolved`, or a five-minute network blip blanks a player
    until the stored row is cleared.
    """

    def __init__(self, name: str, tried: list[str], reason: str) -> None:
        super().__init__(
            f"could not reach Tennis Abstract for {name!r} (tried: {tried}): {reason}"
        )
        self.name = name
        self.tried = tried
        self.reason = reason


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
    suffix_free = [t for t in tokens if t.lower() not in SUFFIX_TOKENS]
    if len(suffix_free) < len(tokens):                             # "Martin Damm Jr"
        add(re.sub(r"\s+", "", " ".join(suffix_free).title()))     #   -> MartinDamm
        add(re.sub(r"[^A-Za-z0-9]", "", " ".join(suffix_free).title()))
    add(re.sub(r"\s+", "", _strip_accents(name.split(",")[0]).title()))  # "Last, First"
    return candidates


def _probe(
    url: str,
    session: Optional[requests.Session] = None,
    attempts: int = PROBE_ATTEMPTS,
    timeout: int = PROBE_TIMEOUT,
    backoff: tuple[float, ...] = PROBE_BACKOFF,
) -> tuple[Optional[int], Optional[str], Optional[str]]:
    """GET `url`, retrying network errors, 5xx and throttling like `_get` does.

    Returns `(status, body, reason)`:
      * a status code plus the body — an *answer*, so a 404 really is a
        non-existent id;
      * `(None, None, reason)` only when every attempt was lost or throttled —
        ambiguous, and never to be read as "the player is not on the site".
    """
    sess = session or requests.Session()
    reason = "no attempt ran"
    for attempt in range(attempts):
        try:
            resp = sess.get(url, headers=HEADERS, timeout=timeout)
        except requests.RequestException as exc:
            reason = f"{type(exc).__name__}: {exc}"
        else:
            if resp.status_code < 500 and resp.status_code not in THROTTLED_STATUSES:
                return resp.status_code, resp.text, None
            reason = f"HTTP {resp.status_code}"   # throttled/blocked says nothing
        if attempt < attempts - 1:
            time.sleep(backoff[min(attempt, len(backoff) - 1)])
    return None, None, reason


_PLAYERLIST_CACHE: dict[str, tuple[float, list[str]]] = {}
_JSFRAGS_MEMO: dict[str, str] = {}
_JSFRAGS_LOCK = threading.Lock()
_PLAYER_CGI_LOCK = threading.Lock()
_LAST_PLAYER_CGI_TIME = 0.0


def _get_player_page(
    player_id: str,
    session: Optional[requests.Session] = None,
    min_spacing: float = 1.0,
) -> str:
    global _LAST_PLAYER_CGI_TIME
    with _PLAYER_CGI_LOCK:
        now = time.monotonic()
        elapsed = now - _LAST_PLAYER_CGI_TIME
        if elapsed < min_spacing:
            time.sleep(min_spacing - elapsed)
        text = _get(PLAYER_PAGE.format(player_id=player_id), session=session).text
        _LAST_PLAYER_CGI_TIME = time.monotonic()
        return text


def load_playerlist(
    session: Optional[requests.Session] = None,
    ttl_hours: float = PLAYERLIST_TTL_HOURS,
    cache_dir: Optional[Path] = None,
) -> list[str]:
    """TA's own autocomplete list (`mwplayerlist.js`): every player it knows.

    One file covers both tours and is the index behind TA's search box, which
    is why it can resolve an id that differs from the display name. ~1.7 MB,
    memoised in-process and on disk — it is only reached when id probing found
    nothing, so a run downloads it at most once.
    """
    now = time.monotonic()
    hit = _PLAYERLIST_CACHE.get(PLAYERLIST_URL)
    if hit and now - hit[0] <= ttl_hours * 3600:
        return hit[1]

    if cache_dir is not None:
        disk_path = Path(cache_dir) / PLAYERLIST_CACHE_FILE
        if disk_path.exists():
            try:
                mtime = disk_path.stat().st_mtime
                if time.time() - mtime <= ttl_hours * 3600:
                    names = json.loads(disk_path.read_text(encoding="utf-8"))
                    if isinstance(names, list) and names:
                        _PLAYERLIST_CACHE[PLAYERLIST_URL] = (now, names)
                        return names
            except Exception:
                pass

    text = _get(PLAYERLIST_URL, session=session).text
    m = re.match(r"\s*var\s+playerlist\s*=\s*\[(.*)\]\s*;?\s*$", text, re.DOTALL)
    if not m:
        raise ValueError("playerlist variable not found in mwplayerlist.js")
    names = [n for n in json.loads(f"[{m.group(1)}]") if isinstance(n, str)]
    _PLAYERLIST_CACHE[PLAYERLIST_URL] = (now, names)

    if cache_dir is not None:
        try:
            disk_path = Path(cache_dir) / PLAYERLIST_CACHE_FILE
            disk_path.parent.mkdir(parents=True, exist_ok=True)
            disk_path.write_text(json.dumps(names, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    return names


def _name_tokens(text: str) -> frozenset[str]:
    """Accent-free, case-folded tokens, minus the '(M) '/'(W) ' prefix, any
    particle and any generational suffix — so both sides of a name comparison
    are on equal terms ('Thiago Agustin Tirante' vs 'Thiago Tirante')."""
    cleaned = re.sub(r"^\([MW]\)\s*", "", text.strip())
    return frozenset(
        t for t in _strip_accents(cleaned).casefold().split()
        if t and t not in PARTICLE_TOKENS and t not in SUFFIX_TOKENS
    )


def match_player_name(name: str, entries: Iterable[str]) -> Optional[str]:
    """Find `name` in TA's list: `'Thiago Tirante'` -> `'(M) Thiago Agustin Tirante'`.

    An exact token match wins; failing that, a *unique* entry containing every
    query token plus at most two more. Ambiguous or absent returns None —
    stats for the wrong player are worse than no stats.
    """
    query = _name_tokens(name)
    if not query:
        return None
    exact: list[str] = []
    partial: list[str] = []
    for entry in entries:
        tokens = _name_tokens(entry)
        if not tokens:
            continue
        if tokens == query:
            exact.append(entry)
        elif len(tokens) <= len(query) + 2 and query <= tokens:
            partial.append(entry)
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None
    return partial[0] if len(partial) == 1 else None


def _canonical_id(
    name: str, session: Optional[requests.Session] = None
) -> tuple[Optional[str], Optional[str]]:
    """`(id, reason)` derived from TA's player list.

    `reason` is set only when the list itself could not be read — ambiguous,
    so the caller reports a network error instead of "player absent".
    """
    try:
        entries = load_playerlist(session=session)
    except (requests.RequestException, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    display = match_player_name(name, entries)
    if not display:
        return None, None
    return player_name_to_id(re.sub(r"^\([MW]\)\s*", "", display)), None


def resolve_player(name: str, session: Optional[requests.Session] = None) -> tuple[Optional[str], list[str]]:
    """Return (player_id | None, candidates_tried).

    Only an *answer* counts as evidence: probes are retried, throttling counts
    as silence, and when TA never answers (or its player list cannot be read)
    `ResolutionNetworkError` is raised rather than reporting the player as
    absent. `None` therefore means "TA looked and did not have this name".
    """
    sess = session or requests.Session()
    tried: list[str] = []
    answered = 0
    reason = "no candidate id"
    for candidate in id_candidates(name):
        tried.append(candidate)
        status, body, why = _probe(JS_FRAGS.format(player_id=candidate), session=sess)
        if status is None:
            reason = why or "no answer"
            continue                        # ambiguous — never "player absent"
        answered += 1
        if status == 200 and body and "player_frag" in body:
            return candidate, tried
    if not answered:
        raise ResolutionNetworkError(name, tried, reason)

    # last resort: TA's own search index, for ids spelled differently than the
    # name (a middle name: Thiago Tirante -> ThiagoAgustinTirante)
    canonical, why = _canonical_id(name, session=sess)
    if why:
        raise ResolutionNetworkError(name, tried, why)
    if canonical and canonical not in tried:
        tried.append(canonical)
        status, body, why = _probe(JS_FRAGS.format(player_id=canonical), session=sess)
        if status is None:
            raise ResolutionNetworkError(name, tried, why or "no answer")
        if status == 200 and body and "player_frag" in body:
            return canonical, tried
    return None, tried


# ------------------------------------------------------------ fetching -----
def _get(url: str, session: Optional[requests.Session] = None, timeout: int = 30) -> requests.Response:
    """GET with polite retry/backoff (3 attempts, 1s/3s) on network errors, 5xx
    and throttling (403/429 — silence, worth waiting out). Any other 4xx
    (e.g. a 404 for an unknown player id) is never retried: it is an answer."""
    sess = session or requests.Session()
    last_exc: Exception | None = None
    for attempt in range(3):
        resp = None
        try:
            resp = sess.get(url, headers=HEADERS, timeout=timeout)
        except requests.RequestException as exc:
            last_exc = exc
        else:
            if resp.status_code < 500 and resp.status_code not in THROTTLED_STATUSES:
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
    """All TA tables -> {id: table}. One BeautifulSoup parse for the id scan,
    then one parse per table (pandas needs the isolated <table> HTML)."""
    soup = BeautifulSoup(fragment_html, "lxml")
    found = {t.get("id") for t in soup.find_all("table") if t.get("id")}
    tables: dict[str, dict] = {}
    for table_id in TABLE_IDS:
        if table_id not in found:
            continue
        parsed = parse_table(str(soup.find("table", id=table_id)), table_id)
        if parsed is not None:
            tables[table_id] = parsed
    return tables


def fetch_player(
    name: str,
    cache_dir: Optional[Path] = None,
    session: Optional[requests.Session] = None,
    probe_body: Optional[str] = None,
) -> dict:
    """Resolve `name` and fetch profile + every table.

    `probe_body` is the already-downloaded `jsfrags/<id>.js` body from the
    resolution step — passing it avoids the redundant 254 KB download that
    `resolve_player` already performed to prove the id exists.
    """
    player_id, tried = resolve_player(name, session=session)
    if player_id is None:
        raise UnresolvedPlayer(name, tried)

    page_html = _get_player_page(player_id, session=session, min_spacing=PLAYERPAGE_DELAY_S)
    profile = parse_profile(page_html)

    if probe_body is None:
        js_text = _get(JS_FRAGS.format(player_id=player_id), session=session).text
    else:
        js_text = probe_body
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


def fetch_player_batch(
    names: list[str],
    cache_dir: Optional[Path] = None,
    ttl_hours: float = CACHE_TTL_HOURS,
    session: Optional[requests.Session] = None,
    on_progress: Optional[Callable[[int, str, dict], None]] = None,
    abort_after: int = NETWORK_ABORT_AFTER,
) -> tuple[dict[str, dict], int]:
    """Fetch several players concurrently.

    `jsfrags` tolerates high concurrency (48-way: 46/404s, 0 blocks); `player.cgi`
    does not (2-way: 43/429), so the two are fetched in separate pools. The
    profile page is fetched through a single lock with a 1.5 s spacing.

    Returns (results, consecutive_network_failures). A caller can stop the
    pass when `consecutive_network_failures >= abort_after` — every remaining
    player would fail the same way, so the job reports the fault instead of
    spending a timeout per player.
    """
    if not names:
        return {}, 0

    sess = session or requests.Session()
    cache_dir = Path(cache_dir) if cache_dir is not None else None
    results: dict[str, dict] = {}
    consecutive_net = 0
    abort: Optional[Exception] = None
    lock = threading.Lock()

    # ---- Phase 1: resolve all names in parallel (jsfrags probes) ----
    def resolve_one(name: str):
        nonlocal consecutive_net, abort
        with lock:
            if abort is not None:
                return name, None, [], None
        tried: list[str] = []
        try:
            player_id, tried = resolve_player(name, session=sess)
        except ResolutionNetworkError as exc:
            with lock:
                consecutive_net += 1
                if consecutive_net >= abort_after:
                    abort = exc
            return name, None, tried, exc
        except UnresolvedPlayer as exc:
            return name, None, exc.tried, exc
        except Exception as exc:
            return name, None, tried, exc

        if player_id is None:
            return name, None, tried, None

        # Re-download jsfrags here so fetch_player can reuse it — the probe
        # already proved this id exists, so this is a second 254 KB fetch.
        # Parallel across all players: jsfrags tolerates high concurrency.
        try:
            js_text = _get(JS_FRAGS.format(player_id=player_id), session=sess).text
        except Exception as exc:
            with lock:
                consecutive_net += 1
                if consecutive_net >= abort_after:
                    abort = exc
            return name, None, tried, exc
        return name, player_id, tried, js_text

    with _TPE(max_workers=JSFRAGS_WORKERS) as ex:
        for name, player_id, tried, js_text_or_exc in ex.map(resolve_one, names):
            if player_id is None:
                exc = js_text_or_exc
                if isinstance(exc, ResolutionNetworkError):
                    payload = _error_payload(name, exc)
                elif isinstance(exc, UnresolvedPlayer):
                    payload = {
                        "player_name": name, "player_id": None, "tried": tried,
                        "fetched_at": datetime.now().isoformat(timespec="seconds"),
                        "unresolved": True, "profile": {}, "tables": {},
                    }
                elif exc is None:
                    # TA answered and had no such name — a permanent absence
                    payload = {
                        "player_name": name, "player_id": None, "tried": tried,
                        "fetched_at": datetime.now().isoformat(timespec="seconds"),
                        "unresolved": True, "profile": {}, "tables": {},
                    }
                else:
                    payload = _error_payload(name, exc)
                if cache_dir is not None:
                    save_cached(cache_dir, payload)
                results[name] = payload
                if on_progress:
                    on_progress(len(results), name, payload)
                continue

            # ---- Phase 2: profile page (throttled) + tables (reused) ----
            assert isinstance(js_text_or_exc, str), js_text_or_exc
            try:
                page_html = _get_player_page(
                    player_id, session=sess, min_spacing=PLAYERPAGE_DELAY_S)
                profile = parse_profile(page_html)
                fragment = extract_player_frag(js_text_or_exc)
                tables = parse_all_tables(fragment)
                payload = {
                    "player_name": name, "player_id": player_id, "tried": tried,
                    "fetched_at": datetime.now().isoformat(timespec="seconds"),
                    "profile": profile, "tables": tables,
                }
            except Exception as exc:
                payload = _error_payload(name, exc)
            if cache_dir is not None:
                save_cached(cache_dir, payload)
            results[name] = payload
            if on_progress:
                on_progress(len(results), name, payload)

    return results, consecutive_net


# --------------------------------------------------------------- cache -----
def _error_payload(name: str, exc: Exception) -> dict:
    """What a failed fetch stores: `error` (retried by the next pass), never
    `unresolved` — that flag is reserved for names Tennis Abstract answered
    about and did not have, so a network blip cannot blank a player for good.
    """
    payload = {
        "player_name": name, "player_id": None,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "profile": {}, "tables": {}, "error": str(exc),
    }
    tried = getattr(exc, "tried", None)
    if tried:                               # id variants probed before dying
        payload["tried"] = list(tried)
    return payload


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
    """Cache-first fetch.

    Returns a payload with `"unresolved": True` when TA *answered* and had no
    such name. `ResolutionNetworkError` deliberately propagates: the caller
    stores it as a retriable `error`, so a network blip never becomes a
    permanent "no data" row.
    """
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
