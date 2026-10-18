"""Desktop entry point — a menu-bar app around the FastAPI dashboard.

Used by the packaged macOS build (``desktop/TennisStats.spec``) and runnable
from a checkout for development:

    .venv/bin/python desktop/launcher.py

What it does
    1. exports ``TENNIS_STATS_DATA_DIR`` to
       ``~/Library/Application Support/TennisStats`` (frozen build only — the
       app bundle in /Applications must stay read-only), then imports the app;
    2. starts uvicorn on 127.0.0.1 (8000 if free, else the next free port) and
       opens the dashboard in the default browser;
    3. installs a menu-bar item: Open dashboard / Rescan / Check for updates /
       Quit;
    4. polls the GitHub release feed and, when a newer tag exists, offers a
       one-click update that downloads the matching zip, swaps the bundle in
       place and relaunches.

Without PyObjC (e.g. a bare dev venv) it falls back to a plain foreground
process: dashboard open, Ctrl-C to quit.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import platform
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

APP_NAME = "Tennis Stats"
REPO_SLUG = "anthony-rm/tennis-stats"
API_LATEST = f"https://api.github.com/repos/{REPO_SLUG}/releases/latest"
RELEASE_PAGE = f"https://github.com/{REPO_SLUG}/releases/latest"
PREFERRED_PORT = 8000
UPDATE_INTERVAL = 6 * 3600          # re-check every six hours
CHROME_APP = "/Applications/Google Chrome.app"

log = logging.getLogger("launcher")


# --------------------------------------------------------------- paths ----
def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_path() -> Path:
    """The ``.app`` directory when frozen, otherwise the project checkout."""
    if frozen():
        # …/Tennis Stats.app/Contents/MacOS/Tennis Stats -> …/Tennis Stats.app
        return Path(sys.executable).resolve().parents[2]
    return Path(__file__).resolve().parent.parent


def version() -> str:
    root = Path(getattr(sys, "_MEIPASS", bundle_path())) if frozen() else bundle_path()
    try:
        return (root / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def data_dir() -> Path:
    if frozen():
        return Path.home() / "Library" / "Application Support" / "TennisStats"
    return bundle_path() / "data"


def setup_tls() -> None:
    """Point OpenSSL at the bundled CA store.

    A PyInstaller bundle inherits the *build machine's* compiled-in OpenSSL
    paths, which do not exist on the friend's Mac, so plain ``urllib`` HTTPS
    (the update check) fails with CERTIFICATE_VERIFY_FAILED unless we say where
    the CA bundle is. ``requests`` is unaffected (it uses certifi directly).
    """
    try:
        import certifi
    except ImportError:  # pragma: no cover - always present in the bundle
        return
    bundled = Path(getattr(sys, "_MEIPASS", "")) / "certifi" / "cacert.pem"
    ca = bundled if bundled.exists() else Path(certifi.where())
    if ca.exists():
        os.environ.setdefault("SSL_CERT_FILE", str(ca))
        os.environ.setdefault("SSL_CERT_DIR", str(ca.parent))


def setup_logging() -> None:
    target = data_dir() / "logs"
    target.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        target / "launcher.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if not frozen() or sys.stderr is not None:
        stream = logging.StreamHandler()
        stream.setFormatter(handler.formatter)
        root.addHandler(stream)


# -------------------------------------------------------------- server ----
def free_port(start: int = PREFERRED_PORT) -> int:
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def preferred_port() -> int:
    """``TENNIS_STATS_PORT`` overrides the default 8000 (dev/testing)."""
    try:
        return int(os.environ.get("TENNIS_STATS_PORT") or PREFERRED_PORT)
    except ValueError:
        return PREFERRED_PORT


def running_instance(port: int) -> str | None:
    """URL of a dashboard already listening on ``port``, if any."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/jobs", timeout=2) as res:
            if res.status == 200:
                return f"http://127.0.0.1:{port}"
    except Exception:
        pass
    return None


class Dashboard:
    """uvicorn in a daemon thread, driven from the Cocoa run loop."""

    def __init__(self, port: int | None = None) -> None:
        self.port = free_port(port or preferred_port())
        self.url = f"http://127.0.0.1:{self.port}"
        self._server = None

    def start(self, timeout: float = 60.0) -> bool:
        import uvicorn

        from app.main import app  # imports lazily: needs TENNIS_STATS_DATA_DIR set

        config = uvicorn.Config(
            app, host="127.0.0.1", port=self.port,
            log_level="info", access_log=False, log_config=None,
        )
        self._server = uvicorn.Server(config)
        threading.Thread(target=self._server.run, name="uvicorn", daemon=True).start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._server.started:
                log.info("dashboard listening on %s", self.url)
                return True
            time.sleep(0.1)
        log.error("dashboard failed to start within %.0fs", timeout)
        return False

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True

    def post(self, path: str) -> None:
        req = urllib.request.Request(
            self.url + path, data=b"{}",
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=15).read()
            log.info("POST %s ok", path)
        except Exception:
            log.exception("POST %s failed", path)

    def rescan(self) -> None:
        self.post("/api/scan")


