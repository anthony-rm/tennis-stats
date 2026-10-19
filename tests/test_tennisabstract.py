"""Tennis Abstract name -> id resolution — offline, no sockets.

The 2026-10-19 network blip (broken VPN/DoH path on the machine) recorded
eight famous players as `unresolved` because a dead request was read as
"player absent". These tests pin the distinction: only *answers* (404/200)
decide, silence raises `ResolutionNetworkError`, and TA's own player list
covers ids spelled differently than the display name (a middle name).
"""
from __future__ import annotations

import pytest
import requests

from core import tennisabstract as ta


class _Resp(requests.Response):
    def __init__(self, status_code: int, text: str = "") -> None:
        super().__init__()
        self.status_code = status_code
        self._content = text.encode("utf-8")


class FakeSession(requests.Session):
    """requests.Session stand-in replaying a script of `(status, body)` tuples
    or exceptions to raise; the last item repeats once the script runs out."""

    def __init__(self, script: list) -> None:
        super().__init__()
        self.script = list(script)
        self.calls = 0

    def get(self, url, *args, **kwargs):
        self.calls += 1
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return _Resp(*item)


class ProbeStub:
    """Stands in for `ta._probe`: one scripted result per call, the last one
    repeating; `.calls` records the probed urls."""

    def __init__(self, results: list) -> None:
        self.results = list(results)
        self.calls: list[str] = []

    def __call__(self, url, session=None, **kw):
        self.calls.append(url)
        result = self.results[min(len(self.calls) - 1, len(self.results) - 1)]
        if len(result) == 2:            # (status, body) shorthand -> full triple
            return (result[0], result[1], None)
        return result


def _probe_stub(results: list) -> ProbeStub:
    return ProbeStub(results)


@pytest.fixture(autouse=True)
def _clean_playerlist_cache():
    ta._PLAYERLIST_CACHE.clear()
    yield
    ta._PLAYERLIST_CACHE.clear()


# ------------------------------------------------------------------ probe ---
def test_probe_retries_a_dead_request_then_succeeds():
    session = FakeSession([
        requests.ConnectionError("dns down"),
        requests.ConnectionError("dns down"),
        (200, "var player_frag = `x`;"),
    ])
    status, body, reason = ta._probe("https://example/x.js", session=session,
                                     backoff=(0.0, 0.0))
    assert (status, reason) == (200, None)
    assert body and "player_frag" in body
    assert session.calls == ta.PROBE_ATTEMPTS


def test_probe_reports_silence_after_every_attempt():
    session = FakeSession([requests.Timeout("read timed out")])
    status, body, reason = ta._probe("https://example/x.js", session=session,
                                     backoff=(0.0, 0.0))
    assert (status, body) == (None, None)
    assert reason and "Timeout" in reason
    assert session.calls == ta.PROBE_ATTEMPTS


def test_probe_reads_a_404_as_an_answer():
    session = FakeSession([(404, "not here")])
    status, body, reason = ta._probe("https://example/x.js", session=session,
                                     backoff=(0.0, 0.0))
    assert (status, body, reason) == (404, "not here", None)
    assert session.calls == 1                # a real 404 is never retried


def test_probe_treats_throttling_as_silence():
    session = FakeSession([(429, "slow down")])
    status, _, reason = ta._probe("https://example/x.js", session=session,
                                  backoff=(0.0, 0.0))
    assert status is None and reason == "HTTP 429"
    assert session.calls == ta.PROBE_ATTEMPTS


# ------------------------------------------------------------- candidates ---
def test_id_candidates_drop_the_generational_suffix():
    assert "MartinDamm" in ta.id_candidates("Martin Damm Jr")
    assert ta.id_candidates("Martin Damm Jr")[0] == "MartinDammJr"  # literal first
    assert ta.id_candidates("Novak Djokovic") == ["NovakDjokovic"]


