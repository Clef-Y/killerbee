#!/usr/bin/env bash
# kb_jtag_recover.sh - JTAG-level "nudge" recovery for the CC1352P7 /
# CC1354P10 boards, for when a command gets rejected (STATUS_ERROR) or the
# UART stops responding entirely after heavy RF use - almost always because
# a previous script run didn't exit cleanly and left the RF core holding a
# command it never got told to release. This is a documented hardware/SDK
# characteristic, not a bug in your own scripts - see each firmware's
# README, "RF core can get stuck after heavy use". No physical unplug
# needed for this class of fault.
#
# What it does: a side-effect-free 4-byte memory read through the same
# debug probe used to flash the board. The read's own output is never
# used - the only thing this needs is DSLite's connect sequence, which
# runs the target's GEL board-reset script as a side effect. Any DSLite
# operation that connects works; a bare memory read is just the minimal
# one. The output file is written to a private temp dir and removed on
# exit, instead of littering /tmp with a fixed, shared filename.
#
# Usage:
#   tools/kb_jtag_recover.sh cc1352p7
#   tools/kb_jtag_recover.sh cc1354p10
#   DSLITE=/path/to/dslite.sh tools/kb_jtag_recover.sh cc1354p10
#
# See USAGE_GUIDE.md's "Firmware recovery" section for the full writeup,
# and that same section for what to do if the debug probe itself (not the
# target chip) has stopped responding - a different failure mode this
# script cannot fix.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DSLITE="${DSLITE:-/opt/ti/uniflash_sl/dslite.sh}"
BOARD="${1:-}"

usage() {
    echo "Usage: $0 <cc1352p7|cc1354p10>" >&2
    echo "  (set DSLITE=/path/to/dslite.sh to override the default" >&2
    echo "   $DSLITE if your install lives elsewhere, e.g. macOS's" >&2
    echo "   /Applications/ti/uniflash_<version>/dslite.sh)" >&2
    exit 1
}

case "$BOARD" in
    cc1352p7)
        CCXML="$SCRIPT_DIR/../firmware/src/kb-cc1352p7/CC1352P7_XDS110.ccxml"
        ;;
    cc1354p10)
        CCXML="$SCRIPT_DIR/../firmware/src/kb-cc1354p10/CC1354P10_XDS110.ccxml"
        ;;
    *)
        usage
        ;;
esac

if [ ! -x "$DSLITE" ]; then
    echo "error: dslite.sh not found/executable at $DSLITE" >&2
    echo "  set DSLITE=/path/to/dslite.sh to point at your install" >&2
    exit 1
fi

if [ ! -f "$CCXML" ]; then
    echo "error: target config not found: $CCXML" >&2
    exit 1
fi

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

echo "Nudging $BOARD via JTAG ($DSLITE, target config $(basename "$CCXML"))..."
"$DSLITE" --mode memory -c "$CCXML" -r 0x0,4 -o "$TMPDIR/discard.bin" -e

echo "Done - the board's GEL board-reset script ran as a side effect of the"
echo "connect above. If the UART still doesn't respond, see USAGE_GUIDE.md's"
echo "Firmware recovery section for the debug-probe-itself-wedged case"
echo "(a real unplug/replug of the probe's USB cable, not this script)."
