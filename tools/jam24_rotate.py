#!/usr/bin/env python3
"""
jam24_rotate.py - Rotating continuous-carrier jammer for specific 2.4GHz
IEEE 802.15.4 channels (11-26), using KillerBee's CC1352P7 driver.

Starts constant-carrier PHY jamming (KBCapabilities.PHYJAM - modulated
PRBS-15 garbage, not reflexive/reactive) on the first channel, then cycles
through the rest of the channel list with a configurable dwell each,
keeping the jammer continuously active across every hop (SET_CHANNEL
restarts the jam on the new channel automatically - see the firmware's
KB_CMD_SET_CHANNEL handler). This is a true flood - full channel occupancy
while dwelling on a channel - not the reactive, ACK-triggered PHYJAM_REFLEX
mode (which only bursts in response to detected traffic).

2.4GHz analog of subg_jam.py - see that script for the sub-1GHz version.
Constant-carrier jamming and its stop path are hardware-validated clean on
both CC1352P7 and CC1354P10 (JAMMER_ON -> JAMMER_OFF -> PING with no reboot,
channel rotation while jamming stays active) - see
firmware/src/kb-cc1352p7/README.md's Validation status section.

Defaults to channels 11 and 25 (2405/2475 MHz) - channel 11 carries real
ambient 802.15.4 traffic in this environment (a nearby, unrelated
network); channel 25 is otherwise silent but showed a receiver image-
leakage artifact from channel 11's strong signal during a full-band scan.

Usage:
    python3 tools/jam24_rotate.py
    python3 tools/jam24_rotate.py -c 11,25 --dwell 5
    python3 tools/jam24_rotate.py -c 11,15,20,25 --dwell 2 --cycles 10
    python3 tools/jam24_rotate.py -c 11,25 --dwell 3 --duration 300

Ctrl+C stops cleanly (JAMMER_OFF, then closes the device) at any point.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from killerbee import KillerBee, KBCapabilities  # type: ignore


def freq_mhz(ch: int) -> int:
    return 2405 + 5 * (ch - 11)


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
    return channels


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-i", "--iface", default="/dev/ttyACM2",
                     help="Serial device (default: /dev/ttyACM2)")
    ap.add_argument("-d", "--devtype", default="cc1352p7",
                     help="KillerBee hardware type (default: cc1352p7)")
    ap.add_argument("-c", "--channels", default="11,25",
                     help="Channel spec, e.g. '11,25' or '11-26' (default: 11,25)")
    ap.add_argument("--dwell", type=float, default=5.0,
                     help="Seconds to jam each channel before hopping to the next "
                          "(default: 5.0)")
    ap.add_argument("--cycles", type=int, default=0,
                     help="Stop after this many full passes through the channel "
                          "list (default: 0 = run until Ctrl+C or --duration)")
    ap.add_argument("--duration", type=float, default=0.0,
                     help="Stop after this many seconds total (default: 0 = "
                          "unbounded, use Ctrl+C or --cycles instead)")
    args = ap.parse_args()

    channels = parse_channels(args.channels)
    if not channels:
        print("error: no channels given", file=sys.stderr)
        sys.exit(1)
    for ch in channels:
        if ch < 11 or ch > 26:
            print("error: channel %d out of range (valid: 11-26)" % ch, file=sys.stderr)
            sys.exit(1)

    kb = KillerBee(device=args.iface, hardware=args.devtype)
    if not kb.check_capability(KBCapabilities.FREQ_2400):
        print("error: %s does not report 2.4GHz (FREQ_2400) support" % args.devtype,
              file=sys.stderr)
        sys.exit(1)
    if not kb.check_capability(KBCapabilities.PHYJAM):
        print("error: %s does not report PHYJAM (constant-carrier jam) support"
              % args.devtype, file=sys.stderr)
        sys.exit(1)

    print("Jamming %d channel(s): %s" % (len(channels), args.channels))
    print("  %.1fs dwell/channel -> ~%.1fs per full cycle"
          % (args.dwell, len(channels) * args.dwell))
    if args.cycles:
        print("  stopping after %d cycle(s)" % args.cycles)
    elif args.duration:
        print("  stopping after %.1fs total" % args.duration)
    else:
        print("  running until Ctrl+C")
    print()

    run_start = time.time()
    cycle = 0
    jamming = False

    def print_hop(ch: int, note: str = "") -> None:
        print("=== ch %2d (%4d MHz) - %.1fs%s ===" % (ch, freq_mhz(ch), args.dwell, note))

    try:
        kb.driver.jammer_on(channel=channels[0], method="constant")
        jamming = True
        print_hop(channels[0])
        time.sleep(args.dwell)

        idx = 0
        while True:
            idx = (idx + 1) % len(channels)
            if idx == 0:
                cycle += 1
                if args.cycles and cycle >= args.cycles:
                    break
            if args.duration and (time.time() - run_start) >= args.duration:
                break

            ch = channels[idx]
            print_hop(ch, " [cycle %d]" % (cycle + 1) if idx == 0 else "")
            kb.set_channel(ch)  # firmware keeps the jam running across the hop
            time.sleep(args.dwell)
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
    print("Done. Ran for %.1fs (%d full cycle(s))." % (elapsed, cycle))


if __name__ == "__main__":
    main()