def chrome_installed() -> bool:
    return Path(CHROME_APP).exists() or Path.home().joinpath("Applications/Google Chrome.app").exists()


# -------------------------------------------------------------- update ----
def parse_version(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in text.lstrip("vV").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) or (0,)


def asset_name(ver: str) -> str:
    return f"TennisStats-{ver}-{platform.machine()}.zip"


def fetch_latest(current: str) -> dict:
    req = urllib.request.Request(
        API_LATEST,
        headers={"Accept": "application/vnd.github+json", "User-Agent": f"TennisStats/{current}"},
    )
    with urllib.request.urlopen(req, timeout=20) as res:
        return json.load(res)


def latest_release(current: str) -> dict | None:
    """Newer release info (version, zip url, page) or None."""
    try:
        data = fetch_latest(current)
    except Exception:
        log.warning("update check failed", exc_info=True)
        return None
    latest = str(data.get("tag_name") or "").lstrip("vV")
    if not latest or parse_version(latest) <= parse_version(current):
        log.info("up to date (%s, latest %s)", current, latest or "?")
        return None
    assets = {a.get("name"): a.get("browser_download_url") for a in data.get("assets", [])}
    url = assets.get(asset_name(latest)) or assets.get(f"TennisStats-{latest}.zip")
    if not url:
        log.warning("release %s has no asset for %s", latest, platform.machine())
        return None
    return {"version": latest, "url": url, "page": data.get("html_url") or RELEASE_PAGE}


SWAP_SCRIPT = """#!/bin/sh
# Wait for the running dashboard to exit, then replace the bundle and relaunch.
DEST="$1"; NEW="$2"; PID="$3"
i=0
while kill -0 "$PID" 2>/dev/null && [ "$i" -lt 240 ]; do sleep 0.25; i=$((i+1)); done
sleep 0.5
rm -rf "$DEST.old"
mv "$DEST" "$DEST.old" 2>/dev/null || rm -rf "$DEST"
if ditto "$NEW" "$DEST"; then
    xattr -dr com.apple.quarantine "$DEST" 2>/dev/null
    rm -rf "$DEST.old"
    open "$DEST" &
else
    rm -rf "$DEST"
    mv "$DEST.old" "$DEST" 2>/dev/null
fi
"""


def download_and_apply(info: dict) -> str | None:
    """Download the release zip, swap the bundle, relaunch.

    Returns None on success (the caller should quit), or an error message.
    """
    dest = bundle_path()
    if not frozen():
        return "this is a development checkout — update with git"
    if not os.access(dest.parent, os.W_OK):
        return f"cannot write to {dest.parent}"

    work = Path(tempfile.mkdtemp(prefix="tennisstats-update-"))
    archive = work / "update.zip"
    try:
        log.info("downloading %s", info["url"])
        req = urllib.request.Request(info["url"], headers={"User-Agent": "TennisStats/updater"})
        with urllib.request.urlopen(req, timeout=300) as res, archive.open("wb") as out:
            while chunk := res.read(1 << 20):
                out.write(chunk)
        staging = work / "staging"
        staging.mkdir()
        subprocess.run(["ditto", "-x", "-k", str(archive), str(staging)], check=True)
        apps = sorted(staging.glob("*.app"))
        if not apps:
            # zip may carry a top-level folder
            apps = sorted(staging.glob("*/*.app"))
        if not apps:
            return "downloaded archive does not contain an app"
        new_app = apps[0]
        subprocess.run(["chmod", "-R", "u+w", str(new_app)], check=False)

        script = work / "swap.sh"
        script.write_text(SWAP_SCRIPT, encoding="utf-8")
        script.chmod(0o755)
        log.info("swapping %s -> %s", new_app, dest)
        subprocess.Popen(
            ["/bin/sh", str(script), str(dest), str(new_app), str(os.getpid())],
            start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        )
        return None
    except Exception as exc:  # noqa: BLE001 - surfaced to the user
        log.exception("update failed")
        return f"{type(exc).__name__}: {exc}"


