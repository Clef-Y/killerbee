#!/usr/bin/env python3
"""
scan24.py - 2.4GHz IEEE 802.15.4 channel scanner for KillerBee's CC1354P10
driver.

2.4GHz analog of subg_scan.py - see that script for the sub-1GHz version.
Same output shape, same rationale for every design choice; only the band,
channel plan, and default channel range differ.

Scans a configurable set of 2.4GHz channels (default 11-26, the full real
IEEE 802.15.4 2.4GHz plan - 2405 + 5*(channel-11) MHz, spanning
2405-2480 MHz), each for a configurable dwell time, and writes:

  - one libpcap file per channel that captured at least one packet -
    channels with zero activity are left out entirely, not written as an
    empty header-only file (readable in Wireshark, or by
    killerbee/zbdump/zbconvert etc.)
  - a detailed per-channel + summary text report, including ambient RSSI
    (direct RF-core energy sampling via a firmware CMD_GET_RSSI command,
    independent of packet capture) for every channel, even ones with zero
    decoded packets - see --rssi-interval/--no-rssi
  - a JSON file with the full structured results, for scripted reuse

In this project's own environment, a full 2.4GHz scan found real ambient
802.15.4 traffic concentrated almost entirely on channel 11 (a nearby,
unrelated network), with channels 15 and 25 showing what looked like
receiver image-leakage artifacts from that strong nearby signal rather
than independent traffic - worth keeping in mind when reading a report
from an environment with its own strong in-band signal: a "busy" channel
next to an otherwise-silent band is a real, checkable finding, not
necessarily a second device.

Usage:
    python3 tools/scan24.py [options]

Examples:
    # Default: channels 11-26, 20s each, into ./scan24_<timestamp>/
    python3 tools/scan24.py

    # 60s dwell per channel, only channels 11, 15, 20, 25
    python3 tools/scan24.py -t 60 -c 11,15,20,25

    # Custom device and output directory
    python3 tools/scan24.py -i /dev/ttyACM0 -o /tmp/my24scan

Interrupting with Ctrl+C stops the current channel's dwell early and still
writes out the report for everything captured so far, rather than losing
the whole run.
"""

import argparse
import datetime
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from serial import SerialException  # type: ignore
from scapy.all import Dot15d4FCS  # type: ignore
from scapy.layers.dot15d4 import Dot15d4Data, Dot15d4Beacon, Dot15d4Cmd  # type: ignore

from killerbee import KillerBee, KBCapabilities, PcapDumper, DLT_IEEE802_15_4  # type: ignore

# Real IEEE 802.15.4 2.4GHz channel plan: 2405 + 5*(channel-11) MHz,
# channels 11-26, spanning 2405-2480 MHz.
FREQ_MHZ = {ch: 2405 + 5 * (ch - 11) for ch in range(11, 27)}

FRAME_TYPE_NAMES = {0: "Beacon", 1: "Data", 2: "Ack", 3: "MAC Command"}


