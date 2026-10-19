"""Self-updater: release decision, menu wiring, download/verify, bundle swap.

Everything here is offline — a local HTTP server plays the GitHub release feed
and the GUI is replaced by a fake menu item. The regression that motivated the
suite is :func:`test_update_entry_click_installs`: in v1.0.1 the menu entry's
*title* changed to “Update to … available” but its *action* stayed
``checkUpdates:``, so the click re-ran the check and nothing was installed.
"""
from __future__ import annotations

import functools
import http.server
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import threading
import zipfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

# desktop/launcher.py imports its sibling desktop/updater.py by module name.
sys.path.insert(0, str(ROOT / "desktop"))

import updater  # noqa: E402


def _load_launcher():
    spec = importlib.util.spec_from_file_location("launcher", ROOT / "desktop" / "launcher.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


launcher = _load_launcher()
ARCH = updater.platform.machine()


# --------------------------------------------------------------- helpers ----
class FakeMenuItem:
    """Duck-typed NSMenuItem: the three setters ``apply_update_state`` uses."""

    def __init__(self) -> None:
        self.title: str = ""
        self.action: str = ""
        self.enabled: bool = False

    def setTitle_(self, title): self.title = title
    def setAction_(self, action): self.action = action
    def setEnabled_(self, enabled): self.enabled = enabled


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # keep pytest output clean
        pass


@pytest.fixture
def http_root(tmp_path):
    """Local HTTP server rooted at ``tmp_path``; yields the base URL."""
    handler = functools.partial(_QuietHandler, directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def make_app(root: pathlib.Path, version: str, name: str = "Fake") -> pathlib.Path:
    """A minimal ``.app`` laid out like the PyInstaller bundle."""
    app = root / f"{name}.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Frameworks").mkdir()
    binary = app / "Contents" / "MacOS" / name
    binary.write_text("#!/bin/sh\necho fake app\n", encoding="utf-8")
    binary.chmod(0o755)
    (app / "Contents" / "Info.plist").write_text(
        "<plist><dict><key>CFBundleIdentifier</key><string>com.anthonyrm.tennisstats</string>"
        "</dict></plist>", encoding="utf-8")
    (app / "Contents" / "Frameworks" / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    return app


def zip_app(app: pathlib.Path, dest: pathlib.Path, prefix: str = "") -> pathlib.Path:
    """Zip ``app`` so that ditto unpacks it at ``prefix/<name>.app``."""
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in sorted(app.rglob("*")):
            if item.is_file():
                zf.write(item, f"{prefix}{app.name}/{item.relative_to(app)}")
    return dest


def feed(tag: str, assets: dict[str, str]) -> dict:
    return {
        "tag_name": tag,
        "html_url": "https://example.invalid/release",
        "assets": [
            {"name": name, "browser_download_url": url} for name, url in assets.items()
        ],
    }


def write_feed(root: pathlib.Path, base: str, tag: str, assets: dict[str, str]) -> str:
    """Drop a release feed into the served root; return its URL."""
    (root / "feed.json").write_text(json.dumps(feed(tag, assets)), encoding="utf-8")
    return f"{base}/feed.json"


# ------------------------------------------------- menu entry wiring --------
def test_update_entry_click_installs():
    """The regression: an update entry must point at the *install* selector."""
    idle = updater.update_menu_state("idle")
    assert idle["action"] == updater.CHECK_ACTION == "checkUpdates:"
    assert idle["enabled"] and idle["title"] == "Check for updates…"

    update = updater.update_menu_state("update", version="1.0.2")
    assert update["action"] == updater.INSTALL_ACTION == "applyUpdate:"
    assert "1.0.2" in update["title"]
    assert update["enabled"]


def test_every_status_is_reachable_and_actioned():
    states = {
        status: updater.update_menu_state(status, version="1.0.2", current="1.0.1")
        for status in ("idle", "checking", "update", "installing", "uptodate",
                       "error", "disabled")
    }
    assert all(s["title"] and s["action"].endswith(":") for s in states.values())
    assert states["checking"] == {**states["checking"], "enabled": False}
    assert states["installing"]["enabled"] is False
    assert states["installing"]["action"] == updater.INSTALL_ACTION
    assert states["disabled"]["enabled"] is False
    assert "failed" in states["error"]["title"]
    assert "latest" in states["uptodate"]["title"]


def test_apply_update_state_writes_title_action_and_enabled():
    item = FakeMenuItem()
    updater.apply_update_state(item, updater.update_menu_state("update", version="2.0.0"))
    assert item.action == "applyUpdate:"       # without this the click does nothing
    assert "2.0.0" in item.title
    assert item.enabled is True


def test_apply_update_state_on_a_real_nsmenuitem():
    """Same wiring against AppKit itself (str -> SEL), where PyObjC exists."""
    try:
        from AppKit import NSMenuItem
    except ImportError:  # pragma: no cover - the bare dev venv has no PyObjC
        pytest.skip("PyObjC not installed")
    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Check for updates…", updater.CHECK_ACTION, "")
    updater.apply_update_state(item, updater.update_menu_state("update", version="1.0.2"))
    assert item.title() == "Update to 1.0.2 available — click to install"
    assert "applyUpdate" in str(item.action()), "the selector must be swapped, not only the title"
    assert item.isEnabled()


# ------------------------------------------------ release-feed decisions ----
def test_check_release_reports_update(monkeypatch):
    name = updater.asset_name("1.0.2")
    monkeypatch.setattr(updater, "fetch_latest", lambda current: feed(
        "v1.0.2", {name: f"https://example.invalid/{name}",
                   updater.sums_name("1.0.2"): "https://example.invalid/sums"}))
    result = updater.check_release("1.0.1")
    assert result["status"] == "update"
    assert result["info"]["version"] == "1.0.2"
    assert result["info"]["url"].endswith(name)
    assert result["info"]["sums_url"].endswith("sums")


def test_check_release_up_to_date(monkeypatch):
    monkeypatch.setattr(updater, "fetch_latest", lambda current: feed("v1.0.1", {}))
    assert updater.check_release("1.0.1") == {"status": "uptodate", "latest": "1.0.1"}
    assert updater.check_release("1.0.2") == {"status": "uptodate", "latest": "1.0.1"}


def test_check_release_never_offers_the_same_or_older_build(monkeypatch):
    monkeypatch.setattr(updater, "fetch_latest", lambda current: feed("v1.0.0", {}))
    assert updater.check_release("1.0.1")["status"] == "uptodate"


def test_check_release_missing_asset_for_this_arch(monkeypatch):
    monkeypatch.setattr(updater, "fetch_latest", lambda current: feed(
        "v1.0.2", {"TennisStats-1.0.2-other.zip": "https://example.invalid/x.zip"}))
    result = updater.check_release("1.0.1")
    assert result["status"] == "error"
    assert updater.asset_name("1.0.2") in result["error"]


def test_check_release_feed_unreachable(monkeypatch):
    def boom(current):
        raise OSError("network unreachable")
    monkeypatch.setattr(updater, "fetch_latest", boom)
    result = updater.check_release("1.0.1")
    assert result["status"] == "error"
    assert "network unreachable" in result["error"]


def test_check_release_disabled_flag(monkeypatch):
    monkeypatch.setenv("TENNIS_STATS_NO_UPDATE", "1")
    assert updater.check_release("1.0.1")["status"] == "disabled"


def test_release_api_url_override(monkeypatch, http_root, tmp_path):
    """``TENNIS_STATS_RELEASE_API`` points the real fetcher at a local feed."""
    name = updater.asset_name("1.0.2")
    url = write_feed(tmp_path, http_root, "v1.0.2", {name: "https://example.invalid/x.zip"})
    monkeypatch.setenv("TENNIS_STATS_RELEASE_API", url)
    assert updater.release_api_url() == url
    result = updater.check_release("1.0.1")
    assert result["status"] == "update"
    assert result["info"]["version"] == "1.0.2"


def test_cli_check_updates_prints_the_decision(monkeypatch, capsys, http_root, tmp_path):
    name = updater.asset_name("9.9.9")                       # newer than any VERSION file
    monkeypatch.setenv("TENNIS_STATS_RELEASE_API",
                       write_feed(tmp_path, http_root, "v9.9.9",
                                  {name: "https://example.invalid/x.zip"}))
    assert launcher.cli_check_updates() == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "update"
    assert printed["info"]["version"] == "9.9.9"
    assert printed["current"] == launcher.version()


# -------------------------------------------------- download and verify -----
def test_stage_update_unpacks_and_verifies(http_root, tmp_path):
    app = make_app(tmp_path / "src", "1.0.2")
    name = updater.asset_name("1.0.2")
    zip_app(app, tmp_path / name)
    work = tmp_path / "work"
    work.mkdir()

    info = {"version": "1.0.2", "url": f"{http_root}/{name}"}
    staged = updater.stage_update(info, work)
    assert staged.name == "Fake.app"
    assert updater.bundle_version(staged) == "1.0.2"
    assert (staged / "Contents" / "MacOS" / "Fake").exists()


def test_stage_update_finds_app_in_a_top_level_folder(http_root, tmp_path):
    app = make_app(tmp_path / "src", "1.0.2")
    name = updater.asset_name("1.0.2")
    zip_app(app, tmp_path / name, prefix="wrapper/")
    work = tmp_path / "work"
    work.mkdir()
    staged = updater.stage_update({"version": "1.0.2", "url": f"{http_root}/{name}"}, work)
    assert staged.name == "Fake.app"


def test_stage_update_rejects_a_different_version(http_root, tmp_path):
    app = make_app(tmp_path / "src", "9.9.9")
    name = updater.asset_name("1.0.2")
    zip_app(app, tmp_path / name)
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(updater.UpdateError, match="9\\.9\\.9"):
        updater.stage_update({"version": "1.0.2", "url": f"{http_root}/{name}"}, work)


def test_stage_update_rejects_a_corrupt_download(http_root, tmp_path):
    app = make_app(tmp_path / "src", "1.0.2")
    name = updater.asset_name("1.0.2")
    zip_app(app, tmp_path / name)
    sums = tmp_path / updater.sums_name("1.0.2")
    sums.write_text(f"{'0' * 64}  {name}\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    info = {"version": "1.0.2", "url": f"{http_root}/{name}",
            "sums_url": f"{http_root}/{sums.name}"}
    with pytest.raises(updater.UpdateError, match="corrupt"):
        updater.stage_update(info, work)


def test_stage_update_accepts_the_published_checksum(http_root, tmp_path):
    app = make_app(tmp_path / "src", "1.0.2")
    name = updater.asset_name("1.0.2")
    zip_app(app, tmp_path / name)
    sums = tmp_path / updater.sums_name("1.0.2")
    sums.write_text(f"{updater.sha256_of(tmp_path / name)}  {name}\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    info = {"version": "1.0.2", "url": f"{http_root}/{name}",
            "sums_url": f"{http_root}/{sums.name}"}
    assert updater.stage_update(info, work).name == "Fake.app"


def test_stage_update_matches_checksum_entries_carrying_the_build_path(http_root, tmp_path):
    """Published SUMS files list ``dist/…`` paths — the entry must still match.

    A non-matching entry would silently *skip* verification instead of
    catching a corrupt archive, which is exactly what happened against the
    real v1.0.3 release.
    """
    app = make_app(tmp_path / "src", "1.0.2")
    name = updater.asset_name("1.0.2")
    zip_app(app, tmp_path / name)
    sums = tmp_path / updater.sums_name("1.0.2")
    work = tmp_path / "work"
    work.mkdir()
    info = {"version": "1.0.2", "url": f"{http_root}/{name}",
            "sums_url": f"{http_root}/{sums.name}"}

    sums.write_text(f"{'0' * 64}  dist/{name}\n", encoding="utf-8")   # wrong hash
    with pytest.raises(updater.UpdateError, match="corrupt"):
        updater.stage_update(info, work)

    shutil.rmtree(work / "staging", ignore_errors=True)
    sums.write_text(f"{updater.sha256_of(tmp_path / name)}  dist/{name}\n", encoding="utf-8")
    assert updater.stage_update(info, work).name == "Fake.app"


def test_stage_update_survives_an_unreachable_checksum(http_root, tmp_path):
    """The checksum is best effort: a dead SUMS URL must not block updates."""
    app = make_app(tmp_path / "src", "1.0.2")
    name = updater.asset_name("1.0.2")
    zip_app(app, tmp_path / name)
    work = tmp_path / "work"
    work.mkdir()
    info = {"version": "1.0.2", "url": f"{http_root}/{name}",
            "sums_url": f"{http_root}/missing-sums.txt"}
    assert updater.stage_update(info, work).name == "Fake.app"


def test_stage_update_rejects_an_archive_without_an_app(http_root, tmp_path):
    with zipfile.ZipFile(tmp_path / updater.asset_name("1.0.2"), "w") as zf:
        zf.writestr("readme.txt", "nothing here")
    work = tmp_path / "work"
    work.mkdir()
    info = {"version": "1.0.2", "url": f"{http_root}/{updater.asset_name('1.0.2')}"}
    with pytest.raises(updater.UpdateError, match="does not contain an app"):
        updater.stage_update(info, work)


def test_stage_update_reports_an_unreachable_download(http_root, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    info = {"version": "1.0.2", "url": f"{http_root}/absent.zip"}
    with pytest.raises(updater.UpdateError, match="download failed"):
        updater.stage_update(info, work)


def test_verify_staged_bundle_requires_a_version_file(tmp_path):
    app = make_app(tmp_path, "1.0.2")
    (app / "Contents" / "Frameworks" / "VERSION").unlink()
    with pytest.raises(updater.UpdateError, match="VERSION"):
        updater.verify_staged_bundle(app, "1.0.2")


@pytest.mark.skipif(not shutil.which("codesign"), reason="codesign not available")
def test_verify_staged_bundle_checks_the_signature(tmp_path):
    app = make_app(tmp_path, "1.0.2")
    signed = subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)],
                            capture_output=True, text=True)
    if signed.returncode != 0:  # pragma: no cover - defensive
        pytest.skip(f"ad-hoc signing unsupported here: {signed.stderr.strip()}")
    assert (app / "Contents" / "_CodeSignature").exists()
    updater.verify_staged_bundle(app, "1.0.2")          # intact -> accepted

    binary = app / "Contents" / "MacOS" / "Fake"
    binary.write_text("#!/bin/sh\necho tampered\n", encoding="utf-8")
    with pytest.raises(updater.UpdateError, match="signature"):
        updater.verify_staged_bundle(app, "1.0.2")


