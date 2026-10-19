"""Selection -> clipboard text (LLM-friendly default, TSV / Markdown / JSON).

Selection shape (as stored by app/store.py and POSTed to /api/export):
    { "<match_id>": ["col.id", "col.id", ...], ...}

Formats
  * **llm** (default) — one readable block per match: a "## N. P1 vs P2"
    heading, one "Label: value" line per selected field grouped under
    "### <group>" headings, player pairs expanded to real player names and
    "P1/P2 +x" gaps rewritten with the player names. No tables, no truncated
    cells, no ambiguous "a | b" — built to be pasted straight into an LLM.
  * tsv / md / md_block / json — rectangular or per-match exports (unchanged).

Rules (documented in README):
  * the first output column/line is always the match label ("P1 vs P2") so a
    pasted fragment still identifies its rows;
  * a column appears iff at least one cell of it is selected;
  * a row appears iff at least one of its cells is selected;
  * unselected cells inside a kept row are emitted as empty strings, which
    keeps TSV rectangular for Excel/Google Sheets.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Iterable

from app.columns import COLUMN_ORDER, COLUMNS, GROUPS

FORMATS = ("llm", "tsv", "md", "md_block", "json")

_GUIDE = (
    "Reading guide:",
    '- every match is a block: "## N. P1 vs P2" then one field per line, "- Label: value";',
    '- fields holding both players are written as "<player> <value>, <player> <value>"'
    " (first player = first named in the match line);",
    '- "—" means not available (stats not fetched, fetch failed, player'
    " unresolved, or absent on Tennis Abstract);",
    "- section headings (###) are the dashboard's column groups.",
)


def _clean(text: str) -> str:
    return " ".join(str(text).replace("\t", " ").splitlines()).strip()


def _selected_columns(cells: dict) -> list[str]:
    wanted = {col for cols in cells.values() for col in cols}
    return [c for c in COLUMN_ORDER if c in wanted]


def _label(row: dict) -> str:
    return f"{row.get('player1') or '?'} vs {row.get('player2') or '?'}"


def _llm_line(col_id: str, value: str, p1: str, p2: str) -> str:
    """'- Label: value' with player pairs / gap prefixes made explicit."""
    label = COLUMNS[col_id].label if col_id in COLUMNS else col_id
    text = _clean(value)
    col = COLUMNS.get(col_id)
    if col is not None and col.pair and " | " in text:
        left, _, right = text.partition(" | ")
        text = f"{p1} {left}, {p2} {right}"
    elif text.startswith("P1 "):
        text = f"{p1}{text[2:]}"
    elif text.startswith("P2 "):
        text = f"{p2}{text[2:]}"
    return f"- {label}: {text}"


def _export_llm(kept: list[dict], cells: dict, columns: list[str]) -> str:
    group_of = {c.id: g for g in GROUPS for c in g.columns}
    today = date.today().isoformat()
    n = len(kept)
    lines = [
        f"# Tennis matches — {today} — {n} match{'es' if n != 1 else ''}",
        "",
        *_GUIDE,
        "",
    ]
    for i, r in enumerate(kept, 1):
        p1 = r.get("player1") or "P1"
        p2 = r.get("player2") or "P2"
        selected = set(cells.get(str(r.get("match_id"))) or [])
        vals = r.get("values") or {}
        lines.append(f"## {i}. {p1} vs {p2}")
        current_group = None
        for col_id in columns:
            if col_id not in selected:
                continue
            g = group_of.get(col_id)
            if g is not None and g.id != current_group:
                current_group = g.id
                lines.append(f"### {g.id}. {g.label}")
            lines.append(_llm_line(col_id, vals.get(col_id, ""), p1, p2))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def export_text(rows: Iterable[dict], cells: dict, fmt: str = "llm") -> str:
    rows = list(rows)
    columns = _selected_columns(cells)
    if not columns:
        return ""

    kept = [r for r in rows if cells.get(str(r.get("match_id")))]
    if not kept:
        return ""

    if fmt == "llm":
        return _export_llm(kept, cells, columns)

    if fmt == "json":
        payload = [
            {
                "match_id": r.get("match_id"),
                "match": _label(r),
                "values": {
                    col: (r.get("values") or {}).get(col, "")
                    for col in columns
                    if col in cells.get(str(r.get("match_id")), [])
                },
            }
            for r in kept
        ]
        return json.dumps(payload, ensure_ascii=False, indent=2)

    if fmt == "md_block":
        blocks = []
        for r in kept:
            chosen = [c for c in columns if c in cells.get(str(r.get("match_id")), [])]
            blocks.append(f"## {_label(r)}\n")
            blocks.append("| | |")
            blocks.append("|---|---|")
            for col in chosen:
                label = COLUMNS[col].label if col in COLUMNS else col
                value = _clean((r.get("values") or {}).get(col, ""))
                blocks.append(f"| {label} | {value} |")
            blocks.append("")
        return "\n".join(blocks).rstrip() + "\n"

    # rectangular table formats -------------------------------------------
    header = ["Match"] + [COLUMNS[c].label if c in COLUMNS else c for c in columns]
    body = []
    for r in kept:
        vals = r.get("values") or {}
        selected = set(cells.get(str(r.get("match_id")), []))
        body.append(
            [_label(r)] + [_clean(vals.get(col, "")) if col in selected else "" for col in columns]
        )

    if fmt == "md":
        lines = [
            "| " + " | ".join(header) + " |",
            "|" + "|".join(["---"] * len(header)) + "|",
        ]
        lines += ["| " + " | ".join(row) + " |" for row in body]
        return "\n".join(lines) + "\n"

    # tsv
    lines = ["\t".join(header)]
    lines += ["\t".join(row) for row in body]
    return "\n".join(lines) + "\n"
