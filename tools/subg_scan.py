#!/usr/bin/env python3
"""
subg_scan.py - Sub-1GHz (915 MHz US ISM) channel scanner for KillerBee's
CC1354P10 driver.

Scans a configurable set of channels (default 0-9, the first 10 of the
real 129-channel SUN O-QPSK Rate Mode 0 plan - 902.2 + 0.2*channel MHz,
spanning 902.2-927.8 MHz. Verified directly against TI's own ti154stack
(their real IEEE 802.15.4g/SUN protocol stack source, in the installed
SDK) rather than assumed - see firmware/src/kb-cc1354p10/README.md's
"Sub-1GHz support" section for the full story, including an earlier,
wrong channel plan this project used before being corrected), each for a
configurable dwell time, and writes:

  - one libpcap file per channel with any captured traffic
    (readable in Wireshark, or by killerbee/zbdump/zbconvert etc.)
  - a detailed per-channel + summary text report
  - a JSON file with the full structured results, for scripted reuse

Usage:
    python3 tools/subg_scan.py [options]

Examples:
    # Default: channels 0-9, 20s each, into ./subg_scan_<timestamp>/
    python3 tools/subg_scan.py

    # 60s dwell per channel, only channels 0, 64 (915.0 MHz exactly), 128
    python3 tools/subg_scan.py -t 60 -c 0,64,128

    # Full real channel plan (0-128, 129 channels - long at any real dwell)
    python3 tools/subg_scan.py -c 0-128

    # Custom device and output directory
    python3 tools/subg_scan.py -i /dev/ttyACM0 -o /tmp/myscan

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

# Real SUN O-QPSK Rate Mode 0 channel plan for the 902-928 MHz US band:
# 902.2 + 0.2*channel MHz, channels 0-128 - see
# firmware/src/kb-cc1354p10/main.c's rfTuneToChannel() comment for the
# TI ti154stack source verification behind these numbers. This is NOT
# what kb.frequency() would print (a different, generic page-31 formula
# from kbutils.py) - documented, deliberate divergence.
FREQ_MHZ = {ch: round(902.2 + 0.2 * ch, 1) for ch in range(0, 129)}

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
                  poll: float = 0.5) -> List[Dict[str, Any]]:
    kb.set_channel(ch, page=31)
    kb.sniffer_on()

    dumper = PcapDumper(DLT_IEEE802_15_4, pcap_path)
    packets: List[Dict[str, Any]] = []
    t_end = time.time() + dwell

    try:
        while time.time() < t_end:
            remaining = t_end - time.time()
            try:
                pkt = kb.pnext(timeout=min(poll, remaining) if remaining > 0 else poll)
            except SerialException:
                break
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

            dumper.pcap_dump(raw, ant_dbm=pkt.get("rssi"), freq_mhz=FREQ_MHZ[ch])
    finally:
        dumper.close()
        kb.sniffer_off()

    return packets


def format_report(channels: List[int], dwell: float,
                   results: Dict[int, List[Dict[str, Any]]],
                   outdir: str) -> str:
    lines = []
    lines.append("=" * 72)
    lines.append("Sub-1GHz (915 MHz US ISM) scan report")
    lines.append("Generated: %s" % datetime.datetime.now().isoformat())
    lines.append("Dwell per channel: %.1fs" % dwell)
    lines.append("Channels scanned: %s" % ", ".join(str(c) for c in channels))
    lines.append("Output directory: %s" % outdir)
    lines.append("=" * 72)

    total_packets = 0
    total_valid = 0
    all_addrs = set()

    for ch in channels:
        pkts = results.get(ch, [])
        valid = [p for p in pkts if p["crc_ok"]]
        total_packets += len(pkts)
        total_valid += len(valid)

        lines.append("")
        lines.append("--- Channel %d (%.1f MHz) ---" % (ch, FREQ_MHZ[ch]))
        lines.append("  Packets captured: %d (%d with valid CRC)" % (len(pkts), len(valid)))

        if not pkts:
            lines.append("  No activity.")
            continue

        rssis = [p["rssi_dbm"] for p in pkts if p["rssi_dbm"] is not None]
        if rssis:
            lines.append("  RSSI range: %d to %d dBm" % (min(rssis), max(rssis)))

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

    lines.append("")
    lines.append("=" * 72)
    lines.append("Summary: %d packets total (%d valid CRC) across %d channel(s)"
                  % (total_packets, total_valid, len(channels)))
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
    ap.add_argument("-c", "--channels", default="0-9",
                     help="Channel spec, e.g. '0-128' (full real plan) or '0,64,128' "
                          "(default: 0-9)")
    ap.add_argument("-o", "--outdir", default=None,
                     help="Output directory (default: ./subg_scan_<timestamp>)")
    args = ap.parse_args()

    channels = parse_channels(args.channels)
    for ch in channels:
        if ch < 0 or ch > 128:
            print("error: channel %d out of range (valid: 0-128)" % ch, file=sys.stderr)
            sys.exit(1)

    outdir = args.outdir or ("subg_scan_%s" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(outdir, exist_ok=True)

    kb = KillerBee(device=args.iface, hardware=args.devtype)
    if not kb.check_capability(KBCapabilities.FREQ_915):
        print("error: %s does not report sub-1GHz (FREQ_915) support" % args.devtype,
              file=sys.stderr)
        sys.exit(1)

    results: Dict[int, List[Dict[str, Any]]] = {}
    scanned: List[int] = []

    try:
        for ch in channels:
            pcap_path = os.path.join(outdir, "ch%d.pcap" % ch)
            print("=== Channel %d (%.1f MHz) - %.1fs ===" % (ch, FREQ_MHZ[ch], args.dwell))
            packets = scan_channel(kb, ch, args.dwell, pcap_path)
            results[ch] = packets
            scanned.append(ch)
            valid = sum(1 for p in packets if p["crc_ok"])
            print("  %d packet(s) captured (%d valid CRC) -> %s"
                  % (len(packets), valid, pcap_path))
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

    print("\nWrote %s and %s (plus one .pcap per channel) in %s"
          % (os.path.basename(report_path), os.path.basename(json_path), outdir))


if __name__ == "__main__":
    main()