def parse_channels(spec: str) -> List[int]:
    channels: List[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            channels.extend(range(int(lo), int(hi) + 1))
        else:
            channels.append(int(part))
    # de-dupe, keep order
    seen = set()
    ordered = []
    for c in channels:
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    return ordered


def decode_frame(raw: bytes) -> Dict[str, Any]:
    """Best-effort IEEE 802.15.4 MHR decode via scapy. Never raises -
    returns {"decode_error": ...} on anything unparseable, since a scan
    should keep going even for garbled/partial captures."""
    info: Dict[str, Any] = {}
    try:
        pkt = Dot15d4FCS(raw)
        info["frame_type"] = FRAME_TYPE_NAMES.get(int(pkt.fcf_frametype),
                                                    "Reserved(%d)" % pkt.fcf_frametype)
        info["ack_request"] = bool(pkt.fcf_ackreq)
        info["security_enabled"] = bool(pkt.fcf_security)
        info["seq_num"] = int(pkt.seqnum)
        info["summary"] = pkt.summary()
        if pkt.haslayer(Dot15d4Data):
            d = pkt[Dot15d4Data]
            info["dst_pan"] = "0x%04x" % d.dest_panid if d.dest_panid is not None else None
            info["dst_addr"] = "0x%04x" % d.dest_addr if isinstance(d.dest_addr, int) else d.dest_addr
            info["src_pan"] = "0x%04x" % d.src_panid if d.src_panid is not None else None
            info["src_addr"] = "0x%04x" % d.src_addr if isinstance(d.src_addr, int) else d.src_addr
        elif pkt.haslayer(Dot15d4Beacon):
            b = pkt[Dot15d4Beacon]
            info["src_pan"] = "0x%04x" % b.src_panid if b.src_panid is not None else None
            info["src_addr"] = "0x%04x" % b.src_addr if isinstance(b.src_addr, int) else b.src_addr
            info["pan_coordinator"] = bool(getattr(b, "pan_coord", False))
        elif pkt.haslayer(Dot15d4Cmd):
            c = pkt[Dot15d4Cmd]
            info["dst_pan"] = "0x%04x" % c.dest_panid if c.dest_panid is not None else None
            info["dst_addr"] = "0x%04x" % c.dest_addr if isinstance(c.dest_addr, int) else c.dest_addr
            info["src_pan"] = "0x%04x" % c.src_panid if c.src_panid is not None else None
            info["src_addr"] = "0x%04x" % c.src_addr if isinstance(c.src_addr, int) else c.src_addr
            info["cmd_id"] = int(c.cmd_id) if hasattr(c, "cmd_id") else None
    except Exception as e:
        info["decode_error"] = str(e)
    return info


def scan_channel(kb: KillerBee, ch: int, dwell: float, pcap_path: str,
                  poll: float = 0.5, rssi_interval: float = 1.0,
                  sample_rssi: bool = True
                  ) -> "tuple[List[Dict[str, Any]], List[Dict[str, Any]]]":
    """Listens on channel ch for dwell seconds. The pcap file at pcap_path
    is only created on the *first* captured packet (lazy open) - a
    channel with zero activity leaves no pcap file behind at all, rather
    than a 24-byte empty-header file.

    Also samples ambient channel energy (regardless of packet activity)
    roughly every rssi_interval seconds via kb.driver.get_rssi() - TI's
    RF_getRssi(), a direct RF-core read, not derived from captured
    packets. This is the only way to tell a genuinely quiet channel from
    one with real RF energy but no decodable 802.15.4 traffic. If the
    connected firmware doesn't support it (older firmware, no
    CMD_GET_RSSI), sample_rssi is expected to already be False - see
    main()'s startup probe.

    Returns (packets, rssi_samples)."""
    kb.set_channel(ch)
    kb.sniffer_on()

    dumper: Optional[PcapDumper] = None
    packets: List[Dict[str, Any]] = []
    rssi_samples: List[Dict[str, Any]] = []
    t_end = time.time() + dwell
    next_rssi_sample = time.time()

    try:
        while time.time() < t_end:
            remaining = t_end - time.time()
            try:
                pkt = kb.pnext(timeout=min(poll, remaining) if remaining > 0 else poll)
            except SerialException:
                break

            if sample_rssi and time.time() >= next_rssi_sample:
                try:
                    rssi = kb.driver.get_rssi()
                except Exception:
                    sample_rssi = False
                else:
                    if rssi is not None:
                        rssi_samples.append({
                            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                            "rssi_dbm": rssi,
                        })
                next_rssi_sample = time.time() + rssi_interval

            if pkt is None:
                continue

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
            packets.append(record)

            if dumper is None:
                dumper = PcapDumper(DLT_IEEE802_15_4, pcap_path)
            dumper.pcap_dump(raw, ant_dbm=pkt.get("rssi"), freq_mhz=FREQ_MHZ[ch])
    finally:
        if dumper is not None:
            dumper.close()
        kb.sniffer_off()

    return packets, rssi_samples


def rssi_stats(samples: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    vals = [s["rssi_dbm"] for s in samples]
    if not vals:
        return None
    return {
        "min": min(vals),
        "max": max(vals),
        "avg": sum(vals) / len(vals),
        "count": len(vals),
    }


def format_report(channels: List[int], dwell: float,
                   results: Dict[int, Dict[str, Any]],
                   outdir: str) -> str:
    lines = []
    lines.append("=" * 72)
    lines.append("2.4GHz IEEE 802.15.4 scan report")
    lines.append("Generated: %s" % datetime.datetime.now().isoformat())
    lines.append("Dwell per channel: %.1fs" % dwell)
    lines.append("Channels scanned: %s" % ", ".join(str(c) for c in channels))
    lines.append("Output directory: %s" % outdir)
    lines.append("=" * 72)

    total_packets = 0
    total_valid = 0
    all_addrs = set()
    active_channels = 0
    quiet_lines = []

    for ch in channels:
        chan = results.get(ch, {})
        pkts = chan.get("packets", [])
        samples = chan.get("rssi_samples", [])
        valid = [p for p in pkts if p["crc_ok"]]
        total_packets += len(pkts)
        total_valid += len(valid)
        stats = rssi_stats(samples)

        if not pkts:
            # No packet activity on this channel. Rather than a verbose
            # per-channel "No activity" block, show one compact line with
            # the ambient RSSI reading (direct RF-core energy sample, not
            # derived from packets - see scan_channel()'s get_rssi() use)
            # so a channel with real RF energy but no decodable 802.15.4
            # traffic is still distinguishable from a truly dead one. If
            # ambient sampling is unavailable, skip the channel entirely,
            # same as before.
            if stats:
                quiet_lines.append(
                    "  ch %2d (%4d MHz): ambient min=%4d avg=%6.1f max=%4d dBm, no packets"
                    % (ch, FREQ_MHZ[ch], stats["min"], stats["avg"], stats["max"])
                )
            continue

        active_channels += 1
        lines.append("")
        lines.append("--- Channel %d (%d MHz) ---" % (ch, FREQ_MHZ[ch]))
        lines.append("  Packets captured: %d (%d with valid CRC)" % (len(pkts), len(valid)))
        if stats:
            lines.append("  Ambient RSSI: min=%d avg=%.1f max=%d dBm (%d samples)"
                          % (stats["min"], stats["avg"], stats["max"], stats["count"]))

        rssis = [p["rssi_dbm"] for p in pkts if p["rssi_dbm"] is not None]
        if rssis:
            lines.append("  Packet RSSI range: %d to %d dBm" % (min(rssis), max(rssis)))

        for p in pkts:
            addr_bits = []
            for key in ("src_addr", "dst_addr"):
                if p.get(key):
                    all_addrs.add(p[key])
            for key, label in (("src_addr", "src"), ("dst_addr", "dst")):
                if p.get(key):
                    addr_bits.append("%s=%s" % (label, p[key]))
            addr_str = (" " + " ".join(addr_bits)) if addr_bits else ""
            crc_str = "OK " if p["crc_ok"] else "BAD"
            ftype = p.get("frame_type", "?")
            lines.append(
                "  [%s] CRC=%s len=%3d rssi=%s type=%-11s%s"
                % (p["timestamp"], crc_str, p["length"],
                   str(p["rssi_dbm"]), ftype, addr_str)
            )
            if p.get("decode_error"):
                lines.append("      (decode error: %s)" % p["decode_error"])

    if quiet_lines:
        lines.append("")
        lines.append("--- Silent channels (ambient RSSI only, no packets) ---")
        lines.extend(quiet_lines)

    if active_channels == 0 and not quiet_lines:
        lines.append("")
        lines.append("No activity on any of the %d channel(s) scanned." % len(channels))

    lines.append("")
    lines.append("=" * 72)
    lines.append("Summary: %d packets total (%d valid CRC) - %d of %d channel(s) "
                  "had any packet activity"
                  % (total_packets, total_valid, active_channels, len(channels)))
    if all_addrs:
        lines.append("Unique addresses seen: %s" % ", ".join(sorted(all_addrs)))
    else:
        lines.append("No devices/addresses identified.")
    lines.append("=" * 72)
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-i", "--iface", default="/dev/ttyACM0",
                     help="Serial device (default: /dev/ttyACM0)")
    ap.add_argument("-d", "--devtype", default="cc1354p10",
                     help="KillerBee hardware type (default: cc1354p10)")
    ap.add_argument("-t", "--dwell", type=float, default=20.0,
                     help="Seconds to listen per channel (default: 20)")
    ap.add_argument("-c", "--channels", default="11-26",
                     help="Channel spec, e.g. '11-26' (full real plan) or '11,15,20,25' "
                          "(default: 11-26)")
    ap.add_argument("-o", "--outdir", default=None,
                     help="Output directory (default: ./scan24_<timestamp>)")
    ap.add_argument("--rssi-interval", type=float, default=1.0,
                     help="Seconds between ambient RSSI samples per channel (default: 1.0)")
    ap.add_argument("--no-rssi", action="store_true",
                     help="Disable ambient RSSI sampling (packet capture only)")
    args = ap.parse_args()

    channels = parse_channels(args.channels)
    for ch in channels:
        if ch < 11 or ch > 26:
            print("error: channel %d out of range (valid: 11-26)" % ch, file=sys.stderr)
            sys.exit(1)

    outdir = args.outdir or ("scan24_%s" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(outdir, exist_ok=True)

    kb = KillerBee(device=args.iface, hardware=args.devtype)
    if not kb.check_capability(KBCapabilities.FREQ_2400):
        print("error: %s does not report 2.4GHz (FREQ_2400) support" % args.devtype,
              file=sys.stderr)
        sys.exit(1)

    sample_rssi = not args.no_rssi
    if sample_rssi:
        try:
            kb.driver.get_rssi()
        except Exception as e:
            sample_rssi = False
            print("note: ambient RSSI sampling unavailable (%s) - continuing "
                  "without it (requires firmware with CMD_GET_RSSI support)" % e)

    results: Dict[int, Dict[str, Any]] = {}
    scanned: List[int] = []

    try:
        for ch in channels:
            pcap_path = os.path.join(outdir, "ch%d.pcap" % ch)
            print("=== Channel %d (%d MHz) - %.1fs ===" % (ch, FREQ_MHZ[ch], args.dwell))
            packets, rssi_samples = scan_channel(kb, ch, args.dwell, pcap_path,
                                                  rssi_interval=args.rssi_interval,
                                                  sample_rssi=sample_rssi)
            results[ch] = {"packets": packets, "rssi_samples": rssi_samples}
            scanned.append(ch)
            valid = sum(1 for p in packets if p["crc_ok"])
            stats = rssi_stats(rssi_samples)
            rssi_str = (" | ambient min=%d avg=%.1f max=%d dBm" %
                        (stats["min"], stats["avg"], stats["max"])) if stats else ""
            if packets:
                print("  %d packet(s) captured (%d valid CRC) -> %s%s"
                      % (len(packets), valid, pcap_path, rssi_str))
            else:
                print("  0 packets captured - no pcap written%s" % rssi_str)
    except KeyboardInterrupt:
        print("\nInterrupted - writing report for the %d channel(s) completed so far."
              % len(scanned))
    finally:
        try:
            kb.close()
        except Exception:
            pass

    report_text = format_report(scanned, args.dwell, results, outdir)
    print("\n" + report_text)

    report_path = os.path.join(outdir, "report.txt")
    with open(report_path, "w") as f:
        f.write(report_text + "\n")

    json_path = os.path.join(outdir, "report.json")
    with open(json_path, "w") as f:
        json.dump({
            "generated": datetime.datetime.now().isoformat(),
            "dwell_seconds": args.dwell,
            "channels": scanned,
            "results": results,
        }, f, indent=2, default=str)

    pcap_count = sum(1 for ch in scanned if results.get(ch, {}).get("packets"))
    print("\nWrote %s and %s (plus %d .pcap file(s), one per channel with "
          "activity) in %s"
          % (os.path.basename(report_path), os.path.basename(json_path),
             pcap_count, outdir))


if __name__ == "__main__":
    main()
