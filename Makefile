# Tennis Stats — local (offline) build + on-demand deployment.
#
#   make help          list every target
#   make build         build the app locally, no network, nothing published
#   make run           run the dashboard from THESE local files (dev server)
#   make app-local     run the freshly built dist/ app as a temporary instance
#   make open-installed  open the published app from /Applications
#   make release VERSION=x.y.z   bump + commit + tag (local only, no push)
#   make deploy        push HEAD + its tag → GitHub builds & publishes
#
PYTHON     ?= .venv/bin/python
BUILD_PY   ?= .venv-build/bin/python
DEV_PORT   ?= 8001
LOCAL_PORT ?= 8010
APP_LOCAL  := dist/Tennis Stats.app
APP_INST   := /Applications/Tennis Stats.app

# `make run` does not auto-scan by default (safe next to the installed app);
# `AUTOSCAN=1 make run` restores the normal dev behaviour.
RUN_ENV = TENNIS_STATS_PORT=$(DEV_PORT)
ifndef AUTOSCAN
RUN_ENV += TENNIS_STATS_NO_AUTOSCAN=1
endif

.DEFAULT_GOAL := help
.PHONY: help venv build-venv test build run app-local open-installed \
        release deploy ci-build check-build-venv clean

help:
	@echo "Local (offline):"
	@echo "  make build          app + DMG + ZIP in dist/ (offline, nothing published)"
	@echo "  make run            dev server from local files on :$(DEV_PORT) (AUTOSCAN=1 to scan)"
	@echo "  make app-local      temporary instance of dist/ app on :$(LOCAL_PORT)"
	@echo "  make open-installed published app from $(APP_INST)"
	@echo "  make test           pytest"
	@echo "One-time setup (needs network):"
	@echo "  make venv           dev venv -> .venv"
	@echo "  make build-venv     build venv -> .venv-build"
	@echo "Deploy (the only steps that touch GitHub):"
	@echo "  make release VERSION=x.y.z   bump VERSION + commit + tag, local only"
	@echo "  make deploy                  push HEAD + its tag -> CI publishes"
	@echo "  make ci-build                CI build without publishing (gh CLI)"

# ---------------------------------------------------------------- setup ----
venv:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements.txt

build-venv:
	python3 -m venv .venv-build
	.venv-build/bin/pip install --upgrade pip
	.venv-build/bin/pip install -r requirements-desktop.txt "playwright==1.64.0"

# ---------------------------------------------------------- local build ----
check-build-venv:
	@test -x "$(BUILD_PY)" || { \
	  echo "missing $(BUILD_PY) — run 'make build-venv' once (needs network)"; exit 1; }

test:
	$(PYTHON) -m pytest tests/ -q

# Fully offline: venvs cached, PyInstaller/codesign/hdiutil are local.
# Produces dist/Tennis Stats.app, .dmg, .zip, SHA256SUMS — nothing is uploaded.
build: check-build-venv test
	PYTHON="$(BUILD_PY)" bash desktop/build_dmg.sh

# ------------------------------------------------- running the two versions --
run:
	$(RUN_ENV) $(PYTHON) -m uvicorn app.main:app --host 127.0.0.1 --port $(DEV_PORT)

app-local:
	TENNIS_STATS_PORT=$(LOCAL_PORT) bash desktop/run_local.sh

open-installed:
	open "$(APP_INST)"

# -------------------------------------------------------------- deploy ------
# Step 1 only: bumps VERSION, commits, tags. Nothing leaves the laptop.
release:
	@test -n "$(VERSION)" || { echo "usage: make release VERSION=x.y.z"; exit 1; }
	@test -z "$$(git status --porcelain)" || { \
	  echo "working tree not clean — commit or stash your changes first, then re-run"; \
	  git status --short; exit 1; }
	@printf '%s\n' "$(VERSION)" > VERSION
	git commit -am "Tennis Stats $(VERSION)"
	git tag -a "v$(VERSION)" -m "Tennis Stats $(VERSION)"
	@echo "==> tagged v$(VERSION) locally — inspect with 'git show v$(VERSION)',"
	@echo "    then 'make deploy' publishes it."

# Step 2 only: the deliberate act that triggers CI build + GitHub release.
deploy:
	git push origin HEAD
	@TAG=$$(git describe --tags --exact-match HEAD 2>/dev/null || true); \
	if [ -n "$$TAG" ]; then \
	  git push origin "$$TAG"; \
	  echo "==> pushed $$TAG — CI now builds and publishes the release"; \
	else \
	  echo "HEAD is not tagged — run 'make release VERSION=x.y.z' first"; \
	  exit 1; \
	fi

# CI build WITHOUT publishing (artifacts only): Actions → release → Run workflow,
# publish box unticked; or, with the gh CLI: make ci-build
ci-build:
	gh workflow run release.yml || \
	  echo "gh CLI not available — use Actions -> release -> Run workflow (publish unticked)"

clean:
	rm -rf build "dist/Tennis Stats.app" dist/dmgroot