def test_download_and_apply_refuses_a_dev_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "frozen", lambda: False)
    error = launcher.download_and_apply({"version": "1.0.2", "url": "https://example.invalid/x"})
    assert error and "development checkout" in error


def test_set_update_entry_writes_every_status():
    """The menu glue: each status lands title/action/enabled as designed."""
    item = FakeMenuItem()
    cancelled: list[int] = []
    cases = [("idle", {}), ("checking", {}), ("update", {"version": "1.0.3"}),
             ("installing", {"version": "1.0.3"}), ("uptodate", {"current": "1.0.2"}),
             ("error", {}), ("disabled", {})]
    for status, kwargs in cases:
        updater.set_update_entry(item, status,
                                  cancel_reset=lambda: cancelled.append(1), **kwargs)
        expected = updater.update_menu_state(status, **kwargs)
        assert (item.title, item.action, item.enabled) == (
            expected["title"], expected["action"], expected["enabled"]), status
    assert len(cancelled) == len(cases)
    assert item.action == updater.CHECK_ACTION     # last status shown was "disabled"


def test_set_update_entry_survives_a_broken_cancel_reset():
    item = FakeMenuItem()

    def boom():
        raise RuntimeError("selector not found")

    updater.set_update_entry(item, "uptodate", current="1.0.2", cancel_reset=boom)
    assert item.title == "✓ 1.0.2 is the latest version"   # the write still happened
    assert item.action == updater.CHECK_ACTION


