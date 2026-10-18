# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the macOS desktop build (onedir + .app bundle).

Build:  bash desktop/build_dmg.sh      (or: pyinstaller desktop/TennisStats.spec)
"""
import pathlib

from PyInstaller.utils.hooks import collect_all

ROOT = pathlib.Path(SPECPATH).resolve().parent
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()

# Playwright ships a node driver (playwright/driver/{node,package/**}) that must
# travel with the bundle: the app drives the *system* Chrome via channel="chrome".
pw_datas, pw_binaries, pw_hidden = collect_all("playwright")

datas = pw_datas + [
    (str(ROOT / "web" / "index.html"), "web"),
    (str(ROOT / "core" / "winamax_core.js"), "core"),
    (str(ROOT / "VERSION"), "."),
]
binaries = pw_binaries

hiddenimports = pw_hidden + [
    "app.main", "app.jobs", "app.store", "app.columns", "app.exporter", "app.paths",
    "core.winamax", "core.tennisabstract",
    # uvicorn resolves these lazily by string
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on",
]

a = Analysis(
    [str(ROOT / "desktop" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "_pytest", "PIL"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Tennis Stats",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Tennis Stats",
)

app = BUNDLE(
    coll,
    name="Tennis Stats.app",
    icon=str(ROOT / "build" / "icon.icns") if (ROOT / "build" / "icon.icns").exists() else None,
    bundle_identifier="com.anthonyrm.tennisstats",
    info_plist={
        "CFBundleName": "Tennis Stats",
        "CFBundleDisplayName": "Tennis Stats",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": "12.0",
        "LSUIElement": True,           # menu-bar app: no Dock icon
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,
    },
)
