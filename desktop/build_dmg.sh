#!/usr/bin/env bash
# Build "Tennis Stats.app" + the DMG (first install) and the ZIP (auto-update).
#
#   bash desktop/build_dmg.sh
#
# Artefacts land in dist/:
#   Tennis Stats.app
#   TennisStats-<version>-<arch>.dmg   ← what a new user downloads
#   TennisStats-<version>-<arch>.zip   ← what the updater downloads
#   SHA256SUMS-<version>-<arch>.txt
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="$(tr -d '[:space:]' < VERSION)"
ARCH="$(uname -m)"
PY="${PYTHON:-python3}"

echo "==> building Tennis Stats $VERSION ($ARCH) with $($PY -V)"
rm -rf build "dist/Tennis Stats.app" dist/dmgroot
mkdir -p build dist

"$PY" desktop/make_icon.py || echo "==> icon skipped"

"$PY" -m PyInstaller --noconfirm --clean \
  --distpath dist --workpath build \
  desktop/TennisStats.spec

APP="dist/Tennis Stats.app"
[ -d "$APP" ] || { echo "build produced no $APP"; exit 1; }

# Playwright's node driver must stay executable after packaging.
find "$APP" -type f -name node -path '*playwright/driver/*' -exec chmod 755 {} \;

# Ad-hoc signature: enough for local running; the app is not notarized, so the
# first launch is a right-click → Open (documented in the README).
codesign --force --deep --sign - "$APP" 2>&1 | tail -3 || true
codesign --verify --deep --strict "$APP" && echo "==> ad-hoc signature ok"

ZIP="dist/TennisStats-${VERSION}-${ARCH}.zip"
rm -f "$ZIP"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"

DMG="dist/TennisStats-${VERSION}-${ARCH}.dmg"
rm -rf dist/dmgroot "$DMG"
mkdir -p dist/dmgroot
ditto "$APP" "dist/dmgroot/Tennis Stats.app"
ln -s /Applications "dist/dmgroot/Applications"
hdiutil create -quiet -volname "Tennis Stats" -srcfolder dist/dmgroot \
  -ov -format UDZO "$DMG"
rm -rf dist/dmgroot

# Checksums with bare file names, so `sha256sum -c SHA256SUMS-*.txt` works
# right after download (and the updater's verify_checksum matches them).
( cd dist && shasum -a 256 "$(basename "$DMG")" "$(basename "$ZIP")" ) \
  | tee "dist/SHA256SUMS-${VERSION}-${ARCH}.txt"

du -sh "$APP" "$DMG" "$ZIP"
echo "==> done"