# ----------------------------------------------------------- menu bar ----
def run_menubar(dashboard: Dashboard) -> int:
    import objc  # noqa: F401  (PyObjC present => packaged build)
    from AppKit import (
        NSAlert, NSApplication, NSApplicationActivationPolicyAccessory, NSMenu,
        NSMenuItem, NSObject, NSStatusBar, NSVariableStatusItemLength,
    )

    nsapp = NSApplication.sharedApplication()
    nsapp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
    status_item.button().setTitle_("🎾")
    status_item.button().setToolTip_(f"{APP_NAME} {version()}")

    menu = NSMenu.alloc().init()

    def add(title: str, action: str | None, *, hidden: bool = False):
        entry = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, "")
        entry.setTarget_(delegate)
        entry.setHidden_(hidden)
        menu.addItem_(entry)
        return entry

    class Delegate(NSObject):
        # --- menu actions (selectors) --------------------------------
        def openDashboard_(self, sender):
            webbrowser.open(dashboard.url)

        def rescan_(self, sender):
            threading.Thread(target=dashboard.rescan, daemon=True).start()

        def checkUpdates_(self, sender):
            threading.Thread(target=self._look, daemon=True).start()

        def applyUpdate_(self, sender):
            info = pending.get("update")
            if not info:
                return
            update_item.setTitle_(f"Downloading {info['version']}…")
            update_item.setEnabled_(False)
            threading.Thread(target=self._install, args=(info,), daemon=True).start()

        def quit_(self, sender):
            dashboard.stop()
            nsapp.terminate_(None)

        # --- called from worker threads (via performSelectorOnMainThread)
        def markUpdate_(self, payload):
            info = json.loads(payload)
            pending["update"] = info
            update_item.setTitle_(f"Update to {info['version']} available…")
            update_item.setHidden_(False)
            update_item.setEnabled_(True)

        def updateFailed_(self, message):
            update_item.setTitle_("Check for updates…")
            update_item.setEnabled_(True)
            alert("Update failed", str(message))

        # --- workers -------------------------------------------------
        @objc.python_method
        def _look(self):
            info = latest_release(version())
            if info:
                self.performSelectorOnMainThread_withObject_waitUntilDone_(
                    "markUpdate:", json.dumps(info), False)

        @objc.python_method
        def _install(self, info):
            error = download_and_apply(info)
            if error:
                self.performSelectorOnMainThread_withObject_waitUntilDone_(
                    "updateFailed:", error, False)
            else:
                self.performSelectorOnMainThread_withObject_waitUntilDone_(
                    "quitAfterUpdate:", None, False)

        def quitAfterUpdate_(self, _):
            dashboard.stop()
            nsapp.terminate_(None)

        def pollUpdates_(self, _):
            while True:
                time.sleep(UPDATE_INTERVAL)
                self._look()

    def alert(title: str, message: str) -> None:
        box = NSAlert.alloc().init()
        box.setMessageText_(title)
        box.setInformativeText_(message)
        box.addButtonWithTitle_("OK")
        box.runModal()

    delegate = Delegate.alloc().init()
    pending: dict = {}

    add("Open dashboard", "openDashboard:", hidden=False)
    add("Rescan now", "rescan:", hidden=False)
    menu.addItem_(NSMenuItem.separatorItem())
    update_item = add("Check for updates…", "checkUpdates:")
    menu.addItem_(NSMenuItem.separatorItem())
    about = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        f"{APP_NAME} {version()}", None, "")
    about.setEnabled_(False)
    menu.addItem_(about)
    add("Quit", "quit:")
    status_item.setMenu_(menu)

    if not chrome_installed():
        box = NSAlert.alloc().init()
        box.setMessageText_("Google Chrome is required")
        box.setInformativeText_(
            "Tennis Stats reads the Winamax pages with your installed Google "
            "Chrome. Install it, then use “Rescan now”.")
        box.addButtonWithTitle_("Download Chrome")
        box.addButtonWithTitle_("Later")
        if box.runModal() == 1000:  # NSAlertFirstButtonReturn
            webbrowser.open("https://www.google.com/chrome/")

    webbrowser.open(dashboard.url)

    threading.Thread(target=delegate._look, daemon=True).start()
    threading.Thread(target=delegate.pollUpdates_, args=(None,), daemon=True).start()

    log.info("menu bar ready")
    nsapp.run()
    dashboard.stop()
    time.sleep(0.5)
    return 0


def run_headless(dashboard: Dashboard) -> int:
    print(f"{APP_NAME} {version()} — {dashboard.url}  (Ctrl-C to quit)")
    webbrowser.open(dashboard.url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        dashboard.stop()
    return 0


def cli_check_updates() -> int:
    """``--check-updates``: print the raw release feed result and exit."""
    try:
        data = fetch_latest(version())
    except Exception as exc:  # noqa: BLE001 - printed for diagnostics
        print(json.dumps({"current": version(), "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps({
        "current": version(),
        "tag": data.get("tag_name"),
        "assets": [a.get("name") for a in data.get("assets", [])],
        "update": latest_release(version()),
    }, indent=2))
    return 0


def main() -> int:
    setup_tls()
    if "--check-updates" in sys.argv[1:]:
        return cli_check_updates()

    os.environ.setdefault("TENNIS_STATS_DATA_DIR", str(data_dir()))
    setup_logging()
    log.info("%s %s starting (%s, frozen=%s)", APP_NAME, version(), platform.machine(), frozen())

    port = preferred_port()
    existing = running_instance(port)
    if existing:
        log.info("dashboard already running at %s", existing)
        webbrowser.open(existing)
        return 0

    dashboard = Dashboard(port)
    if not dashboard.start():
        try:
            from AppKit import NSAlert

            box = NSAlert.alloc().init()
            box.setMessageText_("Tennis Stats could not start")
            box.setInformativeText_(f"See {data_dir() / 'logs' / 'launcher.log'}")
            box.runModal()
        except Exception:
            pass
        return 1

    try:
        import AppKit  # noqa: F401

        return run_menubar(dashboard)
    except ImportError:
        return run_headless(dashboard)


if __name__ == "__main__":
    sys.exit(main())
