"""Playwright driver for Winamax — list scan + per-match odds pass.

Design rules (proven by the feasibility probes in PLAN.md §3):
  * the site is an SPA fed by socket.io: plain HTTP gets an empty shell, so a real
    browser (channel="chrome") is required;
  * a full page.goto() of a /match/<id> deep link is blocked by CloudFront WAF
    (403 "Request blocked") — NEVER deep-link: one long-lived page, navigate by
    clicking cards and history.back();
  * core/winamax_core.js holds every selector; this file only drives the browser.

Usage:
    from core.winamax import WinamaxSession
    async with WinamaxSession() as wx:
        matches = await wx.scan_today()
        odds = await wx.collect_odds(matches[0]["match_id"])
"""
from __future__ import annotations

import asyncio
import random
import re
from pathlib import Path
from typing import Callable, Optional

from playwright.async_api import Browser, Page, async_playwright

LIST_URL = "https://www.winamax.fr/paris-sportifs/sports/5"
CORE_JS = (Path(__file__).parent / "winamax_core.js").read_text(encoding="utf-8")

# Realistic desktop context (French locale + Paris timezone matter for kickoff times).
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

BLOCK_MARKER = "Request blocked"


class WinamaxBlocked(RuntimeError):
    """CloudFront WAF blocked the page (403)."""


