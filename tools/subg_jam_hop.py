#!/usr/bin/env python3
"""
subg_jam_hop.py - On-chip rotating constant-carrier jammer across a
*cross-page* sub-1GHz channel list (page 31 = 915 MHz US ISM and/or page
28 = 863-876 MHz EU/UK), using KillerBee's jam_hop_subg_on()
(KBCapabilities.PHYJAM_HOP). Works against both the CC1354P10 (default,
its own CMD_JAM_HOP_ON) and the CC1352P7 (-d cc1352p7, CMD_JAM_HOP_SUBG_ON
- a separate command there since its own CMD_JAM_HOP_ON is a distinct,
2.4GHz-only hop mode) - same channel plan on both, so the same command
line works against either board.

Same jam (constant-carrier, modulated PRBS-15 garbage - KBCapabilities.
PHYJAM) and same cross-page rotation idea as tools/subg_jam.py's
--page-channels, but the hop loop runs entirely on the CC1354P10 itself -
the host sends one command to start it and one to stop it, with no round
trip per channel hop. Each hop entry carries its own page (not a bare
channel), since page 31 and page 28 use different frequency formulas at
the same raw channel number (e.g. channel 20 is valid on both, at very
different real frequencies) - see
firmware/src/kb-cc1354p10/README.md's "On-chip channel-hop jamming"
section.

Sub-1GHz only - deliberately does not support 2.4GHz (page 0) on this
firmware (ported from the CC1352P7 firmware's 2.4GHz-only
tools/jam24_hop.py; this is the sub-1GHz counterpart).

Why this exists, not just tools/subg_jam.py: driving the exact same
rotation by having the host call SET_CHANNEL once per hop pays a real,
fixed per-hop cost - measured directly on this project's hardware at a
near-flat ~30ms round trip per SET_CHANNEL call, regardless of whether any
real RF retuning happens (a USB/debug-probe control-plane tax, not RF
physics - the RF core's own retune time is sub-millisecond). That makes
very short dwells (well under ~30ms) meaningless with tools/subg_jam.py -
the ~30ms floor dominates regardless of --dwell. Looping the hop on-chip
instead removes that tax: the real per-hop cost becomes
rfTuneToChannel()'s own rfPostAndPoll() polling grain (~1ms worst case)
plus whatever dwell was asked for.

Trade-off: because the host isn't in the loop, this tool can't print a
live per-hop channel/frequency log the way tools/subg_jam.py does - it
just starts the hop, waits (Ctrl+C or --duration), then stops it.

Usage:
    python3 tools/subg_jam_hop.py -i /dev/cu.usbmodemLS4501DC1 --dwell 0.02 \\
        --page-channels 31:9,14,15,19,20,24,106 \\
        --page-channels 28:10,12,20,41,51,57

Ctrl+C stops cleanly (JAMMER_OFF, then closes the device) at any point.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from killerbee import KillerBee, KBCapabilities  # type: ignore

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from subg_jam import parse_page_channels  # reuse, don't duplicate
from subg_scan import PAGE_INFO  # reuse, don't duplicate


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-i", "--iface", default="/dev/ttyACM0",
                     help="Serial device (default: /dev/ttyACM0)")
    ap.add_argument("-d", "--devtype", default="cc1354p10",
                     help="KillerBee hardware type: cc1354p10 (default) or cc1352p7 "
                          "- both implement on-chip sub-1GHz hop jamming, page 28 "
                          "and page 31, at the same channel plan on each")
    ap.add_argument("--page-channels", action="append", required=True,
                     metavar="PAGE:CHANNELS",
                     help="'PAGE:CHANNELS' (e.g. '31:9,14,15,19,20,24,106'), "
                          "repeatable - one per page (28 and/or 31). The full hop "
                          "sequence is every given page's channels in the order "
                          "the flags appear, repeating that same order each cycle.")
    ap.add_argument("--dwell", type=float, default=0.02,
                     help="Seconds to jam each channel before hopping to the next "
                          "(default: 0.02 = 20ms; converted to whole milliseconds "
                          "for the firmware, minimum 0.001)")
    ap.add_argument("--duration", type=float, default=0.0,
                     help="Stop after this many seconds total (default: 0 = "
                          "unbounded, use Ctrl+C instead)")
    args = ap.parse_args()

    hops = parse_page_channels(args.page_channels)  # exits with an error message on anything invalid
    # No flat-count cap here: jam_hop_subg_on() collapses this
    # list into contiguous (page, start, end) ranges before sending, so a
    # large contiguous span (e.g. 9-128) costs the same 3 wire bytes as a
    # single channel - it raises its own clear error if the *collapsed*
    # range count is still too large (many scattered, non-contiguous runs).

    dwell_ms = round(args.dwell * 1000)
    if dwell_ms < 1 or dwell_ms > 0xFFFF:
        print("error: --dwell out of range (valid: 0.001-65.535 seconds)", file=sys.stderr)
        sys.exit(1)

    kb = KillerBee(device=args.iface, hardware=args.devtype)
    for page in sorted(set(p for p, _ in hops)):
        page_info = PAGE_INFO[page]
        if not kb.check_capability(page_info["capability"]):
            print("error: %s does not report %s support (page %d)"
                  % (args.devtype, page_info["band_name"], page), file=sys.stderr)
            sys.exit(1)
    if not kb.check_capability(KBCapabilities.PHYJAM_HOP):
        print("error: %s does not report PHYJAM_HOP (on-chip channel-hop jam) "
              "support" % args.devtype, file=sys.stderr)
        sys.exit(1)

    print("On-chip hopping %d channel(s) across page(s) %s: %s"
          % (len(hops), ", ".join(str(p) for p in sorted(set(p for p, _ in hops))),
             args.page_channels))
    print("  %dms dwell/channel -> ~%dms per full cycle (all on-chip, no "
          "per-hop host round trip)" % (dwell_ms, dwell_ms * len(hops)))
    if args.duration:
        print("  stopping after %.1fs total" % args.duration)
    else:
        print("  running until Ctrl+C")
    print()

    run_start = time.time()
    jamming = False
    try:
        kb.jam_hop_subg_on(hops, dwell_ms)
        jamming = True
        if args.duration:
            time.sleep(args.duration)
        else:
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        if jamming:
            print("Stopping jammer...")
            try:
                kb.jammer_off()
            except Exception as e:
                print("warning: jammer_off() failed: %s" % e, file=sys.stderr)
        try:
            kb.close()
        except Exception:
            pass

    elapsed = time.time() - run_start
    print("Done. Ran for %.1fs." % elapsed)


if __name__ == "__main__":
    main()
