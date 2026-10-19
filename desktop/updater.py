"""Self-update machinery: release feed, verification, bundle swap, menu model.

Everything here is free of Cocoa/uvicorn wiring so the whole update path can be
exercised offline (tests/test_launcher_update.py) without PyObjC. The launcher
owns the integration: the menu entry, the worker threads and, when frozen, the
download + swap handover (``download_and_apply``).

``check_release`` decides *what* to offer, ``update_menu_state`` / ``set_update_entry``
decide how the menu entry says it (title **and** action — a title-only change
made v1.0.1's “Update to … available” entry do nothing), ``stage_update`` +
``verify_*`` fetch and vet the archive, and the detached ``SWAP_SCRIPT`` replaces
the bundle once the app has quit.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import subprocess
import urllib.request
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

REPO_SLUG = "anthony-rm/tennis-stats"
API_LATEST = f"https://api.github.com/repos/{REPO_SLUG}/releases/latest"
RELEASE_PAGE = f"https://github.com/{REPO_SLUG}/releases/latest"

log = logging.getLogger("updater")


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


def sums_name(ver: str) -> str:
    return f"SHA256SUMS-{ver}-{platform.machine()}.txt"


def release_api_url() -> str:
    """Release feed URL — ``TENNIS_STATS_RELEASE_API`` overrides it (tests)."""
    return os.environ.get("TENNIS_STATS_RELEASE_API") or API_LATEST


def fetch_latest(current: str) -> dict:
    req = urllib.request.Request(
        release_api_url(),
        headers={"Accept": "application/vnd.github+json", "User-Agent": f"TennisStats/{current}"},
    )
    with urllib.request.urlopen(req, timeout=20) as res:
        return json.load(res)


def check_release(current: str) -> dict:
    """Decide what the update entry should offer for ``current``.

    Returns ``{"status": ...}`` where status is one of

    ``update``    a newer release with a usable archive — ``info`` then holds
                  ``version`` / ``url`` / ``page`` / ``sums_url``
    ``uptodate``  ``latest`` is not newer than ``current``
    ``disabled``  ``TENNIS_STATS_NO_UPDATE`` turns the updater off
    ``error``     the feed is unreadable or carries no archive for this
                  machine — ``error`` holds the reason, shown to the user
    """
    if os.environ.get("TENNIS_STATS_NO_UPDATE"):
        log.info("update check disabled (TENNIS_STATS_NO_UPDATE)")
        return {"status": "disabled"}
    try:
        data = fetch_latest(current)
    except Exception as exc:  # noqa: BLE001 - reported to the user
        log.warning("update check failed", exc_info=True)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    latest = str(data.get("tag_name") or "").lstrip("vV")
    if not latest:
        return {"status": "error", "error": "the release feed carries no version tag"}
    if parse_version(latest) <= parse_version(current):
        log.info("up to date (%s, latest %s)", current, latest)
        return {"status": "uptodate", "latest": latest}
    assets = {a.get("name"): a.get("browser_download_url") for a in data.get("assets", [])}
    url = assets.get(asset_name(latest)) or assets.get(f"TennisStats-{latest}.zip")
    if not url:
        reason = f"release {latest} has no {asset_name(latest)} archive"
        log.warning(reason)
        return {"status": "error", "error": reason}
    return {
        "status": "update",
        "info": {
            "version": latest,
            "url": url,
            "page": data.get("html_url") or RELEASE_PAGE,
            "sums_url": assets.get(sums_name(latest)),
        },
    }


class UpdateError(RuntimeError):
    """Why an update could not be installed — shown to the user as-is."""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def verify_checksum(archive: Path, sums_url: str | None, name: str) -> None:
    """Compare the download with the published ``SHA256SUMS`` file.

    A missing or unreachable checksum file is only logged (releases are not
    required to publish one) — a *mismatch* aborts the update.
    """
    if not sums_url:
        log.info("no published checksum for %s — skipping verification", name)
        return
    try:
        req = urllib.request.Request(sums_url, headers={"User-Agent": "TennisStats/updater"})
        with urllib.request.urlopen(req, timeout=30) as res:
            text = res.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - the checksum is best effort
        log.warning("checksum file unavailable (%s) — skipping verification", exc)
        return
    expected = None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        # Entries are either bare ("TennisStats-1.0.3-arm64.zip") or carry the
        # build path the shasum ran from ("dist/TennisStats-1.0.3-arm64.zip")
        # — match on the file name, or the layer silently never runs.
        if PurePosixPath(parts[1].lstrip("*")).name == name:
            expected = parts[0].lower()
            break
    if not expected:
        log.warning("%s not listed in the checksum file — skipping verification", name)
        return
    actual = sha256_of(archive)
    if actual != expected:
        raise UpdateError(
            f"the downloaded archive is corrupt ({actual[:12]}… instead of {expected[:12]}…)"
        )
    log.info("checksum ok for %s", name)


def bundle_version(app: Path) -> str | None:
    """Version stamped inside a (frozen) app bundle, if any."""
    for candidate in (
        app / "Contents" / "Frameworks" / "VERSION",   # PyInstaller ≥ 6 (onedir)
        app / "Contents" / "Resources" / "VERSION",
        app / "Contents" / "VERSION",
    ):
        try:
            text = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return text
    return None


def verify_staged_bundle(app: Path, expected: str) -> None:
    """Refuse to install anything that is not the release we asked for."""
    found = bundle_version(app)
    if not found:
        raise UpdateError("the downloaded app carries no VERSION file")
    if found.lstrip("vV") != expected.lstrip("vV"):
        raise UpdateError(f"the downloaded app is {found}, but {expected} was expected")
    if not (app / "Contents" / "_CodeSignature").exists():
        log.warning("%s is not code-signed — installing it anyway", app.name)
        return
    try:
        res = subprocess.run(
            ["codesign", "--verify", "--deep", "--strict", str(app)],
            capture_output=True, text=True,
        )
    except FileNotFoundError:  # pragma: no cover - macOS always ships codesign
        return
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or "").strip().splitlines()
        raise UpdateError("code signature check failed: " + (detail[-1] if detail else "invalid"))


def stage_update(info: dict, work: Path) -> Path:
    """Download the release archive into ``work``, verify it, unpack it.

    Returns the staged ``.app``; raises :class:`UpdateError` with a message
    fit for an alert box.
    """
    url, expected = info["url"], info["version"]
    name = PurePosixPath(urlparse(url).path).name or asset_name(expected)
    archive = work / "update.zip"
    log.info("downloading %s", url)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "TennisStats/updater"})
        with urllib.request.urlopen(req, timeout=300) as res, archive.open("wb") as out:
            while chunk := res.read(1 << 20):
                out.write(chunk)
    except Exception as exc:  # noqa: BLE001 - reported to the user
        raise UpdateError(f"download failed: {type(exc).__name__}: {exc}") from exc
    verify_checksum(archive, info.get("sums_url"), name)
    staging = work / "staging"
    staging.mkdir(exist_ok=True)
    try:
        subprocess.run(
            ["ditto", "-x", "-k", str(archive), str(staging)],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise UpdateError(f"could not unpack the archive: {(exc.stderr or '').strip()}") from exc
    apps = sorted(staging.glob("*.app")) or sorted(staging.glob("*/*.app"))
    if not apps:
        raise UpdateError("the downloaded archive does not contain an app")
    new_app = apps[0]
    subprocess.run(["chmod", "-R", "u+w", str(new_app)], check=False, capture_output=True)
    verify_staged_bundle(new_app, expected)
    return new_app


SWAP_SCRIPT = r"""#!/bin/sh
# Detached updater: wait for the running app to exit, replace its bundle with
# the freshly downloaded one and relaunch.
#   $1 dest (.app to replace)  $2 staged .app  $3 pid to wait for
#   $4 work dir to clean up    $5 log file
# TS_SWAP_TRIES overrides the 60 s wait (tests).
DEST="$1"; NEW="$2"; PID="$3"; WORK="$4"; LOG="$5"
TRIES="${TS_SWAP_TRIES:-240}"
say() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG" 2>/dev/null || true; }
cleanup() { rm -rf "$WORK/update.zip" "$WORK/staging"; rm -f "$0"; rmdir "$WORK" 2>/dev/null; true; }
say "waiting for pid $PID to exit"
i=0
while kill -0 "$PID" 2>/dev/null && [ "$i" -lt "$TRIES" ]; do sleep 0.25; i=$((i+1)); done
if kill -0 "$PID" 2>/dev/null; then
    say "still running after $i tries — keeping the installed app untouched"
    cleanup
    exit 1