def test_download_and_apply_reports_a_failed_handover(tmp_path, monkeypatch):
    """A swap that never starts must return an error, not leave the menu stuck."""
    monkeypatch.setattr(launcher, "frozen", lambda: True)
    dest = tmp_path / "Tennis Stats.app"
    dest.mkdir()
    monkeypatch.setattr(launcher, "bundle_path", lambda: dest)
    seen: dict = {}

    def fake_stage(info, work):
        seen["work"] = work
        (work / "staging").mkdir(exist_ok=True)
        return work / "staging" / "Tennis Stats.app"

    def boom(*args, **kwargs):
        raise OSError("cannot fork")

    monkeypatch.setattr(launcher, "stage_update", fake_stage)
    monkeypatch.setattr(launcher, "spawn_swap", boom)
    error = launcher.download_and_apply({"version": "1.0.2", "url": "https://example.invalid/x"})
    assert "could not start the swap" in error and "cannot fork" in error
    assert not seen["work"].exists(), "the staged download must be cleaned up"


def test_data_dir_honours_the_env_override(tmp_path, monkeypatch):
    """Logs (and the swap log) must follow TENNIS_STATS_DATA_DIR, frozen or not."""
    monkeypatch.setattr(launcher, "frozen", lambda: True)
    monkeypatch.setenv("TENNIS_STATS_DATA_DIR", str(tmp_path))
    assert launcher.data_dir() == tmp_path
    assert launcher.swap_log_path() == tmp_path / "logs" / "update.log"

    monkeypatch.delenv("TENNIS_STATS_DATA_DIR")
    assert launcher.data_dir() == pathlib.Path.home() / "Library" / "Application Support" / "TennisStats"

    monkeypatch.setattr(launcher, "frozen", lambda: False)
    assert launcher.data_dir() == ROOT / "data"


