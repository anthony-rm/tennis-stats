"""Background-job behaviour: default targets, force refresh, live data events.

No network and no browser: the Winamax session and the Tennis Abstract fetch
are replaced by fakes, so only the orchestration (targets, store writes, SSE
events) is exercised.
"""
from __future__ import annotations

import asyncio

import pytest

from app import jobs as jobs_mod
from app.paths import app_version
from app.store import Store

ODDS = {"sectionCount": 1, "oddCount": 2, "buttonCount": 2, "sections": []}


def _match(mid: str, p1: str = "Novak Djokovic", p2: str = "Iga Swiatek",
           live: int = 0) -> dict:
    return {
        "match_id": mid, "url": f"https://www.winamax.fr/paris-sportifs/match/{mid}",
        "tournament": "ATP - Shanghai", "round": "1/4",
        "player1": p1, "player2": p2,
        "start_local": "12:00", "day_offset": 0, "day_indicator": "",
        "live": live,
    }


class FakeSession:
    """Stands in for core.winamax.WinamaxSession.collect_odds()."""

    fetched: list[str] = []

    def __init__(self, on_event=None, delay_range=(1.0, 2.0)):
        self.on_event = on_event
        self.delay_range = delay_range

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def collect_odds(self, match_id: str) -> dict:
        FakeSession.fetched.append(str(match_id))
        return dict(ODDS)


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def _run(jm, start):
    """Start one job, await it, return (job snapshot, drained SSE events)."""
    async def main():
        queue = jm.subscribe()
        job = start()
        await asyncio.wait_for(jm._tasks[job["kind"]], 30)
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        return jm.snapshot()[job["kind"]], events

    return asyncio.run(main())


# ------------------------------------------------------------------ odds ---
def test_odds_default_pass_is_missing_only_whole_list(tmp_path, monkeypatch, store):
    monkeypatch.setattr(jobs_mod, "WinamaxSession", FakeSession)
    FakeSession.fetched = []
    store.upsert_matches([
        _match("1"),
        _match("2", p1="Alexander Zverev", p2="Daniil Medvedev"),
        _match("3", p1="A. Rublev", p2="S. Tsitsipas", live=1),
    ])
    store.set_odds("1", ODDS)
    jm = jobs_mod.JobManager(store, tmp_path / "cache")

    job, events = _run(jm, lambda: jm.start_odds())

    # the whole list is considered (no line selection): match 1 already has
    # odds and match 3 is live, so only match 2 is fetched
    assert FakeSession.fetched == ["2"]
    assert job["status"] == "done"
    assert job["result"] == {"fetched": 1, "failed": []}
    assert store.get_match("2")["odds_json"]
    # one live "data" event per stored match so the grid can refresh mid-pass
    kinds = [e["type"] for e in events]
    assert kinds.count("data") == 1
    assert "job" in kinds


def test_odds_force_refetches_matches_that_already_show_odds(tmp_path, monkeypatch, store):
    monkeypatch.setattr(jobs_mod, "WinamaxSession", FakeSession)
    FakeSession.fetched = []
    store.upsert_matches([_match("1"), _match("2", p1="A. Rublev", p2="S. Tsitsipas")])
    store.set_odds("1", ODDS)
    jm = jobs_mod.JobManager(store, tmp_path / "cache")

    job, _ = _run(jm, lambda: jm.start_odds(force=True))
    assert sorted(FakeSession.fetched) == ["1", "2"]
    assert job["result"] == {"fetched": 2, "failed": []}

    # everything has odds now: the default pass has nothing left to do
    FakeSession.fetched = []
    job2, _ = _run(jm, lambda: jm.start_odds())
    assert FakeSession.fetched == []
    assert job2["message"] == "nothing to fetch"


def test_odds_explicit_ids_are_taken_verbatim(tmp_path, monkeypatch, store):
    monkeypatch.setattr(jobs_mod, "WinamaxSession", FakeSession)
    FakeSession.fetched = []
    store.upsert_matches([_match("1"), _match("2", p1="A. Rublev", p2="S. Tsitsipas")])
    store.set_odds("1", ODDS)
    jm = jobs_mod.JobManager(store, tmp_path / "cache")

    job, _ = _run(jm, lambda: jm.start_odds(["1"]))
    assert FakeSession.fetched == ["1"]
    assert job["result"] == {"fetched": 1, "failed": []}