class WinamaxSession:
    """One long-lived browser page used for both scanning and odds collection."""

    def __init__(
        self,
        headless: bool = True,
        delay_range: tuple[float, float] = (1.0, 2.0),
        on_event: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.headless = headless
        self.delay_range = delay_range
        self.on_event = on_event or (lambda msg: None)
        self._pw = None
        self._browser: Browser | None = None
        self.page: Page | None = None
        self._headed_fallback_done = False

    # -- lifecycle ------------------------------------------------------
    async def __aenter__(self) -> "WinamaxSession":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._launch(self.headless)
        self.page = await self._new_page()
        await self._goto_list()

    async def _launch(self, headless: bool) -> Browser:
        return await self._pw.chromium.launch(
            channel="chrome",
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )

    async def _new_page(self) -> Page:
        ctx = await self._browser.new_context(
            locale="fr-FR",
            timezone_id="Europe/Paris",
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
        )
        page = await ctx.new_page()
        return page

    async def _goto_list(self) -> None:
        assert self.page is not None
        resp = await self.page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60000)
        if resp is not None and resp.status >= 400:
            raise WinamaxBlocked(f"list load failed with HTTP {resp.status}")
        await self.page.wait_for_timeout(5000)
        # the SPA renders its match list asynchronously — wait for real content
        # instead of trusting a fixed delay (a too-early scan sees zero cards)
        try:
            await self.page.wait_for_selector("[data-testid^='match-card-']", timeout=25000)
            self.on_event("list page ready")
        except Exception:
            self.on_event("list page loaded but no match cards rendered yet")
        await self._assert_not_blocked()
        await self._ensure_core()

    async def close(self) -> None:
        try:
            if self._browser:
                await self._browser.close()
        finally:
            if self._pw:
                await self._pw.stop()
                self._pw = None

    # -- shared core ----------------------------------------------------
    async def _ensure_core(self) -> None:
        assert self.page is not None
        ok = await self.page.evaluate("() => !!window.WinamaxCore")
        if not ok:
            await self.page.add_script_tag(content=CORE_JS)
            ok = await self.page.evaluate("() => !!window.WinamaxCore")
            if not ok:
                raise RuntimeError("winamax_core.js failed to load in the page")

    async def _assert_not_blocked(self) -> None:
        assert self.page is not None
        try:
            content = await self.page.content()
        except Exception as exc:  # page crashed / navigated away
            raise WinamaxBlocked(str(exc)) from exc
        if BLOCK_MARKER in content:
            raise WinamaxBlocked("CloudFront 403: Request blocked")

    # -- WAF recovery: retry once in a visible window -------------------
    async def _recover_blocked(self) -> None:
        if self._headed_fallback_done:
            raise WinamaxBlocked("WAF block repeated after headed-mode retry")
        self.on_event("blocked — retrying in headed mode")
        self._headed_fallback_done = True
        await self.close()
        self._pw = await async_playwright().start()
        self._browser = await self._launch(headless=False)
        self.page = await self._new_page()
        await asyncio.sleep(2)
        await self._goto_list()

    # -- Phase 1: list scan ---------------------------------------------
    async def scan_today(
        self,
        include_live: bool = True,
        stop_at_night_gap: bool = True,
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> list[dict]:
        """Scroll the tennis list and return singles matches (see winamax_core.js)."""
        assert self.page is not None
        await self._ensure_core()
        matches = []
        for attempt in range(2):
            try:
                matches = await self.page.evaluate(
                    "async (opts) => window.WinamaxCore.scanMatches(opts)",
                    {
                        "stopAtNightGap": stop_at_night_gap,
                        "includeLive": include_live,
                        "delayMs": 120,
                        "stepPx": 1000,
                        "maxNoNew": 8,
                    },
                )
            except Exception:
                await self._maybe_recover()
                raise
            if matches or attempt == 1:
                break
            # zero matches usually means the virtual list had not rendered yet
            self.on_event("empty scan — waiting for the list and retrying")
            await self.page.wait_for_timeout(4000)
            await self._ensure_core()
        if on_progress:
            on_progress(len(matches))
        return matches or []

    # -- Phase 2: odds pass ---------------------------------------------
    async def collect_odds(
        self, match_id: str, max_scroll_steps: int = 60, capture: Path | None = None
    ) -> dict:
        """Click into a match page, extract every market as JSON, go back.

        Returns the OddsJSON object from winamax_core.js (sections/rows/counts).
        On a WAF block: relaunch in headed mode once and retry the match.
        If `capture` is set, the match page DOM (post-extraction) is saved there
        for the fixture-based selector tests.
        """
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                return await self._collect_odds_once(
                    match_id, max_scroll_steps, capture=capture
                )
            except WinamaxBlocked as exc:
                last_exc = exc
                if attempt == 1:
                    raise
                await self._recover_blocked()
        raise last_exc if last_exc else RuntimeError("unreachable")

    async def _collect_odds_once(
        self, match_id: str, max_scroll_steps: int, capture: Path | None = None
    ) -> dict:
        assert self.page is not None
        if "/paris-sportifs/sports/" not in self.page.url:
            raise WinamaxBlocked(f"not on the list page (url={self.page.url})")
        await self._ensure_core()

        card_sel = f'[data-testid="match-card-{match_id}"]'
        if not await self._scroll_to_card(card_sel, max_scroll_steps):
            raise LookupError(f"match card {match_id} not found in the list DOM")

        try:
            await self.page.locator(card_sel).first.click(timeout=15000)
            await self.page.wait_for_url(f"**/match/{match_id}", timeout=25000)
            await self.page.wait_for_selector(
                "[data-testid^='odd-button-']", timeout=30000, state="attached"
            )
            await self._assert_not_blocked()
        except WinamaxBlocked:
            raise
        except Exception as exc:
            # Click did not navigate: WAF interstitial or layout change.
            await self._assert_not_blocked()
            raise RuntimeError(f"could not open match {match_id}: {exc}") from exc

        await self.page.wait_for_timeout(1200)
        await self._ensure_core()
        odds = await self.page.evaluate(
            "async () => window.WinamaxCore.extractOddsJSON({delayMs: 250, stepPx: 500})"
        )
        if capture is not None:
            # capture from the top of the page: main markets are always there
            await self.page.evaluate(
                "() => { const sc = window.WinamaxCore.findScrollableContainer();"
                " if (sc === window) window.scrollTo(0, 0); else sc.scrollTop = 0; }"
            )
            await self.page.wait_for_timeout(1500)
            capture.parent.mkdir(parents=True, exist_ok=True)
            capture.write_text(await self.page.content(), encoding="utf-8")

        await self._go_back_to_list()
        await asyncio.sleep(random.uniform(*self.delay_range))
        return odds

    async def _go_back_to_list(self) -> None:
        assert self.page is not None
        for _ in range(3):
            await self.page.go_back(wait_until="domcontentloaded", timeout=30000)
            await self.page.wait_for_timeout(2500)
            try:
                await self._assert_not_blocked()
                if await self.page.locator("[data-testid^='match-card-']").count() > 0:
                    await self._ensure_core()
                    return
            except WinamaxBlocked:
                raise
            except Exception:
                continue
        # Fallback: reload the list (allowed — only /match deep links are WAF-blocked)
        await self._goto_list()

    # -- helpers ---------------------------------------------------------
    async def _scroll_to_card(self, selector: str, max_steps: int) -> bool:
        """Bring a (virtualized) match card into the DOM. Two passes: from the
        current position, then from the top of the list."""
        assert self.page is not None
        for attempt in range(2):
            if attempt == 1:
                await self.page.evaluate(
                    "() => { const sc = window.WinamaxCore.findScrollableContainer();"
                    " if (sc === window) window.scrollTo(0, 0); else sc.scrollTop = 0; }"
                )
                await self.page.wait_for_timeout(400)
            for _ in range(max_steps):
                if await self.page.locator(selector).count() > 0:
                    return True
                moved = await self.page.evaluate(
                    "() => { const sc = window.WinamaxCore.findScrollableContainer();"
                    " const before = sc === window ? window.scrollY : sc.scrollTop;"
                    " if (sc === window) window.scrollBy(0, 900); else sc.scrollBy({top: 900});"
                    " const after = sc === window ? window.scrollY : sc.scrollTop;"
                    " return after !== before; }"
                )
                await self.page.wait_for_timeout(120)
                if not moved:
                    break
        return await self.page.locator(selector).count() > 0

    async def _maybe_recover(self) -> None:
        """If the page is WAF-blocked, relaunch headed and reload the list once."""
        try:
            await self._assert_not_blocked()
        except WinamaxBlocked:
            await self._recover_blocked()


def match_url(match_id: str) -> str:
    return f"https://www.winamax.fr/paris-sportifs/match/{match_id}"


def is_valid_match_id(match_id: str) -> bool:
    return bool(re.fullmatch(r"\d{6,12}", str(match_id)))
