#!/usr/bin/env python3
"""
subg_jam.py - Rotating continuous-carrier jammer for specific sub-1GHz
channels, using KillerBee's CC1354P10 driver. Supports two bands via
-p/--page: page 31 (default) = 915 MHz US ISM, channels 0-128; page 28 =
863-876 MHz EU/UK, channels 0-65 (CC1354P10 only - see
firmware/src/kb-cc1354p10/README.md's "Page 28 support" section).

Starts constant-carrier PHY jamming (KBCapabilities.PHYJAM - modulated
PRBS-15 garbage, not reflexive/reactive) on the first channel, then cycles
through the rest of the channel list with a configurable dwell each,
keeping the jammer continuously active across every hop. This is a true
flood - full channel occupancy while dwelling on a channel - not the
reactive, ACK-triggered PHYJAM_REFLEX mode (which only bursts in response
to detected traffic and has much lower duty cycle).

Only safe to automate like this because of a firmware fix made and
verified this session: sub-1GHz constant-carrier jamming used to be a
one-way trip (starting it broke UART communication, and stopping it forced
a full, non-self-recovering chip reboot - see
firmware/src/kb-cc1354p10/README.md's brown-out writeup). That was traced
to a hardware brown-out from the PA's current inrush and fixed by reducing
TX power to 0 dBm; once that landed, the forced-reboot stop path was
retested, found unnecessary, and removed. Verified live: JAMMER_ON
(constant) -> JAMMER_OFF -> PING all clean with no reboot, and a full
SET_CHANNEL rotation across multiple channels while jamming stayed active,
no hangs. If you're running this against firmware from before that fix,
it WILL wedge the board on the first channel - see the README for the
JTAG recovery procedure.

Usage:
    python3 tools/subg_jam.py -i /dev/cu.usbmodemLS4501DC1 -c 9,14,15,19,20,24,106 --dwell 0.036
    python3 tools/subg_jam.py -c 9,14,15,19,20,24,106 --dwell 2
    python3 tools/subg_jam.py -c 9,14,15,19,20,24,106 --dwell 1 --cycles 5
    python3 tools/subg_jam.py -c 9,14,15,19,20,24,106 --dwell 2 --duration 300
    python3 tools/subg_jam.py -p 28 -c 0,13,26,39,52,65 --dwell 2  # 863-876 MHz EU/UK

Ctrl+C stops cleanly (JAMMER_OFF, then closes the device) at any point.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from killerbee import KillerBee, KBCapabilities  # type: ignore

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from subg_scan import PAGE_INFO, parse_channels  # reuse, don't duplicate


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-i", "--iface", default="/dev/ttyACM0",
                     help="Serial device (default: /dev/ttyACM0)")
    ap.add_argument("-d", "--devtype", default="cc1354p10",
                     help="KillerBee hardware type (default: cc1354p10)")
    ap.add_argument("-c", "--channels", required=True,
                     help="Channel spec, e.g. '9,14,15,19,20,24,106' or '0-9'")
    ap.add_argument("-p", "--page", type=int, default=31, choices=(28, 31),
                     help="KillerBee page: 31 = 915 MHz US ISM, channels 0-128 "
                          "(default); 28 = 863-876 MHz EU/UK, channels 0-65 "
                          "(CC1354P10 only)")
    ap.add_argument("--dwell", type=float, default=2.0,
                     help="Seconds to jam each channel before hopping to the next "
                          "(default: 2.0)")
    ap.add_argument("--cycles", type=int, default=0,
                     help="Stop after this many full passes through the channel "
                          "list (default: 0 = run until Ctrl+C or --duration)")
    ap.add_argument("--duration", type=float, default=0.0,
                     help="Stop after this many seconds total (default: 0 = "
                          "unbounded, use Ctrl+C or --cycles instead)")
    args = ap.parse_args()

    page_info = PAGE_INFO[args.page]
    freq_mhz = page_info["freq_mhz"]
    max_channel = page_info["max_channel"]

    channels = parse_channels(args.channels)
    if not channels:
        print("error: no channels given", file=sys.stderr)
        sys.exit(1)
    for ch in channels:
        if ch < 0 or ch > max_channel:
            print("error: channel %d out of range (valid: 0-%d for page %d)"
                  % (ch, max_channel, args.page), file=sys.stderr)
            sys.exit(1)

    kb = KillerBee(device=args.iface, hardware=args.devtype)
    if not kb.check_capability(page_info["capability"]):
        print("error: %s does not report %s support (page %d)"
              % (args.devtype, page_info["band_name"], args.page), file=sys.stderr)
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
        print("=== ch %3d (%6.1f MHz) - %.1fs%s ===" % (ch, freq_mhz[ch], args.dwell, note))

    try:
        # kb.jammer_on() (the generic KillerBee front door) doesn't forward
        # `page` through to the driver - only kb.driver.jammer_on() takes
        # it - so the sub-1GHz page must be set here directly to actually
        # jam on the intended band rather than silently defaulting to
        # 2.4GHz (page 0).
        kb.driver.jammer_on(channel=channels[0], page=args.page, method="constant")
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
            kb.set_channel(ch, page=args.page)  # firmware keeps the jam running across the hop
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
