/**
 * winamax_core.js — shared, UI-free extraction core for Winamax.
 *
 * Single source of truth used by:
 *   1. the Tampermonkey userscript (built into ~/Projets/cotes/useful-tool/script.js
 *      by tennis_stats/tools/build_userscript.py — do not edit script.js by hand),
 *   2. the tennis_stats Playwright driver (core/winamax.py injects this file and
 *      calls the functions below through page.evaluate()).
 *
 * Rules:
 *   - pure DOM/XPath over the rendered page, no network calls, no clipboard, no UI;
 *   - every selector here is covered by tests/test_winamax_core.py fixtures;
 *   - anything clipboard/UI-specific belongs in the userscript wrapper.
 *
 * Public API (window.WinamaxCore):
 *   sleep, xpathNodes, findScrollableContainer,
 *   matchPagePrefix, sportsPageUrl,
 *   extractMatchInfo(card), extractCurrentMatches({includeLive}),
 *   scanMatches({stopAtNightGap, includeLive, onProgress}) -> Promise<Match[]>,
 *   formatMatches(matches) -> String,
 *   getMiddleColumnPlayers(), isSectionHeading(span), hasAssociatedOdds(el),
 *   findSectionHeadings(), extractSectionData(headingEl, filter) -> Promise<Section|null>,
 *   formatSectionRow(row), formatSection(section), formatAllOddsText(odds),
 *   extractOddsJSON({delayMs, onProgress}) -> Promise<OddsJSON>
 *
 * Match  = {matchId, url, tournament, round, player1, player2, matchTime,
 *           dayIndicator, dayOffset, live, oddP1, oddP2, pctP1, pctP2}
 * Row    = {t:'row',   label, value}          -> "label : value"
 *        | {t:'group', label}                 -> "label"            (sub-group heading)
 *        | {t:'team',  label}                 -> "=== label ==="    (player market team)
 *        | {t:'player',label, cells:[{label, value, spaced}]}
 *                                              -> "  label | c | c"
 * Section= {title, rows, buttonCount}
 * OddsJSON = {url, players, sections, sectionCount, oddCount, buttonCount}
 */
