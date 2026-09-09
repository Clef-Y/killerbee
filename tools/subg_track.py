#!/usr/bin/env python3
"""
subg_track.py - Fast sub-1GHz channel sweep + steady-source lock-on
tracker for KillerBee's CC1354P10 driver.

Honest framing up front: this is a *fast repeated sweep*, not true
synchronized frequency-hop following. A single half-duplex receiver
cannot predict or lock onto another device's hop sequence without
out-of-band coordination - nothing can do that from RX alone. What this
tool actually does is cycle through the channel list quickly enough that
a device transmitting periodically (whether on a fixed channel or hopping
on its own schedule) has a good chance of being caught the next time the
sweep revisits its current channel, then switches to a longer, focused
listen on that one channel to build up real evidence of a steady source
before reporting it - rather than reacting to a single, possibly-noise,
packet the way a plain scan does.

State machine, per run:
  SWEEP  - cycle the channel list with a short dwell each (--sweep-dwell).
           set_channel() is called once per hop; the firmware handles
           stop/retune/restart of an already-running sniffer atomically in
           a single round trip (confirmed against main.c's SET_CHANNEL
           handler), and channel hops within the sub-1GHz band never
           trigger the expensive/riskier RF_close()+RF_open() band switch
           - only a cheap CMD_FS retune - so this stays fast and safe.
           Measured on real hardware: ~32ms per hop round trip, so actual
           per-channel time is --sweep-dwell plus that overhead, not the
           dwell alone.
  LOCKED - the moment any packet (any CRC state - a sync-word match is
           itself a signal-presence indicator, see subg_scan.py's
           analysis notes) is seen on a channel during a sweep dwell,
           stay there and keep listening.
             - CONFIRMED if --confirm-min packets arrive within
               --confirm-window seconds of the first detection - real,
               steadily-repeating activity, not a one-off blip.
             - If --confirm-min isn't reached within --confirm-window,
               it's logged as unconfirmed/transient and the sweep resumes.
             - Once CONFIRMED, keeps listening (with a longer
               --confirmed-quiet-timeout, to build up a real capture)
               until it goes quiet, then finalizes that channel's pcap/
               report and resumes sweeping.

Ctrl+C stops cleanly at any point and writes out the report for
everything seen so far, confirmed or not.

Usage:
    python3 tools/subg_track.py [options]

Examples:
    # Default: sweep channels 0-24, 0.3s/channel, until Ctrl+C
    python3 tools/subg_track.py

    # Faster sweep, wider channel range, stop after 10 minutes
    python3 tools/subg_track.py -c 0-49 --sweep-dwell 0.2 --max-runtime 600
"""

import argparse
import datetime
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from serial import SerialException  # type: ignore

from killerbee import KillerBee, KBCapabilities, PcapDumper, DLT_IEEE802_15_4  # type: ignore
from subg_scan import FREQ_MHZ, decode_frame, parse_channels  # reuse, don't duplicate


def now_iso() -> str:
    return datetime.datetime.now().isoformat()


class ChannelTrack:
    """Accumulated evidence for one channel while LOCKED onto it."""

    def __init__(self, ch: int, first_seen: float):
        self.ch = ch
        self.first_seen = first_seen
        self.last_seen = first_seen
        self.packets: List[Dict[str, Any]] = []
        self.confirmed = False
        self.confirmed_at: Optional[float] = None

    def add(self, record: Dict[str, Any]) -> None:
        self.packets.append(record)
        self.last_seen = time.time()


