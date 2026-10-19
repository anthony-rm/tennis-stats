"""Filesystem locations for runtime state (SQLite DB, TA cache, logs).

The desktop build (see ``desktop/``) must not write inside its own bundle, so
it exports ``TENNIS_STATS_DATA_DIR`` (pointing at
``~/Library/Application Support/TennisStats``) *before* importing anything that
resolves a path here. A plain ``git clone`` run keeps using ``<repo>/data``.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_DATA_DIR = "TENNIS_STATS_DATA_DIR"


def data_dir() -> Path:
    """Writable state directory (created on first use)."""
    env = os.environ.get(ENV_DATA_DIR)
    path = Path(env).expanduser() if env else PROJECT_ROOT / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir() -> Path:
    path = data_dir() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_dir() -> Path:
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def clear_cache(path: Optional[Path] = None) -> dict:
    """Delete every cached download (the per-player Tennis Abstract JSON tree).

    The cache directory itself is kept — ``JobManager`` holds its path — but
    the day subdirectories (``<cache>/2026-10-19/*.json``) are removed once
    empty. Returns ``{"files", "bytes", "failed"}``; a file that cannot be
    unlinked is counted, never raised (one busy file must not abort the rest).
    """
    root = Path(path) if path is not None else cache_dir()
    files = total = failed = 0
    for item in sorted(root.rglob("*")):
        if not item.is_file():
            continue
        try:
            size = item.stat().st_size
            item.unlink()
        except OSError:
            failed += 1
            continue
        files += 1
        total += size
    # drop the now-empty day directories, deepest first
    for item in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if item.is_dir():
            try:
                item.rmdir()
            except OSError:
                pass
    return {"files": files, "bytes": total, "failed": failed}


def asset_dir() -> Path:
    """Directory holding bundled read-only files (web UI, userscript core).

    Frozen: PyInstaller's ``_MEIPASS``. Otherwise the project checkout.
    """
    import sys

    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return PROJECT_ROOT


def app_version() -> str:
    """Dashboard version from the VERSION file.

    The desktop build bundles VERSION at the root of the bundle (see
    ``desktop/TennisStats.spec``), so the same lookup works frozen and in a
    checkout; ``dev`` is the last-resort fallback.
    """
    for base in (asset_dir(), PROJECT_ROOT):
        path = base / "VERSION"
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return text
    return "dev"
