#!/usr/bin/env bash
# Usage: ./parse_802154.py [file_or_dir]

TARGET="${1:-/home/mike/killerbee}"

# Collect pcap files (either a single passed file or all .pcap files in the directory)
if [ -f "$TARGET" ]; then
    PCAP_FILES=("$TARGET")
elif [ -d "$TARGET" ]; then
    shopt -s nullglob
    PCAP_FILES=("$TARGET"/*.pcap)
    shopt -u nullglob
else
    echo "Error: Path '$TARGET' not found."
    exit 1
fi

if [ ${#PCAP_FILES[@]} -eq 0 ]; then
    echo "No .pcap files found in $TARGET"
    exit 0
fi

# Pass collected files to Python via environment and heredoc
PYTHONPATH=/home/mike/killerbee /home/mike/killerbee/.venv/bin/python3 - "${PCAP_FILES[@]}" << 'EOF'
import sys
import datetime
from scapy.utils import rdpcap
from scapy.config import conf
from scapy.layers.dot15d4 import Dot15d4

conf.l2types.register(195, Dot15d4)

pcap_files = sys.argv[1:]

FRAME_TYPES = {
    0: "Beacon",
    1: "Data",
    2: "Acknowledgment",
    3: "MAC Command",
    4: "LLDN",
    5: "Multipurpose (MP)",
    6: "Fragment / Extended",
}

FRAME_VERSIONS = {
    0: "IEEE Std 802.15.4-2003",
    1: "IEEE Std 802.15.4-2006",
    2: "IEEE Std 802.15.4-2015 / 802.15.4g",
    3: "Reserved",
}

# Multipurpose frames use a 1-bit dst/src addressing scheme distinct from the
# 2-bit scheme used by general MAC frames. Per Table 7-3/7-4, dst_addr_mode
# in MP frames is 2 bits (0/2/3, same values as general frames), but
# src_addr is a single presence bit — when present it is ALWAYS EUI-64 (mode 3).
# We normalize both branches to the same 2-bit convention (0/2/3) used
# elsewhere in this parser so downstream address-parsing code needs no changes.


def parse_802154_frame(raw):
    if len(raw) < 2:
        return "Truncated frame (too short)"

    offset = 0
    fcf_byte0 = raw[0]
    frame_type = fcf_byte0 & 0x07

    # =========================================================================
    # BRANCH 1: Multipurpose Frames (Frame Type 5)
    # IEEE 802.15.4-2015 Tables 7-3 (1-octet FCF) and 7-4 (2-octet FCF).
    # NOTE: the MP FCF is NOT a truncated/shifted version of the general
    # 2-octet FCF — it has its own, independently defined bit assignments.
    # =========================================================================
    if frame_type == 5:
        long_fcf = (fcf_byte0 >> 3) & 0x01

        if long_fcf == 0:
            # ---- 1-Octet MP FCF (Table 7-3) ----
            # Bits: 0-2 FrameType | 3 LongFC | 4 DstAddrMode(1 bit: 0/2)
            #       | 5 SrcAddrPresent | 6 PANIdPresent | 7 Security
            fcf = fcf_byte0
            offset = 1

            dst_addr_bit = (fcf >> 4) & 0x01
            src_addr_present = (fcf >> 5) & 0x01
            pan_id_present = (fcf >> 6) & 0x01
            sec_enabled = (fcf >> 7) & 0x01

            # 1-octet form has no Frame Pending / AR / IE-Present / Frame
            # Version fields at all — they simply don't exist in this form.
            frame_pending = 0
            ack_req = 0
            ie_present = 0
            frame_ver = 2  # MP frames are a 2015/802.15.4g construct

            seqnum_suppress = 1  # always suppressed in the 1-octet form

            # Normalize to the 2-bit convention used by general frames.
            # 1-octet form only supports "no dst addr" or "16-bit short dst".
            dst_addr_mode = 2 if dst_addr_bit else 0
            src_addr_mode = 3 if src_addr_present else 0  # EUI-64 only

            pan_compress = None  # not used for MP; PAN presence set directly below
            _mp_dst_pan_present = bool(pan_id_present) and dst_addr_mode != 0
            _mp_src_pan_present = False  # 1-octet MP form carries only one PAN ID field, tied to dst
            _mp_explicit_pan = True

        else:
            # ---- 2-Octet MP FCF (Table 7-4) ----
            # Bits: 0-2 FrameType | 3 LongFC | 4-5 DstAddrMode(2 bit)
            #       | 6 SrcAddrPresent(1 bit, EUI-64 only) | 7 PANIdPresent
            #       | 8 Security | 9 SeqNumSuppress | 10 FramePending
            #       | 11 AR | 12 IEPresent | 13-14 FrameVersion | 15 Reserved
            if len(raw) < 2:
                return "Truncated MP Frame"
            fcf = int.from_bytes(raw[0:2], byteorder='little')
            offset = 2

            dst_addr_mode = (fcf >> 4) & 0x03
            src_addr_present = (fcf >> 6) & 0x01
            pan_id_present = (fcf >> 7) & 0x01
            sec_enabled = (fcf >> 8) & 0x01
            seqnum_suppress = (fcf >> 9) & 0x01
            frame_pending = (fcf >> 10) & 0x01
            ack_req = (fcf >> 11) & 0x01
            ie_present = (fcf >> 12) & 0x01
            frame_ver = (fcf >> 13) & 0x03

            src_addr_mode = 3 if src_addr_present else 0  # EUI-64 only

            pan_compress = None
            _mp_dst_pan_present = bool(pan_id_present) and dst_addr_mode != 0
            _mp_src_pan_present = False
            _mp_explicit_pan = True

    # =========================================================================
    # BRANCH 2: General MAC Frames (Data, Command, Beacon, Extended, etc.)
    # IEEE 802.15.4-2015 §7.2.2: Standard 2-Octet FCF
    # =========================================================================
    else:
        if len(raw) < 2:
            return "Truncated MAC Frame"
        fcf = int.from_bytes(raw[0:2], byteorder='little')
        offset = 2
        sec_enabled = (fcf >> 3) & 0x01
        frame_pending = (fcf >> 4) & 0x01
        ack_req = (fcf >> 5) & 0x01
        pan_compress = (fcf >> 6) & 0x01
        seqnum_suppress = (fcf >> 8) & 0x01
        ie_present = (fcf >> 9) & 0x01
        dst_addr_mode = (fcf >> 10) & 0x03
        frame_ver = (fcf >> 12) & 0x03
        src_addr_mode = (fcf >> 14) & 0x03
        _mp_explicit_pan = False  # use Table 7-2 logic below instead

    # -------------------------------------------------------------------------
    # 1. Dynamic Sequence Number Extraction
    # -------------------------------------------------------------------------
    seqnum = None
    if not seqnum_suppress:
        if len(raw) > offset:
            seqnum = raw[offset]
            offset += 1

    # -------------------------------------------------------------------------
    # 2. PAN ID & Addressing Field Evaluation
    #    General frames: IEEE 802.15.4-2015 Table 7-2 compression matrix.
    #    MP frames: PAN presence comes directly from the PAN ID Present bit
    #    (computed in each MP branch above), NOT the Table 7-2 matrix —
    #    Table 7-2 applies only to the general 2-octet-FCF frame formats.
    # -------------------------------------------------------------------------
    if _mp_explicit_pan:
        dst_pan_present = _mp_dst_pan_present
        src_pan_present = _mp_src_pan_present
    elif frame_ver == 2:
        if dst_addr_mode == 0 and src_addr_mode == 0:
            dst_pan_present = (pan_compress == 1)
            src_pan_present = False
        elif dst_addr_mode > 0 and src_addr_mode == 0:
            dst_pan_present = (pan_compress == 0)
            src_pan_present = False
        elif dst_addr_mode == 0 and src_addr_mode > 0:
            dst_pan_present = False
            src_pan_present = (pan_compress == 0)
        else:  # Both dst_addr and src_addr present
            if pan_compress == 0:
                dst_pan_present, src_pan_present = True, True
            else:
                dst_pan_present, src_pan_present = True, False
    else:
        # Legacy 2003/2006 PAN ID Compression Rules
        dst_pan_present = (dst_addr_mode != 0)
        src_pan_present = (src_addr_mode != 0 and pan_compress == 0)

    dst_pan = None
    src_pan = None
    dst_addr = None
    src_addr = None

    # Parse Destination PAN ID
    if dst_pan_present and len(raw) >= offset + 2:
        dst_pan = f"0x{int.from_bytes(raw[offset:offset+2], 'little'):04x}"
        offset += 2

    # Parse Destination Address
    if dst_addr_mode == 2 and len(raw) >= offset + 2:  # 16-bit Short
        dst_addr = f"0x{int.from_bytes(raw[offset:offset+2], 'little'):04x}"
        offset += 2
    elif dst_addr_mode == 3 and len(raw) >= offset + 8:  # 64-bit Extended (EUI-64)
        dst_addr = raw[offset:offset+8].hex(':')
        offset += 8

    # Parse Source PAN ID
    if src_pan_present and len(raw) >= offset + 2:
        src_pan = f"0x{int.from_bytes(raw[offset:offset+2], 'little'):04x}"
        offset += 2

    # Parse Source Address
    if src_addr_mode == 2 and len(raw) >= offset + 2:
        src_addr = f"0x{int.from_bytes(raw[offset:offset+2], 'little'):04x}"
        offset += 2
    elif src_addr_mode == 3 and len(raw) >= offset + 8:
        src_addr = raw[offset:offset+8].hex(':')
        offset += 8

    return {
        "fcf_raw": hex(fcf),
        "frame_type": f"{frame_type} ({FRAME_TYPES.get(frame_type, 'Reserved')})",
        "frame_ver": f"{frame_ver} ({FRAME_VERSIONS.get(frame_ver, 'Unknown')})",
        "sec_enabled": sec_enabled,
        "ack_req": ack_req,
        "seqnum_suppress": seqnum_suppress,
        "seqnum": seqnum if seqnum is not None else "Suppressed",
        "ie_present": ie_present,
        "dst_pan": dst_pan,
        "dst_addr": dst_addr,
        "src_pan": src_pan,
        "src_addr": src_addr,
        "mhr_bytes_parsed": offset,
        "payload_start_hex": raw[offset:].hex()
    }


for path in pcap_files:
    print(f"=== {path} ===")
    try:
        packets = rdpcap(path)
    except Exception as e:
        print(f"  could not read: {e}")
        continue

    for i, pkt in enumerate(packets):
        raw = bytes(pkt)
        result = parse_802154_frame(raw)
        if isinstance(result, str):
            print(f"[{i}] {result}")
            continue
        print(f"[{i}] type={result['frame_type']} ver={result['frame_ver']} "
              f"fcf={result['fcf_raw']} seq={result['seqnum']} "
              f"sec={result['sec_enabled']} ack_req={result['ack_req']} "
              f"dst_pan={result['dst_pan']} dst_addr={result['dst_addr']} "
              f"src_pan={result['src_pan']} src_addr={result['src_addr']} "
              f"payload={result['payload_start_hex']}")
    print()
EOF