(function () {
    'use strict';

    // --- Shared helpers -------------------------------------------------
    function sleep(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }

    function xpathNodes(xpath, context) {
        if (!context) context = document;
        const result = document.evaluate(xpath, context, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
        const nodes = [];
        for (let i = 0; i < result.snapshotLength; i++) nodes.push(result.snapshotItem(i));
        return nodes;
    }

    function xpathFirst(xpath, context) {
        if (!context) context = document;
        return document.evaluate(xpath, context, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
    }

    function xpathBool(xpath, context) {
        if (!context) context = document;
        return document.evaluate(xpath, context, null, XPathResult.BOOLEAN_TYPE, null).booleanValue;
    }

    // --- Page containers ------------------------------------------------
    const matchPagePrefix = 'https://www.winamax.fr/paris-sportifs/match/';
    const sportsPageUrl = 'https://www.winamax.fr/paris-sportifs/sports/5';

    function findScrollableContainer() {
        const grid = xpathFirst("//*[contains(concat(' ', normalize-space(@class), ' '), ' ReactVirtualized__Grid ')]");
        if (!grid) return window;
        let el = grid.parentElement;
        while (el && el !== document.body) {
            const style = getComputedStyle(el);
            if ((style.overflowY === 'auto' || style.overflowY === 'scroll') && el.scrollHeight > el.clientHeight) return el;
            el = el.parentElement;
        }
        return window;
    }

    function scrollTopOf(sc) { return sc === window ? window.scrollY : sc.scrollTop; }
    function scrollToPos(sc, pos) { sc === window ? window.scrollTo(0, pos) : (sc.scrollTop = pos); }
    function scrollByPx(sc, px) {
        if (sc === window) window.scrollBy(0, px);
        else sc.scrollBy({ top: px, behavior: 'auto' });
    }
    function atBottom(sc) {
        if (sc === window) return window.innerHeight + window.scrollY >= document.body.scrollHeight - 100;
        return sc.scrollTop + sc.clientHeight >= sc.scrollHeight - 100;
    }

    // --- Player name extraction from match page header -------------------
    // Two player names from the middleColumn header, last-name form, or null.
    function getMiddleColumnPlayers() {
        const middleColumn = xpathFirst("//*[@data-testid='middleColumn']");
        if (!middleColumn) return null;

        const playerDivs = xpathNodes(
            ".//div[string-length(normalize-space(text())) > 2 and contains(text(), ' ') and not(contains(text(), ':')) and not(contains(text(), 'ATP')) and not(contains(text(), 'WTA')) and not(contains(text(), '1/')) and not(contains(text(), 'Extract')) and not(starts-with(normalize-space(text()), '€')) and not(contains(text(), '€'))]",
            middleColumn
        );

        const players = [];
        for (const div of playerDivs) {
            const name = div.textContent.trim();
            if (name && /^[A-ZÀ-ÖØ-ÿ]/.test(name) && !/^\d/.test(name) && !players.includes(name)) {
                players.push(name.split(' ').at(-1));
                if (players.length >= 2) break;
            }
        }
        return players.length >= 2 ? players : null;
    }

    // --- Sports list: match cards ---------------------------------------
    const dayIndicators = ['demain', 'sam', 'dim', 'lun', 'mar', 'mer', 'jeu', 'ven'];

    function isDayIndicator(text) {
        const lower = text.toLowerCase();
        return dayIndicators.some(ind => lower === ind || lower.startsWith(ind + '.'));
    }

    function looksLikePlayerName(text) {
        if (!text || text.length < 2) return false;
        if (/^\d+(?:[.,]\d+)?$/.test(text)) return false;
        if (isDayIndicator(text)) return false;
        return /[A-ZÀ-ÖØ-öø-ÿ]/.test(text) || /^[a-zA-ZÀ-ÖØ-öø-ÿ\s.-]{2,}$/.test(text);
    }

    // Status words shown instead of/next to players on live cards.
    const liveStatusWords = ['en direct', 'direct', 'interrompu', 'abandon', 'terminé', 'termine',
        'suspendu', 'forfait', 'live', 'pending', 'à venir', 'a venir'];

    function isLiveStatus(text) {
        const lower = text.toLowerCase().trim();
        return liveStatusWords.some(w => lower === w);
    }

    /**
     * Parse one `match-card-*` element.
     * @param {Element} card
     * @param {{includeLive?: boolean}} opts  includeLive keeps cards without a start
     *        time (in-play matches) instead of returning null.
     * @returns {Match|null}
     */
    function extractMatchInfo(card, opts) {
        opts = opts || {};
        const texts = [];
        const walker = document.createTreeWalker(card, NodeFilter.SHOW_TEXT);
        while (walker.nextNode()) {
            const text = walker.currentNode.textContent.trim();
            if (text.length > 0) texts.push(text);
        }

        let tournamentIdx = -1;
        for (let i = 0; i < texts.length; i++) {
            if (texts[i].includes('ATP') || texts[i].includes('WTA')) { tournamentIdx = i; break; }
        }
        if (tournamentIdx === -1) return null;

        let tournament = texts[tournamentIdx];
        tournament = tournament.replace(/\s+\d+(?:[.,]\d+)?$/, '').trim();
        if (tournament.toLowerCase().includes('double')) return null;

        const testid = card.getAttribute('data-testid') || '';
        const matchId = testid.startsWith('match-card-') ? testid.slice('match-card-'.length) : '';

        let timeIdx = -1;
        let dayIndicator = '';
        for (let i = 0; i < texts.length; i++) {
            const t = texts[i];
            if (/^\d{1,2}:\d{2}$/.test(t)) {
                timeIdx = i;
                if (i > 0 && isDayIndicator(texts[i - 1])) dayIndicator = texts[i - 1];
                break;
            }
        }

        let matchTime = '';
        let player1 = '';
        let player2 = '';
        let live = false;

        if (timeIdx !== -1) {
            matchTime = texts[timeIdx];
            for (let i = timeIdx - 1; i >= 0; i--) {
                const t = texts[i];
                if (isDayIndicator(t)) continue;
                if (looksLikePlayerName(t)) { player1 = t; break; }
            }
            for (let i = timeIdx + 1; i < texts.length; i++) {
                const t = texts[i];
                if (looksLikePlayerName(t)) { player2 = t; break; }
            }
        } else if (opts.includeLive) {
            // In-play card: no start time, first two name-like texts are the players.
            live = true;
            const candidates = [];
            for (let i = tournamentIdx + 1; i < texts.length && candidates.length < 2; i++) {
                const t = texts[i];
                if (isDayIndicator(t) || isLiveStatus(t)) continue;
                if (!looksLikePlayerName(t)) continue;
                if (!/\s/.test(t)) continue;           // require "First Last"
                candidates.push(t);
            }
            if (candidates.length === 2) {
                [player1, player2] = candidates;
            }
        }

        if (!player1 || !player2 || player1 === player2) return null;
        if (!live && timeIdx === -1) return null;

        let round = '';
        if (timeIdx !== -1) {
            for (let i = tournamentIdx + 1; i < texts.length; i++) {
                if (/^\d{1,2}:\d{2}$/.test(texts[i])) break;
                if (looksLikePlayerName(texts[i])) break;
                if (!isDayIndicator(texts[i]) && texts[i] !== tournament && !texts[i].includes('ATP') && !texts[i].includes('WTA')) {
                    round = texts[i];
                    break;
                }
            }
        } else if (opts.includeLive) {
            // In-play card (no clock): the round sits between the tournament
            // and the first player — e.g. 'ATP - Shanghai' '1/4' '<player>'.
            // Score digits ('40', '6', …) come after the players, so stop there.
            for (let i = tournamentIdx + 1; i < texts.length; i++) {
                const t = texts[i];
                if (t === tournament || isDayIndicator(t) || isLiveStatus(t)) continue;
                if (looksLikePlayerName(t)) break;
                if (/\d/.test(t) || /^(finale|tour|qualif)/i.test(t)) { round = t; break; }
            }
        }

        // 1X2 odds + implied percentages from the card itself.
        let oddP1 = null, oddP2 = null, pctP1 = null, pctP2 = null;
        const oddBtns = card.querySelectorAll("[data-testid^='odd-button-']");
        const values = [];
        for (const btn of oddBtns) {
            const v = btn.querySelector('.odd-button-value');
            if (v) values.push(v.textContent.trim());
            if (values.length >= 2) break;
        }
        if (values.length >= 2 && values[0] !== '-' && values[1] !== '-') {
            oddP1 = parseFloat(values[0].replace(',', '.'));
            oddP2 = parseFloat(values[1].replace(',', '.'));
            if (Number.isNaN(oddP1)) oddP1 = null;
            if (Number.isNaN(oddP2)) oddP2 = null;
        }
        const pctEls = card.querySelectorAll("[class*='percent-distribution-completeness']");
        const pcts = [];
        for (const el of pctEls) {
            const m = el.textContent.trim().match(/^(\d+(?:[.,]\d+)?)\s*%$/);
            if (m) pcts.push(parseFloat(m[1].replace(',', '.')));
            if (pcts.length >= 2) break;
        }
        if (pcts.length >= 2) { pctP1 = pcts[0]; pctP2 = pcts[1]; }

        return {
            matchId, url: matchId ? matchPagePrefix + matchId : '',
            tournament, round, player1, player2, matchTime, dayIndicator,
            dayOffset: 0, live, oddP1, oddP2, pctP1, pctP2
        };
    }

    function extractCurrentMatches(opts) {
        const matches = [];
        const cards = xpathNodes("//*[starts-with(@data-testid, 'match-card-')]");
        for (const card of cards) {
            try {
                const info = extractMatchInfo(card, opts || {});
                if (info) matches.push(info);
            } catch (err) { console.warn('winamax_core: card parse failed', err); }
        }
        return matches;
    }

    function timeToMinutes(timeStr) {
        const parts = String(timeStr || '').split(':');
        const h = parseInt(parts[0], 10);
        const m = parseInt(parts[1], 10);
        if (Number.isNaN(h) || Number.isNaN(m)) return -1;
        return h * 60 + m;
    }

    /**
     * Scroll the tennis list and collect singles matches (userscript "sports" mode).
     * @param {{stopAtNightGap?: boolean, includeLive?: boolean, onProgress?: Function,
     *          stepPx?: number, delayMs?: number, maxNoNew?: number}} opts
     * @returns {Promise<Match[]>}
     */
    async function scanMatches(opts) {
        opts = opts || {};
        const stopAtNightGap = opts.stopAtNightGap !== false;
        const includeLive = !!opts.includeLive;
        const onProgress = typeof opts.onProgress === 'function' ? opts.onProgress : null;
        const stepPx = opts.stepPx || 1000;
        const delayMs = opts.delayMs == null ? 100 : opts.delayMs;
        const maxNoNew = opts.maxNoNew || 8;

        const todayMatches = new Map();
        const allSeen = new Map();
        const scrollable = findScrollableContainer();
        const NIGHT_GAP_THRESHOLD = 360; // 6 hours in minutes
        let stoppedByNightGap = false;
        let lastRawMinutes = -1;
        let lastNormalized = -1;
        let dayOffset = 0;

        function shouldKeepMatch(m) {
            if (m.live) return true;              // in-play cards have no clock
            const raw = timeToMinutes(m.matchTime);
            if (raw < 0) return false;

            if (lastRawMinutes === -1) {
                lastRawMinutes = raw;
                lastNormalized = raw;
                return true;
            }

            if (raw < lastRawMinutes) dayOffset += 1440;   // midnight wrap
            const normalized = raw + dayOffset;
            const gap = normalized - lastNormalized;
            if (gap > NIGHT_GAP_THRESHOLD && stopAtNightGap) {
                stoppedByNightGap = true;
                return false;                     // first match of the next session
            }

            lastRawMinutes = raw;
            lastNormalized = normalized;
            return true;
        }

        const scanOpts = { includeLive: includeLive };
        let initialMatches = extractCurrentMatches(scanOpts);
        for (const m of initialMatches) {
            const key = `${m.player1}|${m.player2}|${m.tournament}|${m.round}`;
            allSeen.set(key, m);
            if (stoppedByNightGap) continue;
            if (shouldKeepMatch(m)) {
                m.dayOffset = Math.round(dayOffset / 1440);
                todayMatches.set(key, m);
            }
        }
        if (onProgress) onProgress(todayMatches.size);
        if (stoppedByNightGap) return Array.from(todayMatches.values());

        let noNewCount = 0;
        const originalScrollTop = scrollTopOf(scrollable);

        while (noNewCount < maxNoNew && !stoppedByNightGap) {
            const oldScrollTop = scrollTopOf(scrollable);
            scrollByPx(scrollable, stepPx);
            await sleep(delayMs);
            const newScrollTop = scrollTopOf(scrollable);
            if (newScrollTop === oldScrollTop) break;

            let added = 0;
            for (const m of extractCurrentMatches(scanOpts)) {
                const key = `${m.player1}|${m.player2}|${m.tournament}|${m.round}`;
                if (allSeen.has(key)) continue;
                allSeen.set(key, m);
                if (stoppedByNightGap) continue;
                if (shouldKeepMatch(m)) {
                    m.dayOffset = Math.round(dayOffset / 1440);
                    todayMatches.set(key, m);
                    added++;
                }
            }
            if (added === 0) noNewCount++;
            else noNewCount = 0;
            if (onProgress) onProgress(todayMatches.size);
            if (atBottom(scrollable)) break;
        }

        scrollToPos(scrollable, originalScrollTop);
        return Array.from(todayMatches.values());
    }

    function formatMatches(matches) {
        return matches.map(m => {
            const suffix = m.round ? ` - ${m.round}` : '';
            return `${m.player1} vs ${m.player2} (${m.tournament}${suffix})`;
        }).join('\n');
    }

    // --- Match page: section discovery ----------------------------------
    function isSectionHeading(span) {
        const childDiv = span.querySelector && span.querySelector(':scope > div');
        const title = ((childDiv && childDiv.textContent) || span.textContent || '').trim();

        if (!title || title.length < 2 || title.length > 40) return false;
        if (/^\d+([.,]\d+)?%?$/.test(title)) return false;
        if (/^(Extract|Plus de|Moins de|Titulaire|Nouveau|NEW|\?)/i.test(title)) return false;
        if (/^[A-Z][a-z]+\s[A-Z][a-z]+$/.test(title) &&
            !/(Résultat|Double chance|Buteur|Buteurs|Score|Nombre|Écart|Total|Match|Vainqueur|Mi-temps|Buts|Tirs|Joueur|Serial)/i.test(title)) {
            return false;
        }
        return true;
    }

    function hasAssociatedOdds(headingEl) {
        let node = headingEl.parentNode;
        while (node && node !== document.body) {
            if (xpathBool(".//div[starts-with(@data-testid, 'odd-button-')]", node)) return true;
            node = node.parentNode;
        }
        return false;
    }

    /**
     * All section headings currently rendered on the match page.
     * Initialises `dataset.originalTitle` on each heading (used by the
     * userscript's per-section buttons and by extractSectionData).
     */
    function findSectionHeadings() {
        const context = xpathFirst(
            "//*[@data-testid='middleColumn'] | //*[contains(@class, 'ReactVirtualized__Grid')]") || document;
        const allSpans = xpathNodes(".//span[div[not(*)]]", context);
        const headings = [];
        for (const span of allSpans) {
            if (!isSectionHeading(span)) continue;
            if (!hasAssociatedOdds(span)) continue;
            if (!span.dataset || !span.dataset.originalTitle) {
                const childDiv = span.querySelector(':scope > div');
                span.dataset.originalTitle =
                    ((childDiv && childDiv.textContent) || span.textContent || '').trim();
            }
            headings.push(span);
        }
        return headings;
    }

    // --- Match page: section expansion ----------------------------------
    async function expandSection(sectionContainer) {
        const currentOdds = xpathNodes(".//div[starts-with(@data-testid, 'odd-button-')]", sectionContainer);

        // <= 2 odds: compact grid view — switch to list view via the layout toggle.
        if (currentOdds.length <= 2) {
            const tabsWrapper = xpathFirst(".//*[contains(@class, 'tabs-wrapper')]", sectionContainer);
            if (tabsWrapper) {
                const toggleDiv = xpathFirst(".//*[local-name()='svg']/parent::*", tabsWrapper);
                if (toggleDiv && toggleDiv.click) {
                    toggleDiv.click();
                    await sleep(500);
                }
            } else {
                const possibleToggles = xpathNodes(".//*[local-name()='svg' and .//*[local-name()='rect']]/parent::*", sectionContainer);
                for (const toggle of possibleToggles) {
                    if (toggle.click && toggle.innerText === '') {
                        toggle.click();
                        await sleep(500);
                        break;
                    }
                }
            }
        }

        const expandBtns = xpathNodes(
            ".//div[contains(text(),'Plus de sélections')]/ancestor::div[contains(@class, 'expand-button') or contains(@class, 'expand')] | .//div[contains(text(),'Plus de sélections')]/..",
            sectionContainer
        );
        for (const btn of expandBtns) {
            if (btn.textContent.trim().includes('Plus de sélections')) {
                btn.click();
                await sleep(200);
            }
        }
    }

    // Player-market sections (bet-group-template with team sub-sections or
    // Buteur/Passeur/Décisif column headers) — but never "Score exact".
    function isPlayerMarketSection(sectionContainer) {
        const hasBetGroupTemplate = xpathBool(".//*[contains(@class, 'bet-group-template')]", sectionContainer);
        if (!hasBetGroupTemplate) return false;

        const headerRow = xpathFirst(
            ".//*[contains(@class, 'bet-group-template')]//*[1][self::div or self::header]", sectionContainer);
        if (headerRow) {
            const headerCells = xpathNodes(".//p", headerRow);
            if (headerCells.length > 0) {
                const headerTexts = headerCells.map(c => c.textContent.trim());
                const hasColumnHeaders = headerTexts.some(h => /buteur|passeur|décisif|but|passe/i.test(h));
                if (hasColumnHeaders) return true;
            }
        }

        const teamContainers = xpathNodes(".//*[contains(@class, 'bet-group-template')]/div", sectionContainer);
        if (teamContainers.length >= 2) {
            let teamsWithPlayers = 0;
            for (const tc of teamContainers) {
                const hasPlayerName = xpathBool(
                    ".//*[self::p or self::span or self::div][string-length(normalize-space(text())) > 3 and contains(text(), ' ') and not(contains(text(), ':')) and not(contains(text(), 'ATP')) and not(contains(text(), 'WTA')) and not(contains(text(), 'Plus de')) and not(contains(text(), 'Moins de')) and not(contains(text(), 'Extract'))]",
                    tc
                );
                if (hasPlayerName) teamsWithPlayers++;
            }
            if (teamsWithPlayers >= 2) return true;
        }
        return false;
    }

    // --- Match page: structured extraction -------------------------------

    // "Double chance" sections: outcome titles with their odds.
    function extractDoubleChanceRows(sectionContainer, title) {
        const rows = [];
        const outcomeTitles = xpathNodes(
            ".//div[contains(@class, 'bet-group-outcome-title')]//div[contains(@class, 'label-text')]",
            sectionContainer
        );
        for (const titleEl of outcomeTitles) {
            const label = titleEl.textContent.trim();
            if (!label) continue;

            let oddsValue = '';
            let current = titleEl;
            while (current && current !== sectionContainer) {
                const oddsEl = xpathFirst(".//span[contains(@class, 'odd-button-value')]", current.parentNode);
                if (oddsEl) {
                    oddsValue = oddsEl.textContent.trim().replace(',', '.');
                    break;
                }
                current = current.parentNode;
            }
            if (label && oddsValue) rows.push({ t: 'row', label, value: oddsValue });
        }
        return rows;
    }

    // Collect odds cells for one player block inside a player-market section.
    function collectPlayerCells(playerBlock, columnHeaders) {
        const oddsWrappers = xpathNodes(
            ".//div[.//div[starts-with(@data-testid, 'odd-button-')]]", playerBlock);

        // Column-based market (headers present): first wrapper only, 1 row of columns.
        if (columnHeaders.length > 0) {
            const wrapper = oddsWrappers.length > 0 ? oddsWrappers[0] : playerBlock;
            const oddButtons = xpathNodes(".//div[starts-with(@data-testid, 'odd-button-')]", wrapper);
            const cells = [];
            oddButtons.forEach((btn, i) => {
                const span = xpathFirst(".//span[contains(@class, 'odd-button-value')]", btn);
                if (!span) return;
                const value = span.textContent.trim().replace(',', '.');
                const colName = (i < columnHeaders.length) ? columnHeaders[i] : `Col${i + 1}`;
                cells.push({ label: colName, value, spaced: false });
            });
            return cells;
        }

        // Threshold ("paliers") market: every wrapper, labelled by outcome name.
        const cells = [];
        for (const wrapper of oddsWrappers) {
            const oddButtons = xpathNodes(".//div[starts-with(@data-testid, 'odd-button-')]", wrapper);
            for (const btn of oddButtons) {
                const valueSpan = xpathFirst(".//span[contains(@class, 'odd-button-value')]", btn);
                if (!valueSpan) continue;
                const oddValue = valueSpan.textContent.trim().replace(',', '.');

                let outcomeName = '';
                const valueContainer = valueSpan.parentElement;
                if (valueContainer && valueContainer.previousElementSibling) {
                    outcomeName = valueContainer.previousElementSibling.textContent.trim();
                }
                if (!outcomeName) {
                    const nameDiv = xpathFirst(
                        ".//*[contains(@class, 'odd-button-name') or contains(@class, 'outcome-name')]", btn);
                    if (nameDiv) outcomeName = nameDiv.textContent.trim();
                }
                if (outcomeName && oddValue) cells.push({ label: outcomeName, value: oddValue, spaced: true });
            }
        }
        return cells;
    }

    function playerNameElementsIn(container) {
        return xpathNodes(
            ".//*[self::p or self::span or self::div][string-length(normalize-space(text())) > 1 and contains(text(), ' ') and not(contains(text(), ':')) and not(contains(text(), 'ATP')) and not(contains(text(), 'WTA')) and not(contains(text(), 'Plus de')) and not(contains(text(), 'Moins de'))]",
            container
        );
    }

    function teamNameElementIn(container) {
        return xpathFirst(
            ".//*[self::p or self::span or self::div][string-length(normalize-space(text())) > 1 and not(contains(text(), 'Plus de sélections')) and not(contains(text(), 'paliers')) and not(contains(text(), 'ATP')) and not(contains(text(), 'WTA'))]",
            container
        );
    }

    // Walk up from a name element to the block that owns the odds buttons.
    function findPlayerBlock(nameEl, boundary) {
        let cur = nameEl.parentNode;
        while (cur && cur !== boundary) {
            if (xpathBool(".//div[starts-with(@data-testid, 'odd-button-')]", cur)) return cur;
            cur = cur.parentNode;
        }
        return nameEl.parentNode;
    }

    // Player-market sections (team split or direct player blocks) -> rows.
    function extractPlayerMarketRows(sectionContainer, title) {
        const columnHeaders = [];
        const headerRow = xpathFirst(
            ".//*[contains(@class, 'bet-group-template')]//*[1][self::div or self::header]", sectionContainer);
        if (headerRow) {
            for (const c of xpathNodes(".//p", headerRow)) columnHeaders.push(c.textContent.trim());
        }

        const teamContainers = xpathNodes(".//*[contains(@class, 'bet-group-template')]/div", sectionContainer);

        function playerRowFor(nameEl, container) {
            const playerName = nameEl.textContent.trim();
            if (!playerName) return null;
            const playerBlock = findPlayerBlock(nameEl, container);
            if (!playerBlock) return null;
            const cells = collectPlayerCells(playerBlock, columnHeaders);
            if (cells.length === 0) return null;
            return { t: 'player', label: playerName, cells };
        }

        // Case 1: team-split format (e.g. "Joueur décisif").
        if (teamContainers.length > 0) {
            const rows = [];
            const singleUntitled = teamContainers.length === 1 && !teamNameElementIn(teamContainers[0]);

            for (const tc of teamContainers) {
                const teamNameEl = teamNameElementIn(tc);
                const teamName = teamNameEl ? teamNameEl.textContent.trim() : '';
                const containerRows = [];
                for (const nameEl of playerNameElementsIn(tc)) {
                    const row = playerRowFor(nameEl, tc);
                    if (row) containerRows.push(row);
                }
                if (containerRows.length === 0) continue;
                if (!singleUntitled && teamName) rows.push({ t: 'team', label: teamName });
                rows.push(...containerRows);
            }
            return rows.length > 0 ? rows : null;
        }

        // Case 2: direct player blocks inside bet-group-template.
        const candidateBlocks = xpathNodes(
            ".//*[contains(@class, 'bet-group-template')]//*[.//div[starts-with(@data-testid, 'odd-button-')]]",
            sectionContainer
        );
        const rows = [];
        for (const block of candidateBlocks) {
            const hasPlayerName = xpathBool(
                ".//*[self::p or self::span or self::div][string-length(normalize-space(text())) > 1 and contains(text(), ' ') and not(contains(text(), ':')) and not(contains(text(), 'ATP')) and not(contains(text(), 'WTA')) and not(contains(text(), 'Plus de')) and not(contains(text(), 'Moins de'))]",
                block
            );
            if (!hasPlayerName) continue;
            const nameEl = playerNameElementsIn(block)[0];
            if (!nameEl) continue;
            const row = playerRowFor(nameEl, block);
            if (row) rows.push(row);
        }
        return rows.length > 0 ? rows : null;
    }

    /**
     * Extract one section (expand it first).
     * @param {Element} headingEl heading span (dataset.originalTitle set)
     * @param {string|null} filter 'over' | 'under' | '<player name>' | null
     * @returns {Promise<{title, rows, buttonCount}|null>}
     */
    async function extractSectionData(headingEl, filter) {
        if (filter === undefined) filter = null;
        const title = headingEl.dataset.originalTitle || headingEl.textContent.trim();
        if (!title || title.length < 2) return null;

        let sectionContainer = null;
        let node = headingEl.parentNode;
        while (node && node !== document.body) {
            if (xpathBool(".//div[starts-with(@data-testid, 'odd-button-')]", node)) {
                sectionContainer = node;
                break;
            }
            node = node.parentNode;
        }
        if (!sectionContainer) return null;

        // Sub-group title for an odd button (walks up while >1 buttons inside).
        function findGroupTitle(btn) {
            let current = btn.parentNode;
            while (current && current !== sectionContainer) {
                const oddButtonsInCurrent = current.querySelectorAll('[data-testid^="odd-button-"]');
                if (oddButtonsInCurrent.length <= 1) {
                    current = current.parentNode;
                    continue;
                }

                const walker = document.createTreeWalker(current, NodeFilter.SHOW_TEXT);
                const candidates = [];
                while (walker.nextNode()) {
                    const text = walker.currentNode.textContent.trim();
                    if (!text) continue;

                    let isInsideBtnOrNoise = false;
                    let anc = walker.currentNode.parentNode;
                    while (anc && anc !== current) {
                        if (anc.dataset && anc.dataset.testid && anc.dataset.testid.startsWith('odd-button-')) {
                            isInsideBtnOrNoise = true;
                            break;
                        }
                        const className = anc.className || '';
                        if (typeof className === 'string' && (
                            className.includes('odd-button-value') ||
                            className.includes('percent-distribution') ||
                            className.includes('completeness')
                        )) {
                            isInsideBtnOrNoise = true;
                            break;
                        }
                        anc = anc.parentNode;
                    }
                    if (isInsideBtnOrNoise) continue;

                    if (text === 'Oui' || text === 'Non' || text === '1' || text === 'N' || text === '2') continue;
                    if (/^\d+([.,]\d+)?%?$/.test(text)) continue;
                    if (text.toLowerCase() === title.toLowerCase()) continue;
                    if (/^(Extract|Plus de|Moins de|Titulaire|Nouveau|NEW|\?)/i.test(text)) continue;

                    candidates.push(text);
                }
                if (candidates.length > 0) return candidates[0];
                current = current.parentNode;
            }
            return '';
        }

        await expandSection(sectionContainer);

        if (title.toLowerCase().includes('chance')) {
            await sleep(300);
            const rows = extractDoubleChanceRows(sectionContainer, title);
            if (rows.length > 0) return { title, rows, buttonCount: countButtons(sectionContainer) };
        }

        const isScoreExact = title.toLowerCase().includes('score exact');
        if (!isScoreExact && isPlayerMarketSection(sectionContainer)) {
            await sleep(300);
            const rows = extractPlayerMarketRows(sectionContainer, title);
            if (rows) return { title, rows, buttonCount: countButtons(sectionContainer) };
            // fall through to standard extraction
        }

        const oddsButtons = xpathNodes(".//div[starts-with(@data-testid, 'odd-button-')]", sectionContainer);
        if (oddsButtons.length === 0) return null;

        const rawOdds = [];
        for (const btn of oddsButtons) {
            const valueSpan = xpathFirst(".//span[contains(@class, 'odd-button-value')]", btn);
            if (!valueSpan) continue;
            const oddValue = valueSpan.textContent.trim();
            const valueContainer = valueSpan.parentElement;

            let name = '';
            // Strategy 1: sibling of valueSpan's parent inside the button wrapper
            const nameDiv = valueContainer ? valueContainer.previousElementSibling : null;
            if (nameDiv) name = nameDiv.textContent.trim();

            // Strategy 2: name classes inside the button itself
            if (!name) {
                const possibleNameDiv = xpathFirst(
                    ".//*[contains(@class, 'odd-button-name') or contains(@class, 'outcome-name')]", btn);
                if (possibleNameDiv) name = possibleNameDiv.textContent.trim();
            }

            // Strategy 3: 'label-text' near the button (never broader classes —
            // they also carry percentage text like "21%").
            if (!name) {
                let current = btn.parentNode;
                for (let depth = 0; depth < 5 && current && current !== sectionContainer; depth++) {
                    const labelNode = xpathFirst(".//*[contains(@class, 'label-text')]", current);
                    if (labelNode) {
                        const text = labelNode.textContent.trim();
                        name = text.replace(/\s*\d+([.,]\d+)?%$/, '').trim();
                        break;
                    }
                    current = current.parentNode;
                }
            }

            // Strategy 4: text-walking fallback inside the row wrapper
            if (!name) {
                let rowWrapper = btn.parentNode;
                for (let depth = 0; depth < 6 && rowWrapper && rowWrapper !== sectionContainer; depth++) {
                    const textCount = document.evaluate(
                        "count(.//text()[normalize-space(.) != ''])",
                        rowWrapper, null, XPathResult.NUMBER_TYPE, null).numberValue;
                    if (textCount > 2) break;
                    rowWrapper = rowWrapper.parentNode;
                }
                if (rowWrapper && rowWrapper !== sectionContainer) {
                    const textNodes = [];
                    const walker = document.createTreeWalker(rowWrapper, NodeFilter.SHOW_TEXT);
                    while (walker.nextNode()) {
                        const val = walker.currentNode.textContent.trim();
                        if (val && val !== oddValue && val !== oddValue.replace('.', ',') && !val.endsWith('%')) {
                            textNodes.push(val);
                        }
                    }
                    if (textNodes.length > 0) name = textNodes[0];
                }
            }

            if (name && oddValue) {
                rawOdds.push({
                    name,
                    value: oddValue.replace(',', '.'),
                    groupTitle: findGroupTitle(btn)
                });
            }
        }

        if (rawOdds.length === 0) return null;

        const rows = [];
        const spreadRegex = /^(.+?)\s+([+-]\d+(?:[.,]\d+)?)$/;
        let spreadCount = 0;
        const byPlayer = {};
        for (const o of rawOdds) {
            const match = o.name.match(spreadRegex);
            if (match) {
                spreadCount++;
                const playerName = match[1].trim();
                const handicap = match[2];
                if (!byPlayer[playerName]) byPlayer[playerName] = [];
                byPlayer[playerName].push({ sortKey: parseFloat(handicap.replace(',', '.')), raw: o });
            }
        }

        if (title.includes('Nombre de jeux') || title.includes('Écart de jeux')) {
            const overOdds = rawOdds.filter(o => o.name.includes('Plus de'));
            const underOdds = rawOdds.filter(o => o.name.includes('Moins de'));
            const sortByValue = (a, b) => parseFloat(a.value) - parseFloat(b.value);
            overOdds.sort(sortByValue);
            underOdds.sort(sortByValue);

            if (title.includes('Nombre de jeux')) {
                if (filter === 'over') {
                    for (const o of overOdds) {
                        rows.push({ t: 'row', label: `Over ${o.name.replace('Plus de ', '')}`, value: o.value });
                    }
                } else if (filter === 'under') {
                    for (const o of underOdds) {
                        rows.push({ t: 'row', label: `Under ${o.name.replace('Moins de ', '')}`, value: o.value });
                    }
                } else {
                    for (const o of overOdds) {
                        rows.push({ t: 'row', label: `Over ${o.name.replace('Plus de ', '')} jeux`, value: o.value });
                    }
                    for (const o of underOdds) {
                        rows.push({ t: 'row', label: `Under ${o.name.replace('Moins de ', '')} jeux`, value: o.value });
                    }
                }
            } else if (title.includes('Écart de jeux')) {
                const playerNames = getMiddleColumnPlayers();
                const p1 = playerNames && playerNames.length >= 2 ? playerNames[0] : null;
                const p2 = playerNames && playerNames.length >= 2 ? playerNames[1] : null;

                const ecartByPlayer = {};
                for (const o of rawOdds) {
                    const spreadMatch = o.name.match(spreadRegex);
                    const playerNamePart = spreadMatch ? spreadMatch[1].trim() : o.name;
                    if (!ecartByPlayer[playerNamePart]) ecartByPlayer[playerNamePart] = [];
                    ecartByPlayer[playerNamePart].push(o);
                }

                let playersToShow = Object.keys(ecartByPlayer);
                if (filter && p1 && p2 && (filter === p1 || filter === p2)) {
                    playersToShow = playersToShow.filter(p => p.includes(filter));
                }
                playersToShow.sort();

                for (const player of playersToShow) {
                    ecartByPlayer[player].sort((a, b) => parseFloat(a.value) - parseFloat(b.value));
                    for (const o of ecartByPlayer[player]) {
                        rows.push({ t: 'row', label: o.name, value: o.value });
                    }
                }
            }
        } else if (spreadCount > rawOdds.length / 2) {
            const sortedPlayers = Object.keys(byPlayer).sort();
            for (const player of sortedPlayers) {
                byPlayer[player].sort((a, b) => a.sortKey - b.sortKey);
                for (const item of byPlayer[player]) {
                    rows.push({ t: 'row', label: item.raw.name, value: item.raw.value });
                }
            }
        } else {
            const groups = new Map();
            for (const o of rawOdds) {
                if (!groups.has(o.groupTitle)) groups.set(o.groupTitle, []);
                groups.get(o.groupTitle).push(o);
            }
            for (const [groupTitle, groupOdds] of groups.entries()) {
                if (groupTitle) rows.push({ t: 'group', label: groupTitle });
                for (const o of groupOdds) rows.push({ t: 'row', label: o.name, value: o.value });
            }
        }

        if (rows.length === 0) return null;
        return { title, rows, buttonCount: oddsButtons.length };
    }

    function countButtons(container) {
        return xpathNodes(".//div[starts-with(@data-testid, 'odd-button-')]", container).length;
    }

    // --- Formatting (userscript clipboard output) ------------------------
    function formatSectionRow(row) {
        switch (row.t) {
            case 'group': return row.label;
            case 'team': return `=== ${row.label} ===`;
            case 'player':
                return '  ' + row.label + ' | ' +
                    row.cells.map(c => c.label + (c.spaced ? ' : ' : ': ') + c.value).join(' | ');
            default: return `${row.label} : ${row.value}`;
        }
    }

    function isOverUnderTitle(title) {
        return title.includes('Nombre de jeux') || title.includes('Écart de jeux');
    }

    // One section as clipboard text (title first, unless Over/Under section).
    function formatSection(section) {
        const lines = [];
        if (!isOverUnderTitle(section.title)) lines.push(section.title);
        for (const row of section.rows) lines.push(formatSectionRow(row));
        return lines.join('\n');
    }

    // Whole match page as clipboard text (legacy "Extract all odds" output).
    function formatAllOddsText(odds) {
        const out = [];
        for (const section of odds.sections) {
            if (!isOverUnderTitle(section.title)) out.push(section.title);
            for (const row of section.rows) out.push(formatSectionRow(row));
            out.push('');
        }
        while (out.length > 0 && out[out.length - 1] === '') out.pop();
        return out.join('\n');
    }

    /**
     * Scroll a match page, expanding every section, and return all odds as JSON.
     * @param {{delayMs?: number, stepPx?: number, maxNoNew?: number,
     *          onProgress?: Function}} opts
     * @returns {Promise<OddsJSON>}
     */
    async function extractOddsJSON(opts) {
        opts = opts || {};
        const delayMs = opts.delayMs == null ? 250 : opts.delayMs;
        const stepPx = opts.stepPx || 500;
        const maxNoNew = opts.maxNoNew || 15;
        const onProgress = typeof opts.onProgress === 'function' ? opts.onProgress : null;

        const scrollable = findScrollableContainer();
        const sections = [];
        const processed = new Set();
        let buttonCount = 0;

        scrollToPos(scrollable, 0);
        await sleep(300);

        let prevScrollTop = -1;
        let noNewSectionsCount = 0;
        while (noNewSectionsCount < maxNoNew) {
            let foundNew = false;
            for (const heading of findSectionHeadings()) {
                if (processed.has(heading)) continue;
                processed.add(heading);
                const data = await extractSectionData(heading, null);
                if (data) {
                    sections.push(data);
                    buttonCount += data.buttonCount;
                    foundNew = true;
                    if (onProgress) onProgress(sections.length, countOddsRows(data.rows));
                }
            }

            noNewSectionsCount = foundNew ? 0 : noNewSectionsCount + 1;

            const oldScrollTop = scrollTopOf(scrollable);
            scrollByPx(scrollable, stepPx);
            await sleep(delayMs);
            const newScrollTop = scrollTopOf(scrollable);
            if (newScrollTop === oldScrollTop && prevScrollTop === newScrollTop) break;
            prevScrollTop = oldScrollTop;

            if (scrollable === window) {
                if (window.innerHeight + window.scrollY >= document.body.scrollHeight - 100) break;
            } else {
                if (scrollable.scrollTop + scrollable.clientHeight >= scrollable.scrollHeight - 100) break;
            }
        }

        const odds = {
            url: location.href,
            players: getMiddleColumnPlayers(),
            sections,
            sectionCount: sections.length,
            buttonCount
        };
        odds.oddCount = countOddsRows(formatAllOddsText(odds).split('\n'));
        return odds;
    }

    // rows: array of Row objects; textLines: array of formatted lines
    function countOddsRows(x) {
        if (Array.isArray(x) && x.length > 0 && typeof x[0] === 'string') {
            return x.filter(line => line.includes(':')).length;
        }
        const rows = Array.isArray(x) ? x : [];
        return rows.filter(r => r.t === 'row' || r.t === 'player').length;
    }

    // --- Public API ------------------------------------------------------
    window.WinamaxCore = {
        sleep,
        xpathNodes,
        findScrollableContainer,
        matchPagePrefix,
        sportsPageUrl,
        dayIndicators,
        isDayIndicator,
        looksLikePlayerName,
        extractMatchInfo,
        extractCurrentMatches,
        timeToMinutes,
        scanMatches,
        formatMatches,
        getMiddleColumnPlayers,
        isSectionHeading,
        hasAssociatedOdds,
        findSectionHeadings,
        expandSection,
        isPlayerMarketSection,
        extractSectionData,
        formatSectionRow,
        isOverUnderTitle,
        formatSection,
        formatAllOddsText,
        extractOddsJSON
    };
})();