def test_odds_explicit_empty_scope_fetches_nothing(tmp_path, monkeypatch, store):
    """An explicit ``match_ids: []`` is "nobody selected", never "the whole
    list" — an empty scope must never widen on its own."""
    monkeypatch.setattr(jobs_mod, "WinamaxSession", FakeSession)
    FakeSession.fetched = []
    store.upsert_matches([_match("1")])
    store.set_odds("1", ODDS)
    jm = jobs_mod.JobManager(store, tmp_path / "cache")

    job, _ = _run(jm, lambda: jm.start_odds([]))
    assert FakeSession.fetched == []
    assert job["message"] == "nothing to fetch"
    # ... not even with force: the explicit scope wins over "refresh all"
    job2, _ = _run(jm, lambda: jm.start_odds([], force=True))
    assert FakeSession.fetched == []
    assert job2["message"] == "nothing to fetch"


# ----------------------------------------------------------------- stats ---
def _fake_ta(fetched: list):
    def get_player(name, cache_dir, session=None):
        fetched.append(name)
        return {"player_name": name, "player_id": 7, "profile": {}, "tables": {}}
    return get_player


def test_stats_default_pass_is_missing_only_whole_list(tmp_path, monkeypatch, store):
    fetched: list[str] = []
    monkeypatch.setattr(jobs_mod.ta, "get_player", _fake_ta(fetched))
    store.upsert_matches([_match("1")])
    store.upsert_stats("Novak Djokovic",
                       {"player_name": "Novak Djokovic", "profile": {}, "tables": {}})
    jm = jobs_mod.JobManager(store, tmp_path / "cache")

    job, events = _run(jm, lambda: jm.start_stats())

    # the stored player is skipped, the missing one is fetched (whole list)
    assert fetched == ["Iga Swiatek"]
    assert job["result"] == {"stored": 1, "unresolved": []}
    assert [e["type"] for e in events].count("data") == 1
    assert store.get_stats()["Iga Swiatek"]["player_id"] == 7

    # everything is stored now -> a second pass fetches nothing
    fetched.clear()
    job2, _ = _run(jm, lambda: jm.start_stats())
    assert fetched == []
    assert job2["message"] == "player stats already up to date"
    assert job2["result"] == {"stored": 0, "unresolved": []}


def test_stats_retries_stored_errors_but_keeps_unresolved(tmp_path, monkeypatch, store):
    fetched: list[str] = []
    monkeypatch.setattr(jobs_mod.ta, "get_player", _fake_ta(fetched))
    store.upsert_matches([_match("1")])
    store.upsert_stats("Novak Djokovic",
                       {"player_name": "Novak Djokovic", "profile": {}, "tables": {},
                        "error": "network blip"})
    store.upsert_stats("Iga Swiatek",
                       {"player_name": "Iga Swiatek", "unresolved": True,
                        "profile": {}, "tables": {}})
    jm = jobs_mod.JobManager(store, tmp_path / "cache")

    job, _ = _run(jm, lambda: jm.start_stats())

    assert fetched == ["Novak Djokovic"]      # error retried, unresolved kept
    assert "error" not in store.get_stats()["Novak Djokovic"]
    assert job["result"] == {"stored": 1, "unresolved": []}


def test_stats_explicit_names_scope_the_pass(tmp_path, monkeypatch, store):
    """``{names}`` narrows the pass to the players of the selected lines; the
    missing / stored-error rule still decides inside that scope."""
    fetched: list[str] = []
    monkeypatch.setattr(jobs_mod.ta, "get_player", _fake_ta(fetched))
    store.upsert_matches([_match("1"), _match("2", p1="Alexander Zverev",
                                               p2="Daniil Medvedev")])
    store.upsert_stats("Novak Djokovic",
                       {"player_name": "Novak Djokovic", "profile": {}, "tables": {}})
    store.upsert_stats("Alexander Zverev",
                       {"player_name": "Alexander Zverev", "profile": {}, "tables": {},
                        "error": "network blip"})
    jm = jobs_mod.JobManager(store, tmp_path / "cache")

    job, _ = _run(jm, lambda: jm.start_stats(
        ["Novak Djokovic", "Iga Swiatek", "Alexander Zverev"]))

    # stored payload kept, stored error retried, the players of the other
    # match never fetched (they are outside the scope)
    assert fetched == ["Iga Swiatek", "Alexander Zverev"]
    assert job["result"] == {"stored": 2, "unresolved": []}
    assert "Daniil Medvedev" not in store.get_stats()

    # an empty scope selects nobody instead of falling back to the whole list
    fetched.clear()
    job2, _ = _run(jm, lambda: jm.start_stats([]))
    assert fetched == []
    assert job2["message"] == "no player selected"