# ------------------------------------------------------- bundle swap --------
def _run_swap(tmp_path, dest, new, pid, *, tries=240, break_ditto=False):
    """Run the real SWAP_SCRIPT the way ``spawn_swap`` does, minus the GUI."""
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    (work / "update.zip").write_bytes(b"zip-bytes")
    (work / "staging").mkdir(exist_ok=True)
    log = tmp_path / "update.log"
    script = work / "swap.sh"
    script.write_text(updater.SWAP_SCRIPT, encoding="utf-8")

    shim = tmp_path / "shim"
    shim.mkdir(exist_ok=True)
    open_log = tmp_path / "opened.log"
    opener = shim / "open"
    opener.write_text('#!/bin/sh\necho "$@" >> "%s"\n' % open_log, encoding="utf-8")
    opener.chmod(0o755)
    if break_ditto:
        fake_ditto = shim / "ditto"
        fake_ditto.write_text("#!/bin/sh\necho 'ditto: boom' >&2\nexit 1\n", encoding="utf-8")
        fake_ditto.chmod(0o755)

    env = {**os.environ, "PATH": f"{shim}:{os.environ.get('PATH', '')}",
           "TS_SWAP_TRIES": str(tries)}
    proc = subprocess.run(
        ["/bin/sh", str(script), str(dest), str(new), str(pid), str(work), str(log)],
        capture_output=True, text=True, env=env, timeout=120,
    )
    return proc, log, open_log, work


