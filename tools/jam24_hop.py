#!/usr/bin/env python3
"""
jam24_hop.py - On-chip rotating constant-carrier jammer across specific
2.4GHz IEEE 802.15.4 channels (11-26), using KillerBee's CC1352P7 driver's
KBCapabilities.PHYJAM_HOP (CMD_JAM_HOP_ON).

Same jam (constant-carrier, modulated PRBS-15 garbage - KBCapabilities.
PHYJAM) and same channel rotation idea as tools/jam24_rotate.py, but the
hop loop runs entirely on the CC1352P7 itself - the host sends one command
to start it and one to stop it, with no round trip per channel hop.

Why this exists: driving the exact same rotation by having the host call
SET_CHANNEL once per hop (what jam24_rotate.py does) pays a real, fixed
per-hop cost - measured directly on this project's hardware at a near-flat
~30ms round trip per SET_CHANNEL call, regardless of whether any actual RF
retuning happens (a USB/debug-probe control-plane tax, not RF physics -
the RF core's own retune time is sub-millisecond, confirmed by the retuned
and non-retuned cases costing the identical ~30ms). That makes very short
dwells (well under ~30ms) meaningless with jam24_rotate.py - the ~30ms
floor dominates regardless of --dwell. Looping the hop on-chip instead
removes that tax entirely: the real per-hop cost becomes the RF core's own
CMD_FS retune polling grain (~1ms worst case) plus whatever dwell was
asked for, so a --dwell of e.g. 0.02s here actually means ~20ms per
channel, not ~50ms.

Trade-off: because the host isn't in the loop, this tool can't print a
live per-hop channel/frequency log the way jam24_rotate.py does - it just
starts the hop, waits (Ctrl+C, --duration, or forever), then stops it.
Verified on real hardware via an independent RF energy check (a second
board doing ambient RSSI sampling on each target channel while this tool
ran): every listed channel showed a strong RSSI spike (~-59 to -70 dBm)
against a ~-106 to -112 dBm noise floor, confirming genuine on-air hopping
across exactly the given channels, not just command-level success.

Minimum reliable --dwell (measured): pushed down from 20ms to find the
actual floor, same independent-RSSI-monitor method as above. 3ms is clean
and strong on every channel across repeated trials (~-57 to -68 dBm,
32-43 dB above baseline); 2ms still works but with less margin (~-74 to
-82 dBm). 1ms - the wire protocol's own floor (dwell_ms is a uint16 ms
value and 0 is rejected) - is marginal: at least one of four channels came
back measurably weaker or fully indistinguishable from noise in every 1ms
trial, consistent with rfTuneToChannel()'s own ~1ms-worst-case retune cost
being the same order of magnitude as the requested dwell at that point, so
actual per-channel on-air time gets inconsistent hop to hop. No hang/wedge
at any of these dwells during the hop itself, though heavy back-to-back
low-dwell runs did eventually trigger the documented "RF core can get
stuck after heavy use" state once - recovered cleanly via
tools/kb_jtag_recover.sh. Treat 3ms as the shortest --dwell to rely on;
2ms works with reduced margin; below that is not recommended.

Usage:
    python3 tools/jam24_hop.py -i /dev/cu.usbmodemL45003IW1 -c 11,12,13,14,15,20,22,26 --dwell 0.02
    python3 tools/jam24_hop.py -c 11,25 --dwell 0.1
    python3 tools/jam24_hop.py -c 11,15,20,25 --dwell 0.05 --duration 30

Ctrl+C stops cleanly (JAMMER_OFF, then closes the device) at any point.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from killerbee import KillerBee, KBCapabilities  # type: ignore


def parse_channels(spec: str) -> list:
    channels = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            channels.extend(range(int(lo), int(hi) + 1))
        else:
            channels.append(int(part))
    seen = set()
    ordered = []
    for c in channels:
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    return ordered


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-i", "--iface", default="/dev/ttyACM0",
                     help="Serial device (default: /dev/ttyACM0)")
    ap.add_argument("-d", "--devtype", default="cc1352p7",
                     help="KillerBee hardware type (default: cc1352p7 - the only "
                          "one that currently implements on-chip hop jamming)")
    ap.add_argument("-c", "--channels", required=True,
                     help="Channel spec, e.g. '11,12,13,14,15,20,22,26' or '11-26'")
    ap.add_argument("--dwell", type=float, default=0.02,
                     help="Seconds to jam each channel before hopping to the next "
                          "(default: 0.02 = 20ms; converted to whole milliseconds "
                          "for the firmware, minimum 0.001)")
    ap.add_argument("--duration", type=float, default=0.0,
                     help="Stop after this many seconds total (default: 0 = "
                          "unbounded, use Ctrl+C instead)")
    args = ap.parse_args()

    channels = parse_channels(args.channels)
    if not channels:
        print("error: no channels given", file=sys.stderr)
        sys.exit(1)
    for ch in channels:
        if ch < 11 or ch > 26:
            print("error: channel %d out of range (valid: 11-26)" % ch, file=sys.stderr)
            sys.exit(1)
    if len(channels) > 16:
        print("error: too many channels (%d given, max 16 - the real count of "
              "2.4GHz channels 11-26)" % len(channels), file=sys.stderr)
        sys.exit(1)

    dwell_ms = round(args.dwell * 1000)
    if dwell_ms < 1 or dwell_ms > 0xFFFF:
        print("error: --dwell out of range (valid: 0.001-65.535 seconds)", file=sys.stderr)
        sys.exit(1)

    kb = KillerBee(device=args.iface, hardware=args.devtype)
    if not kb.check_capability(KBCapabilities.FREQ_2400):
        print("error: %s does not report 2.4GHz (FREQ_2400) support" % args.devtype,
              file=sys.stderr)
        sys.exit(1)
    if not kb.check_capability(KBCapabilities.PHYJAM_HOP):
        print("error: %s does not report PHYJAM_HOP (on-chip channel-hop jam) "
              "support" % args.devtype, file=sys.stderr)
        sys.exit(1)

    print("On-chip hopping %d channel(s): %s" % (len(channels), args.channels))
    print("  %dms dwell/channel -> ~%dms per full cycle (all on-chip, no "
          "per-hop host round trip)" % (dwell_ms, dwell_ms * len(channels)))
    if args.duration:
        print("  stopping after %.1fs total" % args.duration)
    else:
        print("  running until Ctrl+C")
    print()

    run_start = time.time()
    jamming = False
    try:
        kb.jam_hop_on(channels, dwell_ms)
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