fi
sleep 0.5
if [ ! -d "$NEW" ]; then
    say "staged app vanished: $NEW"
    cleanup
    exit 1
fi
MOVED=0
rm -rf "$DEST.old"
mv "$DEST" "$DEST.old" 2>/dev/null && MOVED=1
if ditto "$NEW" "$DEST"; then
    xattr -dr com.apple.quarantine "$DEST" 2>/dev/null
    rm -rf "$DEST.old"
    say "installed $(basename "$DEST"), relaunching"
    open "$DEST" >>"$LOG" 2>&1
else
    say "copy failed"
    if [ "$MOVED" = 1 ]; then
        rm -rf "$DEST"
        mv "$DEST.old" "$DEST"
        say "restored the previous app"
    fi
fi
cleanup
exit 0
"""


def spawn_swap(dest: Path, new_app: Path, work: Path, log_path: Path) -> None:
    """Start the detached script that replaces the bundle once we have quit."""
    script = work / "swap.sh"
    script.write_text(SWAP_SCRIPT, encoding="utf-8")
    script.chmod(0o755)
    log.info("swapping %s -> %s (log: %s)", new_app, dest, log_path)
    subprocess.Popen(
        ["/bin/sh", str(script), str(dest), str(new_app), str(os.getpid()),
         str(work), str(log_path)],
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
    )


CHECK_ACTION = "checkUpdates:"
INSTALL_ACTION = "applyUpdate:"


def update_menu_state(status: str, *, version: str = "", current: str = "") -> dict:
    """What the update menu entry should say, point at and allow.

    The entry does double duty: it is the *check* button while idle and the
    *install* button once a newer release is known — so its action has to
    switch together with its title. Changing only the title makes the click
    re-run the check and look like nothing happened (the 1.0.1 bug).

    ``status`` ∈ ``idle`` ``checking`` ``update`` ``installing`` ``uptodate``
    ``error`` ``disabled``; returns ``{"title", "action", "enabled"}``.
    """
    state = {"title": "Check for updates…", "action": CHECK_ACTION, "enabled": True}
    if status == "checking":
        state.update(title="Checking for updates…", enabled=False)
    elif status == "update":
        state.update(title=f"Update to {version} available — click to install",
                     action=INSTALL_ACTION)
    elif status == "installing":
        state.update(title=f"Downloading {version}…", action=INSTALL_ACTION, enabled=False)
    elif status == "uptodate":
        state.update(title=f"✓ {current} is the latest version")
    elif status == "error":
        state.update(title="Check for updates… (failed)")
    elif status == "disabled":
        state.update(title="Updates disabled", enabled=False)
    return state


def apply_update_state(item, state: dict) -> None:
    """Write ``state`` onto the ``NSMenuItem`` — main thread only."""
    item.setTitle_(state["title"])
    item.setAction_(state["action"])
    item.setEnabled_(state["enabled"])


def set_update_entry(item, status: str, *, version: str = "", current: str = "",
                     cancel_reset=None) -> None:
    """Main thread: put the update entry into ``status``.

    ``cancel_reset`` drops a pending transient-title revert before the new
    state is written, so an “up to date” line cannot overwrite a fresher one.
    """
    if cancel_reset is not None:
        try:
            cancel_reset()
        except Exception:  # noqa: BLE001 - bookkeeping must not block the UI
            log.exception("could not cancel the pending title reset")
    apply_update_state(item, update_menu_state(status, version=version, current=current))