def _fixture_bundles(tmp_path):
    old = tmp_path / "Applications" / "Tennis Stats.app"
    (old / "Contents").mkdir(parents=True)
    (old / "Contents" / "old.txt").write_text("old build", encoding="utf-8")
    new = tmp_path / "staged" / "Tennis Stats.app"
    (new / "Contents").mkdir(parents=True)
    (new / "Contents" / "new.txt").write_text("new build", encoding="utf-8")
    return old, new


def test_swap_replaces_the_bundle_and_relaunches(tmp_path):
    old, new = _fixture_bundles(tmp_path)
    proc, log, open_log, work = _run_swap(tmp_path, old, new, pid=999999)

    assert proc.returncode == 0, proc.stderr
    assert (old / "Contents" / "new.txt").read_text() == "new build"
    assert not (old / "Contents" / "old.txt").exists()
    assert not (old.parent / "Tennis Stats.app.old").exists()
    assert not (work / "update.zip").exists() and not (work / "staging").exists()
    assert not work.exists(), "the temp work dir must not be left behind"
    assert str(old) in open_log.read_text(), "the new bundle must be relaunched"
    assert "installed" in log.read_text()


def test_swap_leaves_the_app_alone_while_it_still_runs(tmp_path):
    old, new = _fixture_bundles(tmp_path)
    proc, log, open_log, work = _run_swap(tmp_path, old, new, pid=os.getpid(), tries=2)

    assert proc.returncode == 1
    assert (old / "Contents" / "old.txt").exists(), "a live app must not be replaced"
    assert not (old.parent / "Tennis Stats.app.old").exists()
    assert not open_log.exists(), "nothing may be relaunched"
    assert "still running" in log.read_text()


def test_swap_restores_the_previous_app_when_the_copy_fails(tmp_path):
    old, new = _fixture_bundles(tmp_path)
    proc, log, open_log, work = _run_swap(tmp_path, old, new, pid=999999, break_ditto=True)

    assert proc.returncode == 0
    assert (old / "Contents" / "old.txt").exists(), "the working app must come back"
    assert not (old / "Contents" / "new.txt").exists()
    assert not (old.parent / "Tennis Stats.app.old").exists()
    assert "restored" in log.read_text()
