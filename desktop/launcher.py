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
    4. polls the GitHub release feed and, when a newer tag exists, turns the
       update entry into an **installer**: one click downloads the matching
       zip, verifies it (SHA256 + code signature), swaps the bundle in place
       and relaunches. The entry switches *action* together with its title —
       a title-only change would make the click just re-run the check.

``--check-updates`` prints the release-feed decision as JSON and exits;
``--update`` performs the whole check + install + relaunch from the terminal.
The release feed, the archive verification and the menu model live in
``desktop/updater.py`` (imported here).

Without PyObjC (e.g. a bare dev venv) it falls back to a plain foreground
process: dashboard open, Ctrl-C to quit.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import platform
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from updater import (
    CHECK_ACTION, UpdateError, check_release, set_update_entry, spawn_swap,
    stage_update,
)

APP_NAME = "Tennis Stats"
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
    """``TENNIS_STATS_DATA_DIR`` wins (set by run_local.sh / tests), otherwise
    the app-support folder when frozen and ``<repo>/data`` in development."""
    override = os.environ.get("TENNIS_STATS_DATA_DIR")
    if override:
        return Path(override)
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


def swap_log_path() -> Path:
    target = data_dir() / "logs"
    target.mkdir(parents=True, exist_ok=True)
    return target / "update.log"


def download_and_apply(info: dict) -> str | None:
    """Download, verify and stage the new build, then hand over to the swap.

    Returns None when the update is on its way — the caller must then quit so
    the script can replace the bundle — otherwise a message to show the user.
    """
    dest = bundle_path()
    if not frozen():
        return "this is a development checkout — update with git"
    if not os.access(dest.parent, os.W_OK):
        return f"cannot write to {dest.parent}"
    work = Path(tempfile.mkdtemp(prefix="tennisstats-update-"))
    try:
        new_app = stage_update(info, work)
    except UpdateError as exc:
        log.error("update aborted: %s", exc)
        shutil.rmtree(work, ignore_errors=True)
        return str(exc)
    except Exception as exc:  # noqa: BLE001 - surfaced to the user
        log.exception("update failed")
        shutil.rmtree(work, ignore_errors=True)
        return f"{type(exc).__name__}: {exc}"
    try:
        spawn_swap(dest, new_app, work, swap_log_path())
    except Exception as exc:  # noqa: BLE001 - the menu must not stay “Downloading…”
        log.exception("could not hand over to the swap script")
        shutil.rmtree(work, ignore_errors=True)
        return f"could not start the swap: {type(exc).__name__}: {exc}"
    return None


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

    def show(status: str, *, version: str = "", current: str = "") -> None:
        """Main thread only — put the update entry into ``status``."""
        set_update_entry(
            update_item, status, version=version, current=current,
            cancel_reset=lambda: delegate.cancelPreviousPerformRequestsWithTarget_selector_object_(
                delegate, "resetTitle:", None),
        )

    class Delegate(NSObject):
        # --- menu actions (selectors) --------------------------------
        def openDashboard_(self, sender):
            webbrowser.open(dashboard.url)

        def rescan_(self, sender):
            threading.Thread(target=dashboard.rescan, daemon=True).start()

        def checkUpdates_(self, sender):
            if pending.get("installing"):
                return
            show("checking")
            threading.Thread(target=self._look, args=(True,), daemon=True).start()

        def applyUpdate_(self, sender):
            info = pending.get("update")
            if not info or pending.get("installing"):
                return
            pending["installing"] = True
            show("installing", version=info["version"])
            threading.Thread(target=self._install, args=(info,), daemon=True).start()

        def quit_(self, sender):
            dashboard.stop()
            nsapp.terminate_(None)

        # --- called from worker threads (via performSelectorOnMainThread)
        def handleCheck_(self, payload):
            """Turn a finished release check into menu state (+ feedback)."""
            res = json.loads(payload)
            if res["status"] == "update":
                if pending.get("installing"):
                    return                      # never re-arm mid-install
                pending["update"] = res["info"]
                show("update", version=res["info"]["version"])
                return
            if not res.get("interactive"):
                return                          # background poll: stay quiet
            if res["status"] == "uptodate":
                pending.pop("update", None)
                show("uptodate", current=res["latest"])
                self.performSelector_withObject_afterDelay_("resetTitle:", None, 5.0)
            elif res["status"] == "disabled":
                show("disabled")
            else:
                show("error")
                alert("Could not check for updates", res.get("error") or "unknown error")

        def resetTitle_(self, _):
            """Revert the transient “up to date” line to the idle entry."""
            if pending.get("update") or pending.get("installing"):
                return
            show("idle")

        def updateFailed_(self, message):
            pending["installing"] = False
            if pending.get("update"):
                show("update", version=pending["update"]["version"])  # one click retries
            else:
                show("error")
            alert("Update failed", str(message))

        def quitAfterUpdate_(self, _):
            dashboard.stop()
            nsapp.terminate_(None)

        # --- workers -------------------------------------------------
        @objc.python_method
        def _look(self, interactive: bool = False) -> None:
            res = check_release(version())
            res["interactive"] = bool(interactive)
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                "handleCheck:", json.dumps(res), False)

        @objc.python_method
        def _install(self, info) -> None:
            error = download_and_apply(info)
            if error:
                self.performSelectorOnMainThread_withObject_waitUntilDone_(
                    "updateFailed:", error, False)
            else:
                self.performSelectorOnMainThread_withObject_waitUntilDone_(
                    "quitAfterUpdate:", None, False)

        def pollUpdates_(self, _):
            while True:
                time.sleep(UPDATE_INTERVAL)
                self._look(False)

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
    update_item = add("Check for updates…", CHECK_ACTION)
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

    threading.Thread(target=delegate._look, args=(False,), daemon=True).start()
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
    """``--check-updates``: print the release-feed decision as JSON and exit."""
    current = version()
    result = check_release(current)
    print(json.dumps({"current": current, **result}, indent=2))
    return 1 if result["status"] == "error" else 0


def cli_apply_update() -> int:
    """``--update``: check for a newer release and install it right now.

    The process exits once the download is staged; the detached swap script
    then replaces the bundle and relaunches the app.
    """
    current = version()
    result = check_release(current)
    print(json.dumps({"current": current, **result}, indent=2))
    if result["status"] != "update":
        return 1 if result["status"] == "error" else 0
    error = download_and_apply(result["info"])
    if error:
        print(f"update failed: {error}", file=sys.stderr)
        return 1
    print("update downloaded — quitting so the bundle can be replaced and relaunched")
    return 0


def main() -> int:
    setup_tls()
    if "--check-updates" in sys.argv[1:]:
        return cli_check_updates()

    os.environ.setdefault("TENNIS_STATS_DATA_DIR", str(data_dir()))
    setup_logging()
    if "--update" in sys.argv[1:]:
        return cli_apply_update()
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
