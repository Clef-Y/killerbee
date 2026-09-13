#!/usr/bin/env python3
"""
rssi_scan.py - Ambient RSSI scanner across every KillerBee page the
connected firmware supports: page 0 (2.4GHz, channels 11-26), page 31
(915MHz US ISM, channels 0-128), page 28 (863-876MHz EU/UK, channels
0-65). Works against both the CC1354P10 and CC1352P7 firmwares - both
configure the exact same three pages at the exact same channel plan (page
28 was ported verbatim between them - see firmware/src/kb-cc1354p10/
README.md's "Page 28 support" and firmware/src/kb-cc1352p7/README.md's
mirror of it), so one script and one set of channel numbers/frequencies
covers either board via -d.

Samples ambient RF energy directly (GET_RSSI while sniffing - a raw
RF-core energy read, independent of packet decode) rather than capturing
and decoding traffic - built for answering "is something transmitting on
this channel/page right now", e.g. verifying an on-chip hop jammer
running on a *different* board is actually on-air on the channels it
claims (exactly how this project's own hop-jamming features were
validated). For full packet capture + report, use scan24.py (2.4GHz) or
subg_scan.py (sub-1GHz, one page at a time) instead - this tool writes no
packets/pcap at all, RSSI only.

Per page, flags channels whose peak RSSI stands out well above that
page's own baseline (median of all its channels' average RSSI) as likely
carrying a real signal - a simple, self-relative threshold rather than an
assumed absolute noise floor, since ambient noise varies by environment
and by band.

Usage:
    python3 tools/rssi_scan.py -i /dev/cu.usbmodemL45003IW1 -d cc1352p7
    python3 tools/rssi_scan.py -i /dev/cu.usbmodemLS4501DC1 --pages 28,31 -t 1.0
    python3 tools/rssi_scan.py -i ... --pages 28 -c 9-65 -t 3 --threshold 20

If you're trying to verify a *hopping* jammer (e.g. jam24_hop.py /
subg_jam_hop.py running on another board) rather than a steady carrier,
use a --dwell longer than that jammer's own full hop cycle (channel count
* its dwell_ms) - a short dwell here samples on an independent,
unsynchronized clock and can alias against the hop timing, missing the
signal on some channels purely by chance even though it's really there.
Confirmed directly during this project's own hop-jammer validation: a
3-second dwell against a ~1.1s hop cycle caught clear spikes on most
channels (still not literally every one - the two clocks are still
unsynchronized, just much less likely to miss with a several-times-longer
dwell), while a much shorter dwell missed far more.

Troubleshooting: if every channel starts reporting "Device rejected
sniffer_on" partway through a run (rather than a clean RSSI reading or
"no samples"), the RF core has likely wedged into the documented
"RF core can get stuck after heavy use" state (see
firmware/src/kb-cc1354p10/README.md) - a real hardware/firmware
characteristic of this project's boards under heavy RF command load, not
specific to this script. A software RESET may not bring the USB link back
reliably; the documented fix is a JTAG-level nudge via the debug probe
(`dslite.sh --mode memory ...`, see that same README section) - a plain
unplug/replug of the board works too.

**CC1352P7 specifically: switching from page 0 into a sub-1GHz page is
higher-risk than the reverse.** Confirmed by direct, repeated testing
(not just the general note above), and apparently directional: page 0 ->
sub-1GHz reliably reproduced (twice, same way each time) a specific
two-step failure - the *next* SNIFFER_ON after the switch reports success
but GET_RSSI then returns nothing at all (not even the documented -128
error sentinel) for as long as you keep sampling, and the SNIFFER_ON
*after that* fails outright. Going the other way, sub-1GHz -> page 0, and
page 28/page 31 alone or mixed with each other, did not reproduce it in
the same testing. Matches (with more precision than previously written
down) the CC1352P7 README's already-documented "2.4GHz <-> sub-1GHz band
switch... same unreliable blocking wait" risk. This script defaults
`--pages` to 28,31,0 specifically so the one unavoidable transition (if
you scan both bands at all) happens sub-1GHz -> page 0 rather than the
riskier direction - a real mitigation based on this finding, not just
loss containment, though a small sample size (this project's own two
boards) means treat "directional" as a strong lead, not a certainty.

Ctrl+C stops cleanly and still prints/writes the summary for whatever was
collected so far.
"""

import argparse
import datetime
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from killerbee import KillerBee, KBCapabilities  # type: ignore


def _freq_page0(ch: int) -> float:
    return 2405 + 5 * (ch - 11)