def _raising_ta(exc_factory):
    """A get_player that raises `exc_factory(name)` — the outage simulation."""
    def get_player(name, cache_dir, session=None):
        raise exc_factory(name)
    return get_player


def test_stats_stores_network_faults_as_retriable_errors(tmp_path, monkeypatch, store):
    from core.tennisabstract import ResolutionNetworkError
    monkeypatch.setattr(
        jobs_mod.ta, "get_player",
        _raising_ta(lambda n: ResolutionNetworkError(n, [n], "ConnectTimeout: dns down")))
    store.upsert_matches([_match("1")])        # two players, below the abort limit
    jm = jobs_mod.JobManager(store, tmp_path / "cache")

    job, _ = _run(jm, lambda: jm.start_stats())

    assert job["status"] == "done"
    assert job["result"] == {"stored": 2, "unresolved": []}
    rows = store.get_stats()
    assert sorted(rows) == ["Iga Swiatek", "Novak Djokovic"]
    for row in rows.values():
        assert "ConnectTimeout" in row["error"]
        assert not row.get("unresolved"), "a dead request is never 'player absent'"
        assert row["tried"] == [row["player_name"]]

    # ... so the next pass picks them up again (they are errors, not unresolved)
    fetched: list[str] = []
    monkeypatch.setattr(jobs_mod.ta, "get_player", _fake_ta(fetched))
    job2, _ = _run(jm, lambda: jm.start_stats())
    assert sorted(fetched) == ["Iga Swiatek", "Novak Djokovic"]
    assert job2["result"] == {"stored": 2, "unresolved": []}


def test_stats_pass_stops_after_repeated_network_failures(tmp_path, monkeypatch, store):
    from core.tennisabstract import ResolutionNetworkError
    fetched: list[str] = []
    monkeypatch.setattr(
        jobs_mod.ta, "get_player",
        _raising_ta(lambda n: (fetched.append(n),
                               ResolutionNetworkError(n, [n], "read timed out"))[1]))
    store.upsert_matches([_match("1"), _match("2", p1="Alexander Zverev",
                                               p2="Daniil Medvedev")])   # four players
    jm = jobs_mod.JobManager(store, tmp_path / "cache")

    job, _ = _run(jm, lambda: jm.start_stats())

    assert job["status"] == "error"
    assert "Tennis Abstract unreachable" in job["message"]
    # the pass gives up instead of burning a timeout on every remaining player
    assert len(fetched) == jobs_mod.NETWORK_ABORT_AFTER
    assert job["result"]["stored"] == jobs_mod.NETWORK_ABORT_AFTER
    assert job["result"]["unresolved"] == []
    assert "check your Internet connection" in job["result"]["suggestion"]
    rows = store.get_stats()
    assert len(rows) == jobs_mod.NETWORK_ABORT_AFTER
    assert all("error" in r and not r.get("unresolved") for r in rows.values())


def test_stats_network_counter_resets_on_a_successful_fetch(tmp_path, monkeypatch, store):
    from core.tennisabstract import ResolutionNetworkError
    calls = {"n": 0}

    def get_player(name, cache_dir, session=None):
        calls["n"] += 1
        if calls["n"] in (1, 2, 4):            # fail, fail, ok, fail
            raise ResolutionNetworkError(name, [name], "blip")
        return {"player_name": name, "player_id": 7, "profile": {}, "tables": {}}

    monkeypatch.setattr(jobs_mod.ta, "get_player", get_player)
    store.upsert_matches([_match("1"), _match("2", p1="Alexander Zverev",
                                               p2="Daniil Medvedev")])
    jm = jobs_mod.JobManager(store, tmp_path / "cache")

    job, _ = _run(jm, lambda: jm.start_stats())

    assert job["status"] == "done"             # the counter restarted on success
    rows = store.get_stats()
    assert len(rows) == 4
    assert sum(1 for r in rows.values() if "error" in r) == 3


# ---------------------------------------------------------------- version ---
def test_app_version_reads_the_version_file():
    version = app_version()
    assert version and version != "dev"
    assert version.count(".") >= 1
    from pathlib import Path
    assert version == (Path(__file__).parent.parent / "VERSION") \
        .read_text(encoding="utf-8").strip()
