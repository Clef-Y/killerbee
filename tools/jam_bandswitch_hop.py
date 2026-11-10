#!/usr/bin/env python3
"""
jam_bandswitch_hop.py - Single-board jammer that rotates between an
on-chip 2.4GHz channel hop (page 0, KB_CMD_JAM_HOP_ON) and an on-chip
sub-1GHz channel hop (page 28 and/or 31, KB_CMD_JAM_HOP_SUBG_ON),
switching bands on a timer - one board covering both bands over time,
freeing a *second* board to act purely as an independent rssi_scan.py
monitor instead of also needing to jam.

Requires a CC1352P7: it's the only firmware with an on-chip hop mode for
BOTH page 0 (jam24_hop.py) and page 28/31 (subg_jam_hop.py) - the
CC1354P10 has no page-0/2.4GHz support at all, on-chip-hop or otherwise.

**This is NOT the same thing as a single on-chip hop sequence spanning
both bands per channel** (e.g. hopping 2.4GHz ch11 -> page 28 ch10 -> 2.4GHz
ch15 -> ... at the requested --dwell, all in one uninterrupted on-chip
loop) - the firmware has no such mode. KB_CMD_JAM_HOP_ON's hop list is
hardcoded page-0-only (jamHopThread() in main.c never touches BAND_SUBG)
and KB_CMD_JAM_HOP_SUBG_ON's is sub-1GHz-only (jamHopSubgThread() never
touches BAND_24GHZ) - two separate on-chip loops, two separate wire
commands, by design (see firmware/src/kb-cc1352p7/README.md's "On-chip
channel-hop jamming" section). What this script actually does: run the
2.4GHz on-chip hop (at --dwell, e.g. the 3ms measured floor - see
jam24_hop.py's docstring) for --band-duration seconds, stop it, switch
the radio to sub-1GHz and run the page 28/31 on-chip hop for
--band-duration seconds, stop it, switch back, repeat. Within each band
segment, channel hopping is the same real on-chip loop jam24_hop.py/
subg_jam_hop.py use - only the *band* switch is host-driven, once per
segment, not once per channel.

**Real, documented risk in this loop, not a hypothetical:** each band
switch calls the firmware's rfSwitchBand() (RF_close()+RF_open() -
TI SDK internals, not this project's code), the same "unreliable blocking
wait" already documented as a real, if intermittent, hang risk elsewhere
in this project (see firmware/src/kb-cc1352p7/README.md's shared-risk
note and rfSwitchBand()'s own comment in main.c). rssi_scan.py's own
docstring found the 2.4GHz-into-sub-1GHz direction specifically higher-
risk than the reverse. This script's loop does that risky direction once
per cycle, indefinitely if left running - accepted here because that's
literally what "switch between page 28 and 2.4GHz on one board" requires,
but --band-duration should stay a real number of seconds (not sub-second)
to keep the switch-attempt rate sane, and every switch is health-checked
(see below) rather than blindly trusted.

Health check after every switch: issues a real command (get_dev_info(),
i.e. CMD_PING) right after starting the new band's hop, since this
project has already hardware-validated that the board stays fully
responsive to PING/GET_CHANNEL while an on-chip hop is running. If that
check fails, this is treated as a wedged band switch: the loop stops
immediately (not blindly running --band-duration against a possibly-dead
board) and prints the JTAG recovery command rather than retrying blind.

Usage:
    python3 tools/jam_bandswitch_hop.py -i /dev/cu.usbmodemL45003IW1 \\
        -c 11,15,20,25 --page-channels 28:9,20,40,65 \\
        --dwell 0.003 --band-duration 3 --duration 30

    # Unbounded, Ctrl+C to stop (stops whichever band is currently active):
    python3 tools/jam_bandswitch_hop.py -i /dev/cu.usbmodemL45003IW1 \\
        -c 11,25 --page-channels 28:9-65 --dwell 0.003 --band-duration 5

Ctrl+C (or --duration/--cycles) stops the currently-active band's jam
cleanly, then closes the device.
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
    ap.add_argument("-i", "--iface", default="/dev/ttyACM0",
                     help="Serial device (default: /dev/ttyACM0) - must be a "
                          "CC1352P7, the only board with on-chip hop on both bands")
    ap.add_argument("-d", "--devtype", default="cc1352p7",
                     help="KillerBee hardware type (default: cc1352p7)")
    ap.add_argument("-c", "--channels", required=True,
                     help="2.4GHz channel spec, e.g. '11,15,20,25' or '11-26'")
    ap.add_argument("--page-channels", action="append", required=True,
                     metavar="PAGE:CHANNELS",
                     help="Sub-1GHz 'PAGE:CHANNELS' (e.g. '28:9,20,40,65'), "
                          "repeatable - one per page (28 and/or 31)")
    ap.add_argument("--dwell", type=float, default=0.003,
                     help="Seconds per channel within each band's on-chip hop "
                          "(default: 0.003 = 3ms, the measured reliable floor - "
                          "see jam24_hop.py/subg_jam_hop.py docstrings)")
    ap.add_argument("--band-duration", type=float, default=3.0,
                     help="Seconds to run each band before switching to the "
                          "other (default: 3.0) - keep this a real number of "
                          "seconds, not sub-second, given the band-switch risk "
                          "described above")
    ap.add_argument("--cycles", type=int, default=0,
                     help="Stop after this many full 2.4GHz+sub-1GHz cycles "
                          "(default: 0 = run until Ctrl+C or --duration)")
    ap.add_argument("--duration", type=float, default=0.0,
                     help="Stop after this many seconds total (default: 0 = "
                          "unbounded, use Ctrl+C or --cycles instead)")
    args = ap.parse_args()

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
        print("error: too many 2.4GHz channels (%d given, max 16)" % len(channels24),
              file=sys.stderr)
        sys.exit(1)

    hops_subg = parse_page_channels(args.page_channels)  # exits on its own on bad input

    dwell_ms = round(args.dwell * 1000)
    if dwell_ms < 1 or dwell_ms > 0xFFFF:
        print("error: --dwell out of range (valid: 0.001-65.535 seconds)", file=sys.stderr)
        sys.exit(1)
    if args.band_duration <= 0:
        print("error: --band-duration must be > 0", file=sys.stderr)
        sys.exit(1)

    kb = KillerBee(device=args.iface, hardware=args.devtype)

    ok = True
    if not kb.check_capability(KBCapabilities.FREQ_2400):
        print("error: %s does not report 2.4GHz (FREQ_2400) support" % args.devtype,
              file=sys.stderr)
        ok = False
    for page in sorted(set(p for p, _ in hops_subg)):
        page_info = PAGE_INFO[page]
        if not kb.check_capability(page_info["capability"]):
            print("error: %s does not report %s support (page %d)"
                  % (args.devtype, page_info["band_name"], page), file=sys.stderr)
            ok = False
    if not kb.check_capability(KBCapabilities.PHYJAM_HOP):
        print("error: %s does not report PHYJAM_HOP (on-chip channel-hop jam) "
              "support" % args.devtype, file=sys.stderr)
        ok = False
    if not ok:
        kb.close()
        sys.exit(1)

    subg_pages = sorted(set(p for p, _ in hops_subg))
    print("Band-switching hop on %s (%s):" % (args.iface, args.devtype))
    print("  2.4GHz: %d channel(s) [%s], %dms dwell" % (len(channels24), args.channels, dwell_ms))
    print("  Sub-1GHz: %d hop(s) across page(s) %s, %dms dwell"
          % (len(hops_subg), ", ".join(str(p) for p in subg_pages), dwell_ms))
    print("  %.1fs per band before switching" % args.band_duration)
    if args.cycles:
        print("  stopping after %d cycle(s)" % args.cycles)
    if args.duration:
        print("  stopping after %.1fs total" % args.duration)
    if not args.cycles and not args.duration:
        print("  running until Ctrl+C")
    print()

    def health_check(label: str) -> bool:
        try:
            kb.get_dev_info()
            return True
        except Exception as e:
            print("error: %s - board unresponsive after band switch (%s)"
                  % (label, e), file=sys.stderr)
            print("  Likely a wedged band switch (rfSwitchBand()'s documented "
                  "risk - see this script's docstring). Recover with:",
                  file=sys.stderr)
            print("    tools/kb_jtag_recover.sh %s" % args.devtype, file=sys.stderr)
            return False

    run_start = time.time()
    cycle = 0
    active_band = None  # "24" or "subg", tracks what to stop in finally
    try:
        while True:
            print("[t=%6.1fs] switching to 2.4GHz..." % (time.time() - run_start))
            kb.jam_hop_on(channels24, dwell_ms)
            active_band = "24"
            if not health_check("2.4GHz hop start"):
                break
            print("[t=%6.1fs] 2.4GHz active" % (time.time() - run_start))
            time.sleep(args.band_duration)
            kb.jammer_off()
            active_band = None

            print("[t=%6.1fs] switching to sub-1GHz..." % (time.time() - run_start))
            kb.jam_hop_subg_on(hops_subg, dwell_ms)
            active_band = "subg"
            if not health_check("sub-1GHz hop start"):
                break
            print("[t=%6.1fs] sub-1GHz active" % (time.time() - run_start))
            time.sleep(args.band_duration)
            kb.jammer_off()
            active_band = None

            cycle += 1
            if args.cycles and cycle >= args.cycles:
                break
            if args.duration and (time.time() - run_start) >= args.duration:
                break
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        if active_band is not None:
            print("Stopping jammer (%s band)..." % active_band)
            try:
                kb.jammer_off()
            except Exception as e:
                print("warning: jammer_off() failed: %s" % e, file=sys.stderr)
        try:
            kb.close()
        except Exception:
            pass

    elapsed = time.time() - run_start
    print("Done. Ran for %.1fs, %d full band-switch cycle(s)." % (elapsed, cycle))


if __name__ == "__main__":
    main()