# ------------------------------------------------------------ name lookup ---
def test_match_player_name_exact_and_middle_name():
    entries = ["(M) Thiago Agustin Tirante", "(M) Novak Djokovic", "(W) Iga Swiatek"]
    assert ta.match_player_name("Novak Djokovic", entries) == "(M) Novak Djokovic"
    assert ta.match_player_name("Thiago Tirante", entries) == "(M) Thiago Agustin Tirante"
    assert ta.match_player_name("Iga Swiatek", entries) == "(W) Iga Swiatek"


def test_match_player_name_refuses_to_guess():
    entries = ["(M) Martin Damm", "(M) Martin Damm Sr", "(M) Novak Djokovic"]
    assert ta.match_player_name("Martin Damm", entries) is None   # two candidates
    assert ta.match_player_name("Zhang Zhizhen", entries) is None  # absent
    assert ta.match_player_name("", entries) is None


def test_load_playerlist_parses_and_memoises(monkeypatch):
    calls: list[str] = []

    class R:
        text = 'var playerlist=["(M) Novak Djokovic", "(W) Iga Swiatek"];\r\n'

    monkeypatch.setattr(ta, "_get",
                        lambda url, session=None, timeout=30:
                        (calls.append(url), R)[1])
    first = ta.load_playerlist()
    second = ta.load_playerlist()
    assert first == ["(M) Novak Djokovic", "(W) Iga Swiatek"]
    assert first is second and calls == [ta.PLAYERLIST_URL]   # fetched once


def test_load_playerlist_rejects_an_unexpected_format(monkeypatch):
    class R:
        text = "/* something else entirely */"

    monkeypatch.setattr(ta, "_get", lambda *a, **k: R())
    with pytest.raises(ValueError):
        ta.load_playerlist()


# ------------------------------------------------------------- resolution ---
def test_resolve_returns_the_first_answered_id(monkeypatch):
    monkeypatch.setattr(ta, "_probe", _probe_stub([(200, "var player_frag = `x`;")]))
    player_id, tried = ta.resolve_player("Novak Djokovic")
    assert player_id == "NovakDjokovic"
    assert tried == ["NovakDjokovic"]


def test_resolve_raises_when_nothing_ever_answered(monkeypatch):
    monkeypatch.setattr(ta, "_probe",
                        _probe_stub([(None, None, "ConnectTimeout: dns down")]))
    with pytest.raises(ta.ResolutionNetworkError) as exc:
        ta.resolve_player("Marin Cilic")
    assert exc.value.name == "Marin Cilic"
    assert "ConnectTimeout" in str(exc.value)
    assert exc.value.tried == ["MarinCilic"]


def test_resolve_falls_back_to_tas_own_player_list(monkeypatch):
    monkeypatch.setattr(ta, "_probe",
                        _probe_stub([(404, "nope"), (200, "var player_frag = `x`;")]))
    monkeypatch.setattr(ta, "load_playerlist",
                        lambda session=None: ["(M) Thiago Agustin Tirante"])
    player_id, tried = ta.resolve_player("Thiago Tirante")
    assert player_id == "ThiagoAgustinTirante"
    assert tried == ["ThiagoTirante", "ThiagoAgustinTirante"]
    assert ta.PLAYERLIST_URL not in tried


def test_resolve_stays_unresolved_when_ta_answers_and_has_no_match(monkeypatch):
    monkeypatch.setattr(ta, "_probe", _probe_stub([(404, "nope")]))
    monkeypatch.setattr(ta, "load_playerlist", lambda session=None: ["(M) Novak Djokovic"])
    player_id, tried = ta.resolve_player("Zhang Zhizhen")
    assert player_id is None
    assert tried == ["ZhangZhizhen"]


def test_resolve_raises_when_the_player_list_itself_is_unreachable(monkeypatch):
    monkeypatch.setattr(ta, "_probe", _probe_stub([(404, "nope")]))

    def dead_list(session=None):
        raise requests.ConnectionError("DoH is down")

    monkeypatch.setattr(ta, "load_playerlist", dead_list)
    with pytest.raises(ta.ResolutionNetworkError):
        ta.resolve_player("Zhang Zhizhen")
