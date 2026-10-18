"""Selector regression tests for core/winamax_core.js against saved fixtures.

The fixtures are real DOM snapshots captured by core/winamax.py:
    tests/fixtures/list_page.html     — Winamax tennis list (match cards)
    tests/fixtures/match_page.html    — a match page after a full odds pass

The core runs in real Chrome (channel="chrome") because it is plain DOM/XPath
code that must execute in a browser. Run:

    .venv/bin/python -m pytest tests/ -q
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

FIXTURES = Path(__file__).parent / "fixtures"
CORE_JS = (Path(__file__).parent.parent / "core" / "winamax_core.js").read_text(encoding="utf-8")

LIST_FIXTURE = FIXTURES / "list_page.html"
MATCH_FIXTURE = FIXTURES / "match_page.html"


@pytest.fixture(scope="session")
def browser_loop():
    """One event loop + one browser for the whole session."""
    import asyncio

    loop = asyncio.new_event_loop()

    async def _launch():
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(channel="chrome", headless=True)
        return pw, browser

    pw, browser = loop.run_until_complete(_launch())

    class Ctx:
        def __init__(self):
            self.loop, self.pw, self.browser = loop, pw, browser

        def open(self, fixture: Path):
            async def _go():
                page = await self.browser.new_page()
                # fixtures must be offline-deterministic: only the local file loads
                async def _handler(route, request):
                    if request.url.startswith("file:"):
                        await route.continue_()
                    else:
                        await route.abort()
                await page.route("**/*", _handler)
                await page.goto(fixture.as_uri(), wait_until="domcontentloaded")
                errors: list[str] = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                await page.add_script_tag(content=CORE_JS)
                await page.wait_for_timeout(300)
                errors.clear()   # only errors from core execution count from here on
                return page, errors
            return loop.run_until_complete(_go())

        def run(self, coro):
            return loop.run_until_complete(coro)

        async def close_all(self):
            await self.browser.close()
            await self.pw.stop()

    ctx = Ctx()
    yield ctx
    ctx.run(ctx.close_all())
    loop.close()


# ----------------------------------------------------------- list fixture ---
def test_core_loads(browser_loop):
    page, errors = browser_loop.open(LIST_FIXTURE)
    assert browser_loop.run(page.evaluate("() => typeof window.WinamaxCore")) == "object"
    assert errors == []


def test_list_scan_extracts_match_ids(browser_loop):
    page, _ = browser_loop.open(LIST_FIXTURE)
    matches = browser_loop.run(page.evaluate(
        "() => window.WinamaxCore.scanMatches({stopAtNightGap: true, includeLive: true, delayMs: 10})"))
    assert len(matches) >= 3, "expected the fixture list to yield matches"
    for m in matches:
        assert m["matchId"].isdigit(), m
        assert m["url"].endswith(m["matchId"])
        assert m["tournament"].startswith(("ATP", "WTA")), m
        assert m["player1"] and m["player2"] and m["player1"] != m["player2"]
    # known fixture content: an upcoming Shanghai match with 1X2 odds + %
    medvedev = next((m for m in matches if m["player1"] == "Daniil Medvedev"), None)
    assert medvedev is not None, [m["player1"] for m in matches]
    assert medvedev["matchId"] == "75620252"
    assert medvedev["matchTime"] == "12:00"
    assert medvedev["live"] is False
    assert medvedev["oddP1"] and medvedev["oddP2"]
    assert medvedev["pctP1"] is not None and medvedev["pctP2"] is not None
    # live cards are kept (no clock) when includeLive is set
    assert any(m["live"] for m in matches), "fixture contains an in-play card"
    # ... and their round is scraped too (it sits right after the tournament)
    for m in matches:
        if m["live"]:
            assert m["round"] == "1/4", m


def test_doubles_filtered_out(browser_loop):
    page, _ = browser_loop.open(LIST_FIXTURE)
    matches = browser_loop.run(page.evaluate(
        "() => window.WinamaxCore.scanMatches({includeLive: true, delayMs: 10})"))
    assert all("double" not in m["tournament"].lower() for m in matches)
    # fixture has a Wuhan doubles card — it must never appear
    assert all("Doubles" not in m["tournament"] for m in matches)


def test_userscript_mode_excludes_live(browser_loop):
    """The userscript path (includeLive: false) matches legacy behaviour."""
    page, _ = browser_loop.open(LIST_FIXTURE)
    matches = browser_loop.run(page.evaluate(
        "() => window.WinamaxCore.scanMatches({delayMs: 10})"))
    assert all(not m["live"] for m in matches)
    assert len(matches) >= 1


def test_format_matches_output(browser_loop):
    page, _ = browser_loop.open(LIST_FIXTURE)
    text = browser_loop.run(page.evaluate(
        "() => { const m = window.WinamaxCore.scanMatches({delayMs: 10});"
        " return m.then(list => window.WinamaxCore.formatMatches(list)); }"))
    assert " vs " in text
    assert "(" in text and ")" in text


# ---------------------------------------------------------- match fixture ---
def test_match_headings_discovered(browser_loop):
    page, _ = browser_loop.open(MATCH_FIXTURE)
    # DOM nodes don't survive JSON serialization — map to titles inside the page
    titles = browser_loop.run(page.evaluate(
        "() => window.WinamaxCore.findSectionHeadings().map(h => h.dataset.originalTitle)"))
    assert len(titles) >= 3, titles
    assert any("Vainqueur" in t for t in titles), titles
    # heading filter must not pick up numbers / UI text
    assert not any(t.replace(".", "").isdigit() for t in titles if t)


def test_section_extraction_produces_rows(browser_loop):
    page, _ = browser_loop.open(MATCH_FIXTURE)
    result = browser_loop.run(page.evaluate(
        """async () => {
            const heads = window.WinamaxCore.findSectionHeadings();
            const preferred = heads.find(x => x.dataset.originalTitle.includes('Vainqueur'));
            const ordered = preferred ? [preferred, ...heads] : heads;
            for (const h of ordered) {
                const data = await window.WinamaxCore.extractSectionData(h, null);
                if (data && data.rows && data.rows.length) {
                    return {title: data.title, rows: data.rows, n: data.rows.length};
                }
            }
            return {missing: true, titles: heads.map(x => x.dataset.originalTitle)};
        }"""))
    assert "missing" not in result, result
    row_types = {r["t"] for r in result["rows"]}
    assert row_types <= {"row", "group", "team", "player"}, row_types
    values = [r["value"] for r in result["rows"] if r["t"] == "row"]
    assert values, result
    for v in values:
        float(str(v).replace(",", "."))   # odds must parse as numbers

    # legacy clipboard formatting still produces "label : value" lines
    text = browser_loop.run(page.evaluate(
        "(t) => window.WinamaxCore.formatSection(t)", result))
    lines = text.split("\n")
    assert lines[0] == result["title"]
    assert any(" : " in ln for ln in lines)


def test_extract_odds_json_structure(browser_loop):
    page, _ = browser_loop.open(MATCH_FIXTURE)
    odds = browser_loop.run(page.evaluate(
        "async () => window.WinamaxCore.extractOddsJSON({delayMs: 60, maxNoNew: 4})"))
    assert set(odds) >= {"url", "players", "sections", "sectionCount", "oddCount", "buttonCount"}
    assert odds["sectionCount"] >= 1
    assert odds["oddCount"] >= 10, odds["oddCount"]
    for s in odds["sections"]:
        assert s["title"] and isinstance(s["rows"], list) and s["rows"]
    # formatted legacy text must contain at least one odd line
    text = browser_loop.run(page.evaluate(
        "(o) => window.WinamaxCore.formatAllOddsText(o)", odds))
    assert " : " in text


def test_no_page_errors_on_fixtures(browser_loop):
    for fixture in (LIST_FIXTURE, MATCH_FIXTURE):
        page, errors = browser_loop.open(fixture)
        browser_loop.run(page.evaluate("() => window.WinamaxCore.findSectionHeadings()"))
        assert errors == [], (fixture.name, errors)


# ----------------------------------------------------------- build check ----
def test_userscript_build_is_current():
    # the userscript wrapper lives OUTSIDE this repo (~/Projets/cotes); on a CI
    # runner it is absent, so the freshness check can only run on this machine
    ui_js = Path.home() / "Projets" / "cotes" / "useful-tool" / "src" / "ui.js"
    if not ui_js.exists():
        pytest.skip(f"userscript project not present ({ui_js})")
    import subprocess
    import sys
    build = Path(__file__).parent.parent / "tools" / "build_userscript.py"
    proc = subprocess.run([sys.executable, str(build), "--check"],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
