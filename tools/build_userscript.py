#!/usr/bin/env python3
"""Build the Tampermonkey userscript from the shared extraction core.

Sources:
    core/winamax_core.js                  (extraction logic — shared with Playwright)
    <cotes>/useful-tool/src/ui.js          (userscript UI / clipboard / navigation)
Output:
    <cotes>/useful-tool/script.js          (self-contained userscript — GENERATED,
                                            never edit by hand; v9.28 backup kept as
                                            script.js.bak-v9.28)

Usage:
    python tools/build_userscript.py [--check]

--check builds into a temp file and only reports whether the on-disk script.js
is up to date (exit 1 if stale) — useful in tests/CI.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CORE_JS = PROJECT_ROOT / "core" / "winamax_core.js"

USERSCRIPT_DIR = Path.home() / "Projets" / "cotes" / "useful-tool"
UI_JS = USERSCRIPT_DIR / "src" / "ui.js"
OUTPUT = USERSCRIPT_DIR / "script.js"

HEADER = """// ==UserScript==
// @name         Winamax Tools
// @version      9.29
// @description  Extracts betting odds from Winamax. GENERATED FILE: built from tennis_stats/core/winamax_core.js + src/ui.js (python tools/build_userscript.py) — edit those, not this one.
// @match        https://www.winamax.fr/*
// @updateURL    https://raw.githubusercontent.com/anthony-rm/useful-tool/main/script.js
// @downloadURL  https://raw.githubusercontent.com/anthony-rm/useful-tool/main/script.js
// @grant        GM_setClipboard
// @grant        GM.setClipboard
// ==/UserScript==
"""


def build() -> str:
    core = CORE_JS.read_text(encoding="utf-8").rstrip("\n")
    ui = UI_JS.read_text(encoding="utf-8").rstrip("\n")
    for path, text in ((CORE_JS, core), (UI_JS, ui)):
        if not text:
            sys.exit(f"error: {path} is empty")
    return (
        HEADER
        + "\n(function() {\n    'use strict';\n\n"
        + "// ================= shared extraction core (winamax_core.js) =================\n"
        + core
        + "\n\n// ================= userscript UI (src/ui.js) =================\n"
        + ui
        + "\n})();\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="verify script.js matches the sources (no write)")
    args = parser.parse_args()

    content = build()

    if args.check:
        on_disk = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if on_disk == content:
            print(f"OK: {OUTPUT} is up to date")
            return 0
        print(f"STALE: {OUTPUT} differs from core+ui sources — rerun build_userscript.py")
        return 1

    OUTPUT.write_text(content, encoding="utf-8")
    print(f"wrote {OUTPUT} ({len(content.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
