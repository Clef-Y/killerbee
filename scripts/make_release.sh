#!/usr/bin/env bash
# make_release.sh - Assemble a shareable zip so others can reproduce a
# KillerBee TI board (CC1354P10 / CC1352P7): prebuilt firmware images, the
# per-board flash target configs, the Python host tools + killerbee
# package, the macOS GUI, and the docs.
#
# Deliberately does NOT bundle TI's proprietary tools (UniFlash / DSLite /
# the SimpleLink SDK / compiler) - those are not ours to redistribute.
# Recipients download UniFlash from TI directly; see the TI SDK setup docs
# included in the bundle.
#
# Usage:
#   scripts/make_release.sh            # -> dist/killerbee-ti-boards-<date>.zip
#   scripts/make_release.sh /tmp/out   # choose a different output directory

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${1:-$REPO_ROOT/dist}"
STAMP="$(date +%Y%m%d)"
STAGE_NAME="killerbee-ti-boards-$STAMP"
STAGE="$(mktemp -d)/$STAGE_NAME"

mkdir -p "$STAGE" "$OUT_DIR"

echo "Staging release in $STAGE ..."

# --- Prebuilt firmware images + flash target configs -----------------------
for board in kb-cc1354p10 kb-cc1352p7; do
    src="$REPO_ROOT/firmware/src/$board"
    hex=$(ls "$src"/tirtos7/ticlang/*.hex 2>/dev/null | head -1 || true)
    if [ -z "$hex" ]; then
        echo "WARNING: no prebuilt .hex found for $board - build it first" >&2
    fi
    mkdir -p "$STAGE/firmware/$board"
    [ -n "$hex" ] && cp "$hex" "$STAGE/firmware/$board/"
    cp "$src"/*.ccxml "$STAGE/firmware/$board/" 2>/dev/null || true
    cp "$src"/README.md "$STAGE/firmware/$board/" 2>/dev/null || true
done

# --- Python host tools -----------------------------------------------------
mkdir -p "$STAGE/tools"
cp "$REPO_ROOT"/tools/*.py "$STAGE/tools/" 2>/dev/null || true
for t in zbid zbdump zbreplay zbstumbler zbscapy kb_jtag_recover.sh; do
    [ -e "$REPO_ROOT/tools/$t" ] && cp "$REPO_ROOT/tools/$t" "$STAGE/tools/"
done

# --- killerbee package (the actual library the tools import) ---------------
cp -R "$REPO_ROOT/killerbee" "$STAGE/killerbee"
# Drop compiled caches so the bundle is clean/small.
find "$STAGE/killerbee" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# --- macOS GUI -------------------------------------------------------------
cp -R "$REPO_ROOT/macapp" "$STAGE/macapp"
find "$STAGE/macapp" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# --- Docs ------------------------------------------------------------------
for d in BUILD_YOUR_OWN_BOARD.md USAGE_GUIDE.md MACOS_SETUP.md \
         TI_SDK_SETUP_DEBIAN.md TI_SDK_SETUP_MACOS.md \
         ARCHITECTURE.md README.md LICENSE; do
    [ -e "$REPO_ROOT/$d" ] && cp "$REPO_ROOT/$d" "$STAGE/"
done

# --- A short top-level note in the bundle ----------------------------------
cat > "$STAGE/START_HERE.txt" <<'EOF'
KillerBee TI boards - prebuilt bundle
=====================================

Start with BUILD_YOUR_OWN_BOARD.md.

Contents:
  firmware/   prebuilt .hex images + per-board flash configs (.ccxml)
  tools/      Python host tools (zbid, rssi_scan.py, zbdump, etc.)
  killerbee/  the Python library the tools import
  macapp/     macOS GUI for device discovery + RSSI scan (receive-only)
  *.md        setup, usage, and TI SDK install guides

NOT included (download from TI yourself - proprietary, not redistributable):
  - TI UniFlash  (provides dslite.sh, needed to flash the firmware)
  - TI SimpleLink SDK + compiler  (only needed to REBUILD firmware)
  See TI_SDK_SETUP_DEBIAN.md / TI_SDK_SETUP_MACOS.md.

The jamming tools in tools/ transmit and are illegal to operate in many
jurisdictions. Use them only where you are legally authorized to transmit.
See BUILD_YOUR_OWN_BOARD.md's final section.
EOF

# --- Zip it up -------------------------------------------------------------
ZIP_PATH="$OUT_DIR/$STAGE_NAME.zip"
rm -f "$ZIP_PATH"
( cd "$(dirname "$STAGE")" && zip -r -q "$ZIP_PATH" "$STAGE_NAME" )
rm -rf "$(dirname "$STAGE")"

echo "Wrote $ZIP_PATH"
du -h "$ZIP_PATH" | awk '{print "Size: " $1}'
echo "Done. Recipients: start with BUILD_YOUR_OWN_BOARD.md inside the zip."
