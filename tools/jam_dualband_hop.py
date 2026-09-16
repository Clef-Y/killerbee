#!/usr/bin/env python3
"""
jam_dualband_hop.py - Simultaneous on-chip hop jamming across BOTH bands at
once, using two physical boards together: 2.4GHz (page 0, channels 11-26)
on one board via KBCapabilities.PHYJAM_HOP's CMD_JAM_HOP_ON, and sub-1GHz
(page 31 and/or page 28, cross-page capable) on a second board via the same
capability's CMD_JAM_HOP_SUBG_ON. Wraps tools/jam24_hop.py and
tools/subg_jam_hop.py's own channel-parsing/validation logic unchanged
(imported, not duplicated) - this script's only new job is running both at
once and stopping both cleanly together.

Why two boards, not one: only the CC1352P7 firmware implements an on-chip
2.4GHz hop mode (see jam24_hop.py) - the CC1354P10 has no 2.4GHz/page-0
support at all. Sub-1GHz on-chip hop (subg_jam_hop.py) works on either
board. So the only way to hop-jam both bands *at once* is two boards: the
CC1352P7 doing 2.4GHz, and a second board (the CC1354P10, or a second
CC1352P7) doing sub-1GHz - a single board has one radio and can't run two
bands concurrently (rfSwitchBand() is a real, if usually cheap, band
switch, not a second parallel radio).

Each board's hop loop is fully on-chip and independent once started - see
jam24_hop.py/subg_jam_hop.py's own docstrings for the per-hop host-round-
trip cost this removes, and for the measured minimum reliable --dwell
(3ms on both bands; 1ms is the wire protocol's own floor but marginal -
see those scripts' docstrings and firmware/src/kb-cc1352p7/README.md +
firmware/src/kb-cc1354p10/README.md's "On-chip channel-hop jamming"
sections for the full measurement). This script's own job is just
sequencing: start both hops back to back (each is a single command over
its own board's separate USB/serial link, so the two starts are at most a
few ms apart), wait (Ctrl+C or --duration), then stop both. There is no
host-side synchronization between the two hops once running - each board
hops on its own independent on-chip clock, same as running jam24_hop.py
and subg_jam_hop.py in two separate terminals, just coordinated here so
one Ctrl+C/--duration cleanly stops both instead of two processes to
manage by hand.

Usage:
    python3 tools/jam_dualband_hop.py \\
        --iface24 /dev/cu.usbmodemL45003IW1 \\
        --iface-subg /dev/cu.usbmodemLS4501DC1 \\
        -c 11,15,20,25 --page-channels 31:9,40,80,120 --dwell 0.003

    # Different dwell per band, bounded run, both sub-1GHz pages at once:
    python3 tools/jam_dualband_hop.py \\
        --iface24 /dev/cu.usbmodemL45003IW1 --iface-subg /dev/cu.usbmodemLS4501DC1 \\
        -c 11,25 --dwell24 0.02 \\
        --page-channels 31:9,14,15,19,20,24,106 --page-channels 28:10,12,20,41,51,57 \\
        --dwell-subg 0.003 --duration 30

Ctrl+C (or --duration) stops both cleanly - JAMMER_OFF on each board, then
closes both devices. If one board's stop fails, the other is still
attempted rather than left running.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from killerbee import KillerBee, KBCapabilities  # type: ignore

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from jam24_hop import parse_channels  # reuse, don't duplicate
from subg_jam import parse_page_channels  # reuse, don't duplicate
from subg_scan import PAGE_INFO  # reuse, don't duplicate


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--iface24", required=True,
                     help="Serial device for the 2.4GHz board (must be a CC1352P7 - "
                          "the only firmware with an on-chip 2.4GHz hop mode)")
    ap.add_argument("--devtype24", default="cc1352p7",
                     help="Hardware type for the 2.4GHz board (default: cc1352p7)")
    ap.add_argument("--iface-subg", required=True,
                     help="Serial device for the sub-1GHz board - a second, "
                          "physically different board from --iface24")
    ap.add_argument("--devtype-subg", default="cc1354p10",
                     help="Hardware type for the sub-1GHz board (default: cc1354p10; "
                          "cc1352p7 also works if it's a second CC1352P7)")
    ap.add_argument("-c", "--channels", required=True,
                     help="2.4GHz channel spec, e.g. '11,12,13,14,15,20,22,26' or "
                          "'11-26'")
    ap.add_argument("--page-channels", action="append", required=True,
                     metavar="PAGE:CHANNELS",
                     help="Sub-1GHz 'PAGE:CHANNELS' (e.g. '31:9,14,15,19,20,24,106'), "
                          "repeatable - one per page (28 and/or 31)")
    ap.add_argument("--dwell", type=float, default=None,
                     help="Seconds per channel for BOTH bands (overridden per-band "
                          "by --dwell24/--dwell-subg if given). No default - pass "
                          "this, or both of the per-band flags.")
    ap.add_argument("--dwell24", type=float, default=None,
                     help="Seconds per channel for the 2.4GHz hop (default: --dwell)")
    ap.add_argument("--dwell-subg", type=float, default=None,
                     help="Seconds per channel for the sub-1GHz hop (default: --dwell)")
    ap.add_argument("--duration", type=float, default=0.0,
                     help="Stop both after this many seconds total (default: 0 = "
                          "unbounded, use Ctrl+C instead)")
    args = ap.parse_args()

    dwell24 = args.dwell24 if args.dwell24 is not None else args.dwell
    dwell_subg = args.dwell_subg if args.dwell_subg is not None else args.dwell
    if dwell24 is None or dwell_subg is None:
        print("error: give --dwell (applies to both bands) or both --dwell24 "
              "and --dwell-subg", file=sys.stderr)
        sys.exit(1)

    if args.iface24 == args.iface_subg:
        print("error: --iface24 and --iface-subg must be two different serial "
              "devices (two physical boards) - a single board has one radio "
              "and can't hop both bands at once", file=sys.stderr)
        sys.exit(1)

    channels24 = parse_channels(args.channels)
    if not channels24:
        print("error: no 2.4GHz channels given", file=sys.stderr)
        sys.exit(1)
    for ch in channels24:
        if ch < 11 or ch > 26:
            print("error: 2.4GHz channel %d out of range (valid: 11-26)" % ch,
                  file=sys.stderr)
            sys.exit(1)
    if len(channels24) > 16:
        print("error: too many 2.4GHz channels (%d given, max 16 - the real "
              "count of 2.4GHz channels 11-26)" % len(channels24), file=sys.stderr)
        sys.exit(1)

    hops_subg = parse_page_channels(args.page_channels)  # exits on its own on bad input

    dwell24_ms = round(dwell24 * 1000)
    dwell_subg_ms = round(dwell_subg * 1000)
    for label, ms in (("--dwell/--dwell24", dwell24_ms), ("--dwell/--dwell-subg", dwell_subg_ms)):
        if ms < 1 or ms > 0xFFFF:
            print("error: %s out of range (valid: 0.001-65.535 seconds)" % label,
                  file=sys.stderr)
            sys.exit(1)

    kb24 = KillerBee(device=args.iface24, hardware=args.devtype24)
    kb_subg = KillerBee(device=args.iface_subg, hardware=args.devtype_subg)

    ok = True
    if not kb24.check_capability(KBCapabilities.FREQ_2400):
        print("error: %s does not report 2.4GHz (FREQ_2400) support" % args.devtype24,
              file=sys.stderr)
        ok = False
    if not kb24.check_capability(KBCapabilities.PHYJAM_HOP):
        print("error: %s (--iface24) does not report PHYJAM_HOP support" % args.devtype24,
              file=sys.stderr)
        ok = False
    for page in sorted(set(p for p, _ in hops_subg)):
        page_info = PAGE_INFO[page]
        if not kb_subg.check_capability(page_info["capability"]):
            print("error: %s does not report %s support (page %d)"
                  % (args.devtype_subg, page_info["band_name"], page), file=sys.stderr)
            ok = False
    if not kb_subg.check_capability(KBCapabilities.PHYJAM_HOP):
        print("error: %s (--iface-subg) does not report PHYJAM_HOP support" % args.devtype_subg,
              file=sys.stderr)
        ok = False

    if not ok:
        kb24.close()
        kb_subg.close()
        sys.exit(1)

    print("2.4GHz hop on %s (%s): %d channel(s) [%s], %dms dwell -> ~%dms/cycle"
          % (args.iface24, args.devtype24, len(channels24), args.channels,
             dwell24_ms, dwell24_ms * len(channels24)))
    print("Sub-1GHz hop on %s (%s): %d hop(s) across page(s) %s, %dms dwell -> ~%dms/cycle"
          % (args.iface_subg, args.devtype_subg, len(hops_subg),
             ", ".join(str(p) for p in sorted(set(p for p, _ in hops_subg))),
             dwell_subg_ms, dwell_subg_ms * len(hops_subg)))
    if args.duration:
        print("Stopping both after %.1fs total" % args.duration)
    else:
        print("Running until Ctrl+C")
    print()

    run_start = time.time()
    jamming24 = False
    jamming_subg = False
    try:
        kb24.jam_hop_on(channels24, dwell24_ms)
        jamming24 = True
        kb_subg.jam_hop_subg_on(hops_subg, dwell_subg_ms)
        jamming_subg = True

        if args.duration:
            time.sleep(args.duration)
        else:
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        print("Stopping jammers...")
        if jamming24:
            try:
                kb24.jammer_off()
            except Exception as e:
                print("warning: 2.4GHz jammer_off() failed: %s" % e, file=sys.stderr)
        if jamming_subg:
            try:
                kb_subg.jammer_off()
            except Exception as e:
                print("warning: sub-1GHz jammer_off() failed: %s" % e, file=sys.stderr)
        try:
            kb24.close()
        except Exception:
            pass
        try:
            kb_subg.close()
        except Exception:
            pass

    elapsed = time.time() - run_start
    print("Done. Ran for %.1fs." % elapsed)


if __name__ == "__main__":
    main()