def _freq_page31(ch: int) -> float:
    return round(902.2 + 0.2 * ch, 1)


def _freq_page28(ch: int) -> float:
    return round(863.0 + 0.2 * ch, 1)


# Shared by both firmwares - see module docstring for why one table covers either board.
PAGE_INFO: Dict[int, Dict[str, Any]] = {
    0: {"channels": list(range(11, 27)), "freq_fn": _freq_page0,
        "capability": KBCapabilities.FREQ_2400, "band_name": "2.4 GHz IEEE 802.15.4"},
    31: {"channels": list(range(0, 129)), "freq_fn": _freq_page31,
         "capability": KBCapabilities.FREQ_915, "band_name": "915 MHz US ISM"},
    28: {"channels": list(range(0, 66)), "freq_fn": _freq_page28,
         "capability": KBCapabilities.FREQ_863_WIDE, "band_name": "863-876 MHz EU/UK"},
}


def parse_channel_list(spec: str, valid: List[int]) -> List[int]:
    chans: List[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            chans.extend(range(int(lo), int(hi) + 1))
        else:
            chans.append(int(part))
    seen = set()
    ordered = []
    for c in chans:
        if c not in seen and c in valid:
            seen.add(c)
            ordered.append(c)
    return ordered


def sample_rssi(kb: KillerBee, page: int, channel: int, dwell: float,
                 poll_interval: float) -> Optional[Dict[str, Any]]:
    kb.set_channel(channel, page=page)
    kb.sniffer_on()
    samples: List[int] = []
    t_end = time.time() + dwell
    try:
        while time.time() < t_end:
            try:
                r = kb.driver.get_rssi()
            except Exception:
                break
            if r is not None:
                samples.append(r)
            # Throttled deliberately: an earlier version of this function
            # called get_rssi() back-to-back with no delay at all (~30ms
            # apart, purely the command round-trip time) and reliably broke
            # SNIFFER_ON on a *later* channel after a few hundred calls -
            # confirmed by direct testing, same general class of issue this
            # project has hit before with other RF commands fired without
            # any yield between them (see firmware/src/kb-cc1354p10/
            # README.md's CMD_INJECT command-pool section). A small sleep
            # between polls avoided it in the same test.
            time.sleep(poll_interval)
    finally:
        kb.sniffer_off()
    if not samples:
        return None
    return {
        "min": min(samples),
        "max": max(samples),
        "avg": sum(samples) / len(samples),
        "count": len(samples),
    }


def scan_page(kb: KillerBee, page: int, channels: List[int], dwell: float,
              poll_interval: float) -> Dict[int, Dict[str, Any]]:
    info = PAGE_INFO[page]
    results: Dict[int, Dict[str, Any]] = {}
    for ch in channels:
        freq = info["freq_fn"](ch)
        try:
            stats = sample_rssi(kb, page, ch, dwell, poll_interval)
        except Exception as e:
            # Don't let one bad channel (e.g. a transient SNIFFER_ON
            # rejection) lose every result already collected this run.
            print("  page %2d ch %3d (%7.1f MHz): error (%s) - skipping"
                  % (page, ch, freq, e))
            continue
        if stats:
            print("  page %2d ch %3d (%7.1f MHz): min=%4d avg=%6.1f max=%4d dBm (n=%d)"
                  % (page, ch, freq, stats["min"], stats["avg"], stats["max"], stats["count"]))
            results[ch] = stats
        else:
            print("  page %2d ch %3d (%7.1f MHz): no samples" % (page, ch, freq))
    return results


def flag_signals(results: Dict[int, Dict[str, Any]], threshold: float) -> List[Tuple[int, float]]:
    """Self-relative anomaly flagging: a channel's peak vs. its page's own
    median-of-averages, not an assumed absolute noise floor."""
    if not results:
        return []
    avgs = sorted(r["avg"] for r in results.values())
    baseline = avgs[len(avgs) // 2]
    flagged = [(ch, stats["max"] - baseline) for ch, stats in results.items()
               if stats["max"] - baseline >= threshold]
    return sorted(flagged, key=lambda x: -x[1])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-i", "--iface", required=True, help="Serial device")
    ap.add_argument("-d", "--devtype", default="cc1354p10", choices=("cc1354p10", "cc1352p7"),
                     help="KillerBee hardware type (default: cc1354p10)")
    ap.add_argument("-p", "--pages", default="28,31,0",
                     help="Comma list of pages to scan, in the order given (default: "
                          "28,31,0 - every page both firmwares configure, sub-1GHz "
                          "first and 2.4GHz last deliberately - see this script's "
                          "module docstring for the CC1352P7 band-switch caveat this "
                          "ordering limits the blast radius of, but does not avoid)")
    ap.add_argument("-c", "--channels", default=None,
                     help="Restrict to these channels (e.g. '9-65'), applied to every "
                          "page in --pages and clipped to that page's real range "
                          "(default: each page's full range)")
    ap.add_argument("-t", "--dwell", type=float, default=0.5,
                     help="Seconds to sample RSSI per channel (default: 0.5) - see "
                          "this script's module docstring if you're trying to catch a "
                          "hopping jammer specifically")
    ap.add_argument("--poll-interval", type=float, default=0.05,
                     help="Seconds to sleep between GET_RSSI polls within a channel's "
                          "dwell (default: 0.05). Keep this nonzero - polling as fast "
                          "as the command round trip allows (no delay at all) was "
                          "confirmed to eventually break SNIFFER_ON on a later channel")
    ap.add_argument("--threshold", type=float, default=15.0,
                     help="dB a channel's peak must be above its page's own median "
                          "channel RSSI to be flagged as a likely signal (default: 15.0)")
    ap.add_argument("-o", "--outdir", default=None,
                     help="Also write a JSON report here (default: print only)")
    args = ap.parse_args()

    try:
        pages = [int(p) for p in args.pages.split(",") if p.strip()]
    except ValueError:
        print("error: --pages must be a comma list of integers", file=sys.stderr)
        sys.exit(1)
    for p in pages:
        if p not in PAGE_INFO:
            print("error: unsupported page %d (valid: %s)"
                  % (p, ", ".join(str(k) for k in PAGE_INFO)), file=sys.stderr)
            sys.exit(1)

    kb = KillerBee(device=args.iface, hardware=args.devtype)

    all_results: Dict[int, Dict[int, Dict[str, Any]]] = {}
    prev_page: Optional[int] = None
    try:
        for page in pages:
            if args.devtype == "cc1352p7" and prev_page == 0 and page != 0:
                print("note: switching from page 0 (2.4GHz) into sub-1GHz on the "
                      "CC1352P7 - this specific direction has a confirmed, "
                      "reproducible failure mode on this board, see this script's "
                      "module docstring\n")
            prev_page = page
            info = PAGE_INFO[page]
            if not kb.check_capability(info["capability"]):
                print("skipping page %d (%s): %s does not report support\n"
                      % (page, info["band_name"], args.devtype))
                continue

            channels = info["channels"]
            if args.channels:
                channels = parse_channel_list(args.channels, info["channels"])
                if not channels:
                    print("skipping page %d: no valid channels in --channels %r for "
                          "this page\n" % (page, args.channels))
                    continue

            print("=== page %d (%s) - %d channel(s), %.2fs each ==="
                  % (page, info["band_name"], len(channels), args.dwell))
            all_results[page] = scan_page(kb, page, channels, args.dwell, args.poll_interval)
            print()
    except KeyboardInterrupt:
        print("\nInterrupted - reporting whatever was collected so far.\n")
    finally:
        try:
            kb.close()
        except Exception:
            pass

    print("=" * 72)
    print("Summary - likely signals (peak RSSI well above that page's own baseline)")
    print("=" * 72)
    any_flagged = False
    for page, results in all_results.items():
        flagged = flag_signals(results, args.threshold)
        if flagged:
            any_flagged = True
            info = PAGE_INFO[page]
            for ch, delta in flagged:
                stats = results[ch]
                print("  page %2d ch %3d (%7.1f MHz): max=%4d dBm, %.1f dB above page baseline"
                      % (page, ch, info["freq_fn"](ch), stats["max"], delta))
    if not any_flagged:
        print("  none - every scanned channel stayed close to its page's own baseline")
    print("=" * 72)

    if args.outdir:
        os.makedirs(args.outdir, exist_ok=True)
        report_path = os.path.join(
            args.outdir, "rssi_scan_%s.json" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        with open(report_path, "w") as f:
            json.dump({
                "generated": datetime.datetime.now().isoformat(),
                "devtype": args.devtype,
                "dwell": args.dwell,
                "poll_interval": args.poll_interval,
                "threshold": args.threshold,
                "results": {str(p): {str(c): s for c, s in r.items()}
                            for p, r in all_results.items()},
            }, f, indent=2)
        print("\nWrote %s" % report_path)


if __name__ == "__main__":
    main()
