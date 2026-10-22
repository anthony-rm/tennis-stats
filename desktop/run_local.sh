#!/usr/bin/env bash
# Launch the locally built "dist/Tennis Stats.app" as a TEMPORARY instance that
# can run next to the installed /Applications app:
#
#   * own port            TENNIS_STATS_PORT (default 8010; installed app: 8000)
#   * no self-update      TENNIS_STATS_NO_UPDATE=1 (temporary build never
#                         replaces itself from the GitHub release)
#   * repo data dir       TENNIS_STATS_DATA_DIR=<repo>/data
#                         (installed app keeps ~/Library/Application Support)
#   * (no auto-scan       the app never auto-scans on start — refresh is a
#                         manual action, so this instance is always safe)
#
# The bundle binary is executed directly instead of `open` so the environment
# variables actually reach it. Instance runs detached; log: build/local-run.log
set -euo pipefail
cd "$(dirname "$0")/.."

APP="dist/Tennis Stats.app"
[ -d "$APP" ] || { echo "no local build at $APP — run 'make build' first" >&2; exit 1; }

export TENNIS_STATS_PORT="${TENNIS_STATS_PORT:-8010}"
export TENNIS_STATS_NO_UPDATE=1
export TENNIS_STATS_DATA_DIR="${TENNIS_STATS_DATA_DIR:-$PWD/data}"
[ -n "${AUTOSCAN:-}" ] || export TENNIS_STATS_NO_AUTOSCAN=1

mkdir -p build
LOG="$PWD/build/local-run.log"
nohup "$APP/Contents/MacOS/Tennis Stats" >"$LOG" 2>&1 &
sleep 2
echo "==> local instance: http://127.0.0.1:$TENNIS_STATS_PORT  (log: $LOG)"