def record_packet(kb: KillerBee, ch: int, pkt: Dict[str, Any]) -> Dict[str, Any]:
    raw = pkt["bytes"]
    record: Dict[str, Any] = {
        "channel": ch,
        "freq_mhz": FREQ_MHZ[ch],
        "timestamp": pkt["datetime"].isoformat() if pkt.get("datetime") else None,
        "rssi_dbm": pkt.get("rssi"),
        "crc_ok": bool(pkt.get("validcrc")),
        "length": len(raw),
        "hex": raw.hex(),
    }
    record.update(decode_frame(raw))
    return record


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-i", "--iface", default="/dev/ttyACM0")
    ap.add_argument("-d", "--devtype", default="cc1354p10")
    ap.add_argument("-c", "--channels", default="0-24",
                     help="Channel spec to sweep, e.g. '0-24' or '0,14,64' (default: 0-24)")
    ap.add_argument("--sweep-dwell", type=float, default=0.3,
                     help="Seconds to listen per channel while sweeping (default: 0.3)")
    ap.add_argument("--confirm-min", type=int, default=3,
                     help="Packets required within --confirm-window to call a source "
                          "'confirmed steady' (default: 3)")
    ap.add_argument("--confirm-window", type=float, default=20.0,
                     help="Seconds from first detection to reach --confirm-min "
                          "before giving up as unconfirmed (default: 20)")
    ap.add_argument("--quiet-timeout", type=float, default=8.0,
                     help="Seconds of silence before abandoning an UNCONFIRMED lock "
                          "and resuming the sweep (default: 8)")
    ap.add_argument("--confirmed-quiet-timeout", type=float, default=30.0,
                     help="Seconds of silence before finalizing a CONFIRMED lock "
                          "and resuming the sweep (default: 30)")
    ap.add_argument("--max-runtime", type=float, default=None,
                     help="Stop after this many seconds total (default: run until Ctrl+C)")
    ap.add_argument("-o", "--outdir", default=None,
                     help="Output directory (default: ./subg_track_<timestamp>)")
    args = ap.parse_args()

    channels = parse_channels(args.channels)
    for ch in channels:
        if ch < 0 or ch > 128:
            print("error: channel %d out of range (valid: 0-128)" % ch, file=sys.stderr)
            sys.exit(1)

    outdir = args.outdir or ("subg_track_%s" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(outdir, exist_ok=True)

    kb = KillerBee(device=args.iface, hardware=args.devtype)
    if not kb.check_capability(KBCapabilities.FREQ_915):
        print("error: %s does not report sub-1GHz (FREQ_915) support" % args.devtype,
              file=sys.stderr)
        sys.exit(1)

    # Set equal to the per-channel dwell (--sweep-dwell) at the user's
    # request, rather than the ~32ms actually measured for a set_channel()
    # round trip while sniffing (SET_CHANNEL retunes the already-running
    # CMD_PROP_RX in one UART round trip - see this file's header comment).
    # This only affects the printed cycle-time estimate below, not actual
    # sweep timing/behavior.
    HOP_OVERHEAD_S = args.sweep_dwell
    print("Sweeping %d channels (%s), %.2fs dwell each -> ~%.1fs per full sweep cycle "
          "(measured ~%dms/hop overhead included)"
          % (len(channels), args.channels, args.sweep_dwell,
             len(channels) * (args.sweep_dwell + HOP_OVERHEAD_S),
             int(HOP_OVERHEAD_S * 1000)))
    print("Confirm: %d+ packets within %.0fs of first detection. Ctrl+C to stop.\n"
          % (args.confirm_min, args.confirm_window))

    confirmed_sources: List[Dict[str, Any]] = []
    unconfirmed_blips: List[Dict[str, Any]] = []
    run_start = time.time()

    kb.set_channel(channels[0], page=31)
    kb.sniffer_on()

    try:
        idx = 0
        while True:
            if args.max_runtime is not None and (time.time() - run_start) >= args.max_runtime:
                print("\nMax runtime reached.")
                break

            ch = channels[idx % len(channels)]
            idx += 1

            kb.set_channel(ch, page=31)
            try:
                pkt = kb.pnext(timeout=args.sweep_dwell)
            except SerialException:
                print("error: lost communication with the device - it may need the "
                      "JTAG UART-resync step (see firmware README) before it will "
                      "respond again.", file=sys.stderr)
                break

            if pkt is None:
                continue

            # --- Activity seen: enter LOCKED state on this channel ---
            track = ChannelTrack(ch, time.time())
            track.add(record_packet(kb, ch, pkt))
            print("[%s] activity on channel %d (%.1f MHz) - locking on..."
                  % (now_iso(), ch, FREQ_MHZ[ch]))

            while True:
                if args.max_runtime is not None and (time.time() - run_start) >= args.max_runtime:
                    break

                quiet_limit = (args.confirmed_quiet_timeout if track.confirmed
                               else args.quiet_timeout)
                since_last = time.time() - track.last_seen

                if not track.confirmed and (time.time() - track.first_seen) > args.confirm_window:
                    print("  [%s] ch%d not confirmed (%d packet(s) in %.0fs, needed %d) - "
                          "resuming sweep" % (now_iso(), ch, len(track.packets),
                                               args.confirm_window, args.confirm_min))
                    unconfirmed_blips.append({
                        "channel": ch, "freq_mhz": FREQ_MHZ[ch],
                        "packet_count": len(track.packets),
                        "packets": track.packets,
                    })
                    break

                if since_last > quiet_limit:
                    if track.confirmed:
                        print("  [%s] ch%d gone quiet (%.0fs) - finalizing confirmed source "
                              "(%d packets total)" % (now_iso(), ch, quiet_limit,
                                                       len(track.packets)))
                        pcap_path = os.path.join(outdir, "ch%d_confirmed.pcap" % ch)
                        dumper = PcapDumper(DLT_IEEE802_15_4, pcap_path)
                        for p in track.packets:
                            dumper.pcap_dump(bytes.fromhex(p["hex"]),
                                              ant_dbm=p["rssi_dbm"], freq_mhz=FREQ_MHZ[ch])
                        dumper.close()
                        confirmed_sources.append({
                            "channel": ch, "freq_mhz": FREQ_MHZ[ch],
                            "first_seen": datetime.datetime.fromtimestamp(track.first_seen).isoformat(),
                            "last_seen": datetime.datetime.fromtimestamp(track.last_seen).isoformat(),
                            "packet_count": len(track.packets),
                            "valid_crc_count": sum(1 for p in track.packets if p["crc_ok"]),
                            "pcap": pcap_path,
                            "packets": track.packets,
                        })
                    else:
                        print("  [%s] ch%d gone quiet (%.0fs), never reached confirm "
                              "threshold - resuming sweep" % (now_iso(), ch, quiet_limit))
                        unconfirmed_blips.append({
                            "channel": ch, "freq_mhz": FREQ_MHZ[ch],
                            "packet_count": len(track.packets),
                            "packets": track.packets,
                        })
                    break

                try:
                    pkt = kb.pnext(timeout=min(1.0, max(0.1, quiet_limit - since_last)))
                except SerialException:
                    print("error: lost communication with the device.", file=sys.stderr)
                    pkt = None
                    track.confirmed = False  # force finalize-as-unconfirmed path below
                    break

                if pkt is None:
                    continue

                track.add(record_packet(kb, ch, pkt))

                if not track.confirmed and len(track.packets) >= args.confirm_min:
                    track.confirmed = True
                    track.confirmed_at = time.time()
                    print("  [%s] ch%d CONFIRMED steady source (%d packets in %.1fs)"
                          % (now_iso(), ch, len(track.packets),
                             track.confirmed_at - track.first_seen))
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        try:
            kb.sniffer_off()
        except Exception:
            pass
        try:
            kb.close()
        except Exception:
            pass

    # --- Final report ---
    lines = []
    lines.append("=" * 72)
    lines.append("Sub-1GHz fast-sweep + lock-on tracking report")
    lines.append("Generated: %s" % now_iso())
    lines.append("Channels swept: %s (%.2fs dwell each)" % (args.channels, args.sweep_dwell))
    lines.append("Confirm threshold: %d+ packets within %.0fs" % (args.confirm_min, args.confirm_window))
    lines.append("Total run time: %.1fs" % (time.time() - run_start))
    lines.append("=" * 72)

    lines.append("")
    lines.append("CONFIRMED steady sources: %d" % len(confirmed_sources))
    for src in confirmed_sources:
        lines.append("")
        lines.append("--- Channel %d (%.1f MHz) - CONFIRMED ---" % (src["channel"], src["freq_mhz"]))
        lines.append("  First seen: %s" % src["first_seen"])
        lines.append("  Last seen:  %s" % src["last_seen"])
        lines.append("  Packets: %d (%d valid CRC)" % (src["packet_count"], src["valid_crc_count"]))
        lines.append("  Pcap: %s" % src["pcap"])
        rssis = [p["rssi_dbm"] for p in src["packets"] if p["rssi_dbm"] is not None]
        if rssis:
            lines.append("  RSSI range: %d to %d dBm" % (min(rssis), max(rssis)))

    lines.append("")
    lines.append("Unconfirmed blips (activity seen, but didn't reach the confirm "
                  "threshold): %d" % len(unconfirmed_blips))
    for blip in unconfirmed_blips:
        lines.append("  channel %d (%.1f MHz): %d packet(s)"
                      % (blip["channel"], blip["freq_mhz"], blip["packet_count"]))

    lines.append("")
    lines.append("=" * 72)
    report_text = "\n".join(lines)
    print("\n" + report_text)

    with open(os.path.join(outdir, "report.txt"), "w") as f:
        f.write(report_text + "\n")
    with open(os.path.join(outdir, "report.json"), "w") as f:
        json.dump({
            "generated": now_iso(),
            "args": vars(args),
            "confirmed_sources": confirmed_sources,
            "unconfirmed_blips": unconfirmed_blips,
        }, f, indent=2, default=str)

    print("\nWrote report.txt, report.json, and any confirmed-source pcaps in %s" % outdir)


if __name__ == "__main__":
    main()
