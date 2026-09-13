# KillerBee Usage Guide

A practical, example-driven guide to this KillerBee fork: the Python library, every
CLI tool in `tools/`, and the custom CC1352P7 / CC1354P10 firmware devices this
project adds on top of upstream KillerBee's original hardware support.

See also: [README.md](README.md) (project overview, supported hardware),
[ARCHITECTURE.md](ARCHITECTURE.md) (how the pieces fit together),
[firmware/src/kb-cc1352p7/README.md](firmware/src/kb-cc1352p7/README.md) and
[firmware/src/kb-cc1354p10/README.md](firmware/src/kb-cc1354p10/README.md)
(wire protocol, hardware bring-up notes, and JTAG recovery procedures for those
two boards specifically).

> **Legal/ethical note:** KillerBee is a security assessment framework for
> IEEE 802.15.4 and ZigBee. Injection, jamming, and the various flood/spoof
> tools below transmit real RF energy. Only point them at networks and
> hardware you own or are explicitly authorized to test.

---

## Table of contents

- [Setup](#setup)
- [Supported hardware](#supported-hardware)
- [Quick start](#quick-start)
- [CLI tools reference](#cli-tools-reference)
  - [Device discovery](#device-discovery)
  - [Capture & replay](#capture--replay)
  - [Jamming](#jamming)
  - [Attack / flood tools](#attack--flood-tools)
  - [Key & crypto tools](#key--crypto-tools)
  - [File conversion & analysis](#file-conversion--analysis)
  - [Interactive scapy shell](#interactive-scapy-shell)
  - [RSSI detection (CC1354P10 + CC1352P7, every page)](#rssi-detection-this-fork-cc1354p10--cc1352p7-every-page)
  - [Sub-1GHz tools (CC1354P10)](#sub-1ghz-tools-cc1354p10)
  - [Bootloader (RZUSBSTICK only)](#bootloader-rzusbstick-only)
  - [Currently broken in this fork](#currently-broken-in-this-fork)
- [Python library usage](#python-library-usage)
  - [Device discovery](#library-device-discovery)
  - [Capability checking](#capability-checking)
  - [Sniffing](#sniffing)
  - [Injecting frames](#injecting-frames)
  - [Jamming](#library-jamming)
  - [RSSI sampling](#rssi-sampling)
  - [Sub-1GHz (page 31)](#sub-1ghz-page-31)
- [Scapy extensions](#scapy-extensions)
- [Firmware recovery (CC1352P7 / CC1354P10)](#firmware-recovery-cc1352p7--cc1354p10)
- [Troubleshooting](#troubleshooting)

---

## Setup

```sh
cd killerbee
python3 -m venv .venv
source .venv/bin/activate
pip install pyserial pyusb scapy pycrypto
export PYTHONPATH="$(pwd)"   # tools/ import killerbee via relative sys.path,
                             # but running them directly still needs this
                             # unless you pip install -e . (see below)
```

`pip install -e .` is also supported via `setup.py`, but its build-time
dependency check is strict about pyUSB versions; if it complains, the
`PYTHONPATH` approach above is the fast path and is exactly what every
example in this guide assumes.

Every tool takes a **device string** (`-i`/`--iface`/`--dev`, e.g. a serial
port path like `/dev/ttyACM0`) and, where the hardware can't be
auto-detected unambiguously, a **hardware type** (`-d`/`--device`, e.g.
`cc1352p7`). Run [`zbid`](#device-discovery) first to find both.

---

## Supported hardware

| `hardware=` string | Device | Bands |
|---|---|---|
| `cc1352p7` | TI CC1352P7 LaunchPad, this project's firmware | 2.4GHz (11-26), sub-1GHz Wi-SUN FSK (page 31, ch 0-128) |
| `cc1354p10` | TI CC1354P10 + standalone LP-XDS110, this project's firmware (primary/default hardware) | 2.4GHz (11-26), sub-1GHz SUN O-QPSK (page 31, ch 0-128) |
| `nrf52840` | Nordic nRF52840, this project's firmware | 2.4GHz |
| `apimote` | GoodFET ApiMote v2 | 2.4GHz |
| `rzusbstick` | Atmel RZUSBSTICK | 2.4GHz |
| `cc2530` / `cc2531` | TI CC2530/CC2531 USB dongle | 2.4GHz |
| `bumblebee` | Bumblebee | 2.4GHz |
| `telosb` | TelosB/Tmote (GoodFET) | 2.4GHz |
| `zigduino` | Zigduino | 2.4GHz |
| `sewio` | Sewio Open-Sniffer (IP-connected, `device` is an IP address) | 2.4GHz |

Both `cc1352p7` and `cc1354p10` enumerate as **two** `/dev/ttyACM*` ports per
board (one debug/CMSIS-DAP, one the actual KillerBee UART) - `zbid` figures
out which is which; see each firmware's README for details if you're
addressing a port directly.

---

## Quick start

```sh
# Find every attached KillerBee-recognized device
python3 tools/zbid

# Sniff channel 11, write to a pcap, stop after 100 packets
python3 tools/zbdump -i /dev/ttyACM0 -d cc1354p10 -c 11 -n 100 -w capture.pcap

# Replay that capture from a second device
python3 tools/zbreplay -i /dev/ttyACM2 -r capture.pcap -c 11 -n 20 -s 0.1
```

---

## CLI tools reference

All tools live in `tools/`. Run any of them with `-h` for the exact,
auto-generated flag list (a few of the older ones predate `argparse` and
print a hand-written usage banner instead - both are shown below,
matching each tool's actual behavior).

### Device discovery

#### `zbid`

Lists every attached KillerBee-recognized device.

```
usage: zbid [-h] [-i INCLUDE] [-g IGNORE]

  -i INCLUDE, --iface INCLUDE, --dev INCLUDE
  -g IGNORE, --gps IGNORE, --ignore IGNORE   ignore a serial device (e.g. an
                                              attached GPS receiver)
```

```sh
python3 tools/zbid
python3 tools/zbid -g /dev/ttyUSB0   # ignore a non-KillerBee serial device
```

### Capture & replay

#### `zbdump`

`tcpdump`-style promiscuous sniffer. Writes libpcap (Wireshark-compatible)
or Daintree SNA.

```
usage: zbdump [-h] [-i DEVSTRING] [-d DEVICE] [-w PCAPFILE] [-W DSNAFILE] [-p]
              [-P PAN_ID_HEX] [-c CHANNEL] [-s SUBGHZ_PAGE] [-n COUNT] [-v]

  -i DEVSTRING, --iface DEVSTRING   (required) serial device path
  -d DEVICE, --device DEVICE        (required) hardware type, e.g. cc1352p7
  -w PCAPFILE, --pcapfile PCAPFILE  write libpcap output
  -W DSNAFILE, --dsnafile DSNAFILE  write Daintree SNA output
  -p, --ppi                         add CACE PPI headers to the pcap
  -P PAN_ID_HEX, --pan_id_hex HEX   only capture frames on this PAN ID
  -c CHANNEL, -f CHANNEL, --channel CHANNEL   (required)
  -s SUBGHZ_PAGE, --subghz_page SUBGHZ_PAGE   page 31 = sub-1GHz
  -n COUNT, --count COUNT           stop after COUNT packets (-1 = forever)
  -v                                verbose
```

```sh
# Sniff 2.4GHz channel 11 indefinitely, Ctrl+C to stop
python3 tools/zbdump -i /dev/ttyACM0 -d cc1354p10 -c 11 -w capture.pcap -n -1

# Sub-1GHz (Wi-SUN/O-QPSK), page 31, channel 9, stop after 50 packets
python3 tools/zbdump -i /dev/ttyACM0 -d cc1354p10 -c 9 -s 31 -n 50 -w subg.pcap

# Only capture one PAN, add PPI headers for Wireshark RSSI display
python3 tools/zbdump -i /dev/ttyACM0 -d cc1354p10 -c 15 -P 0x1a62 -p -w pan.pcap -n -1
```

#### `zbreplay`

Replays frames from a pcap or Daintree SNA capture, verbatim, over the air.
ACK frames in the capture are skipped automatically.

```
usage: zbreplay [-h] [-i DEVSTRING] [-r PCAPFILE] [-R DSNAFILE] [-c CHANNEL]
                [-z SUBGHZ_PAGE] [-n COUNT] [-s SLEEP] [-D]

  -i DEVSTRING, --iface DEVSTRING
  -r PCAPFILE, --pcapfile PCAPFILE
  -R DSNAFILE, --dsnafile DSNAFILE
  -c CHANNEL, -f CHANNEL, --channel CHANNEL   (required)
  -z SUBGHZ_PAGE, --subghz_page SUBGHZ_PAGE
  -n COUNT, --count COUNT           stop after COUNT frames (-1 = all)
  -s SLEEP, --sleep SLEEP           delay between frames, seconds (default 1.0)
  -D                                show device info and exit
```

```sh
# Replay every frame from a capture on channel 11, 100ms between frames
python3 tools/zbreplay -i /dev/ttyACM2 -r capture.pcap -c 11 -s 0.1

# Replay just the first 10 frames
python3 tools/zbreplay -i /dev/ttyACM2 -r capture.pcap -c 11 -n 10 -s 0.05
```

#### `zbstumbler`

Actively discovers ZigBee coordinators/routers by transmitting beacon
request frames while hopping channels, logging responses.

```
usage: zbstumbler [-h] [-i DEVSTRING] [-g IGNORE] [-s DELAY] [-v] [-c CHANNEL]
                  [-w CSVFILE] [-D]

  -s DELAY, --delay DELAY   seconds per channel before hopping
  -c CHANNEL, --channel CHANNEL   restrict to a single channel instead of hopping
  -w CSVFILE, --file CSVFILE   log discovered devices to CSV
  -v, --verbose
  -D                         show device info and exit
```

```sh
# Hop all channels, log results to CSV
python3 tools/zbstumbler -i /dev/ttyACM0 -w discovered.csv -v

# Stay on one channel
python3 tools/zbstumbler -i /dev/ttyACM0 -c 15 -w ch15.csv
```

#### `zbwireshark`

Streams live-sniffed packets to Wireshark via a named pipe, for real-time
analysis instead of capture-then-open.

```
usage: zbwireshark [-h] [-i DEVSTRING] [-p] -c CHANNEL [-s SUBGHZ_PAGE]
                   [-n COUNT] [-D]

  -c CHANNEL, -f CHANNEL, --channel CHANNEL   (required)
  -p, --ppi          include CACE PPI info
  -n COUNT           limit to COUNT packets
```

```sh
python3 tools/zbwireshark -i /dev/ttyACM0 -c 11
# Wireshark opens automatically, showing frames as they arrive
```

### Jamming

#### `zbjammer`

Enables the firmware's jamming mode on a channel (constant-carrier by
default, depending on firmware/hardware). Runs until Ctrl+C.

```
usage: zbjammer [-h] [-i DEVSTRING] [-c CHANNEL] [-z SUBGHZ_PAGE] [-D]

  -c CHANNEL, -f CHANNEL, --channel CHANNEL
  -z SUBGHZ_PAGE, --subghz_page SUBGHZ_PAGE
```

```sh
python3 tools/zbjammer -i /dev/ttyACM2 -c 11
```

#### `jam24_rotate.py` (this fork, CC1352P7)

Rotating constant-carrier jam across a list of 2.4GHz channels, keeping the
jam continuously active across every hop. See
[Firmware recovery](#firmware-recovery-cc1352p7--cc1354p10) if `jammer_on()`
is ever rejected - it means the board's RF command pool needs a JTAG nudge,
almost always from a previous run that didn't exit cleanly.

```
usage: jam24_rotate.py [-h] [-i IFACE] [-d DEVTYPE] [-c CHANNELS]
                       [--dwell DWELL] [--cycles CYCLES] [--duration DURATION]

  -i IFACE       serial device (default: /dev/ttyACM2)
  -d DEVTYPE     hardware type (default: cc1352p7)
  -c CHANNELS    channel spec, e.g. '11,25' or '11-26' (default: 11,25)
  --dwell        seconds per channel before hopping (default: 5.0)
  --cycles       stop after N full passes (default: 0 = unbounded)
  --duration     stop after N seconds total (default: 0 = unbounded)
```

```sh
python3 tools/jam24_rotate.py                              # ch 11,25 forever, Ctrl+C to stop
python3 tools/jam24_rotate.py -c 11,15,20,25 --dwell 2      # more channels, faster hop
python3 tools/jam24_rotate.py --duration 300 --cycles 0     # run for 5 minutes then stop
```

#### `jam24_hop.py` (this fork, CC1352P7 only)

Same jam and channel rotation as `jam24_rotate.py`, but the hop loop runs
entirely on-chip (`KBCapabilities.PHYJAM_HOP`) instead of the host calling
`SET_CHANNEL` once per hop - removes a measured, near-flat ~30ms
per-hop USB/debug-probe control-plane tax that dominates `jam24_rotate.py`'s
`--dwell` below ~30ms regardless of the value chosen. See
firmware/src/kb-cc1352p7/README.md's "On-chip channel-hop jamming" section
for the full measurement and hardware validation (including an independent
RF-energy check confirming genuine on-air hopping, not just command-level
success). Trade-off: no live per-hop log, since the host isn't in the loop.

```
usage: jam24_hop.py [-h] [-i IFACE] [-d DEVTYPE] -c CHANNELS
                     [--dwell DWELL] [--duration DURATION]

  -i IFACE       serial device (default: /dev/ttyACM0)
  -d DEVTYPE     hardware type (default: cc1352p7 - the only one that
                 currently implements this)
  -c CHANNELS    channel spec, e.g. '11,12,13,14,15,20,22,26' or '11-26' (required)
  --dwell        seconds per channel before hopping (default: 0.02 = 20ms;
                 converted to whole milliseconds for the firmware)
  --duration     stop after N seconds total (default: 0 = unbounded, Ctrl+C)
```

```sh
python3 tools/jam24_hop.py -i /dev/cu.usbmodemL45003IW1 -c 11,12,13,14,15,20,22,26 --dwell 0.02
python3 tools/jam24_hop.py -c 11,15,20,25 --dwell 0.05 --duration 30
```

#### `subg_jam.py` (this fork, CC1354P10, sub-1GHz)

Same idea as `jam24_rotate.py` but for the 902-928 MHz band. See
[Sub-1GHz tools](#sub-1ghz-tools-cc1354p10) below.

#### `subg_jam_hop.py` (this fork, CC1354P10, sub-1GHz)

Same jam as `subg_jam.py`, but the hop loop runs entirely on-chip
(`KBCapabilities.PHYJAM_HOP`) instead of the host calling `SET_CHANNEL`
once per hop - same measured ~30ms-per-hop control-plane tax this removes
as `jam24_hop.py` does for 2.4GHz (see that entry above). Sub-1GHz only,
and supports hopping across *both* page 31 (915 MHz) and page 28
(863-876 MHz) in a single run via repeatable `--page-channels
PAGE:CHANNELS`, same syntax `subg_jam.py` uses. See
firmware/src/kb-cc1354p10/README.md's "On-chip channel-hop jamming"
section for the full measurement, cross-page design notes, and hardware
validation (including an independent RF-energy check).

```
usage: subg_jam_hop.py [-h] [-i IFACE] [-d DEVTYPE] --page-channels PAGE:CHANNELS
                        [--dwell DWELL] [--duration DURATION]

  -i IFACE            serial device (default: /dev/ttyACM0)
  -d DEVTYPE          hardware type (default: cc1354p10)
  --page-channels     'PAGE:CHANNELS' (e.g. '31:9,14,15,19,20,24,106'),
                       repeatable - one per page (28 and/or 31) (required)
  --dwell             seconds per channel before hopping (default: 0.02 = 20ms)
  --duration          stop after N seconds total (default: 0 = unbounded, Ctrl+C)
```

```sh
python3 tools/subg_jam_hop.py -i /dev/cu.usbmodemLS4501DC1 --dwell 0.02 \
    --page-channels 31:9,14,15,19,20,24,106 \
    --page-channels 28:10,12,20,41,51,57
```

### Attack / flood tools

These target a specific, already-identified network (PAN ID, coordinator
address, etc. - get these from `zbstumbler` or a capture first).

#### `zbassocflood`

Floods a target network with association request frames.

```
Usage: zbassocflood [-pcDis] [-i devnumstring] [-p PANID] [-c channel]
                     [-s per-packet delay/float]

e.g.: zbassocflood -p 0xBAAD -c 11 -s 0.1
```

```sh
python3 tools/zbassocflood -i /dev/ttyACM0 -p 0xBAAD -c 11 -s 0.1
```

#### `zbfakebeacon`

Impersonates a coordinator by transmitting fake beacon frames (either
continuously, or only in response to beacon requests).

```
usage: zbfakebeacon [-h] -f CHANNEL [-i DEVSTRING] -p PANID -e EPANID
                    [-s COORDINATOR] [-g] [-r RATE] [--numloops NUMLOOPS]

  -p PANID, --panid PANID
  -e EPANID, --epanid EPANID   extended PAN ID
  -s COORDINATOR                coordinator short address
  -g, --go                      spam beacons unconditionally instead of
                                 waiting for beacon requests
  -r RATE, --rate RATE          packets/sec when using -g
```

```sh
python3 tools/zbfakebeacon -f 11 -i /dev/ttyACM0 -p 0xBAAD -e 0x1122334455667788 -g -r 5
```

#### `zbpanidconflictflood`

Sends PAN ID conflict notifications to force a target coordinator into a
PAN ID change.

```
usage: zbpanidconflictflood [-h] -f CHANNEL [-i DEVSTRING]
                            [-l LISTENINTERFACE] -p PANID -e EPANID -s
                            COORDINATOR [-w SLEEP]
```

```sh
python3 tools/zbpanidconflictflood -f 11 -i /dev/ttyACM0 -p 0xBAAD -e 0x1122334455667788 -s 0x0000
```

#### `zborphannotify`

Sends orphan notifications / disassociation requests to knock a device off
a network.

```
usage: zborphannotify [-h] -f CHANNEL [-i DEVSTRING] -p PANID -s COORDINATOR
                      -d DEVICE [-q SRCSEQ] [--devleave] [--zblayer]
                      [--numloops NUMLOOPS]

  --devleave   pretend to be an end device requesting to leave
  --zblayer    send a ZigBee NWK-layer disassociation instead of 802.15.4 MAC
```

```sh
python3 tools/zborphannotify -f 11 -i /dev/ttyACM0 -p 0xBAAD -s 0x0000 -d 0x1122334455667788
```

#### `zbrealign`

Sends a coordinator realignment frame to move a target PAN to a new
channel/PAN ID.

```
usage: zbrealign [-h] -f CHANNEL [-z SUBGHZ_PAGE] [-i DEVSTRING] -p PANID -s
                 COORDINATOR -l LONGCOORD -d DEVICE -e DEVICESHORT [-q SRCSEQ]
                 [-b SRCSEQZBNWK] --newchannel NEWCHANNEL --newpanid NEWPANID
                 [--numloops NUMLOOPS]
```

```sh
python3 tools/zbrealign -f 11 -i /dev/ttyACM0 -p 0xBAAD -s 0x0000 \
    -l 0x1122334455667788 -d 0x8877665544332211 -e 0x0001 \
    --newchannel 15 --newpanid 0xCAFE
```

#### `zbcat`

Sends a single, fully custom-crafted 802.15.4 data frame.

```
usage: zbcat [-h] -f CHANNEL [-i DEVSTRING] -p PANID -s SOURCE -d DEST
             [-e EXTSOURCE] --data DATA [-a] [-l] [-n SEQNUM] [-r]
             [--numloops NUMLOOPS]

  -a, --ack           request ACK
  -l, --shortaddress  use short addressing
  -r, --respond       wait for and print a response
```

```sh
python3 tools/zbcat -f 11 -i /dev/ttyACM0 -p 0xBAAD -s 0x0001 -d 0x0000 --data deadbeef
```

### Key & crypto tools

#### `zbkey`

Attempts to recover a network key by sending an association request
followed by a data request, hoping the coordinator responds with a
key-transport frame.

```
Usage: ./zbkey -f [channel] -s 0.1 -p [PANID] -a [IEEE64bitaddress] -i [deviceid]
```

```sh
python3 tools/zbkey -f 14 -s 0.1 -p aa1a -a 0fc8071c08c25100 -i /dev/ttyACM0
```

#### `zbdsniff`

Extracts a plaintext ZigBee network key from a capture file (or a whole
directory of them), if one was transported in the clear.

```
Options:
  -f FILE, --file=FILE       one pcap file
  -d DIR, --dir=DIR          a directory of pcap files
  -k TRANSPORTKEY, --transport-key=KEY   known transport key for decryption
  -v, --verbose
```

```sh
python3 tools/zbdsniff -f capture.pcap -v
python3 tools/zbdsniff -d ./captures/ -k 5a6967426565416c6c69616e63653039
```

#### `zbgoodfind`

Given a captured encrypted frame and a binary file of candidate key
material (e.g. an extracted firmware image), searches for the byte sequence
that decrypts it correctly - i.e. finds the network key hiding in a
firmware dump.

```
usage: zbgoodfind [-h] [-F] (-R DAINTREE | -r PCAP | -d) [-f BINARY_FILE] [-v]

  -F, --skip-fcs         don't skip the 2-byte FCS at the end of each frame
  -r PCAP                pcap file with the encrypted frame(s)
  -R DAINTREE            Daintree SNA file instead
  -f BINARY_FILE         binary file to search (e.g. a firmware dump)
  -d, --test             generate a synthetic test binary instead of searching
```

```sh
python3 tools/zbgoodfind -r captured_encrypted.pcap -f firmware_dump.bin -v
```

### File conversion & analysis

#### `zbconvert`

Converts between Daintree SNA and libpcap formats (timestamps are not
preserved).

```
usage: zbconvert [-h] -i INFILE -o OUTFILE [-n] [-c COUNT]

  -n, --noclobber   don't overwrite an existing OUTFILE
  -c COUNT          only convert the first COUNT packets
```

```sh
python3 tools/zbconvert -i capture.dcf -o capture.pcap
python3 tools/zbconvert -i capture.pcap -o capture.dcf -n
```

#### `parse_pcaps.sh`

Batch-runs `tshark` (or similar) over one pcap file or every `.pcap` in a
directory.

```
Usage: ./parse_pcaps.sh [file_or_dir]
```

```sh
./tools/parse_pcaps.sh captures/single.pcap
./tools/parse_pcaps.sh captures/         # every .pcap in the directory
```

### Interactive scapy shell

#### `zbscapy`

Launches an interactive Scapy REPL with KillerBee's 802.15.4/ZigBee layers
and helper functions (`kbsniff`, `kbsendp`, `kbdecrypt`, ...) preloaded -
see [Scapy extensions](#scapy-extensions) for what's available inside it.

```
Usage: zbscapy [-c new_startup_file] [-p new_prestart_file] [-C] [-P] [-H]

  -H   header-less start
  -C   do not read the startup file
  -P   do not read the pre-startup file
```

```sh
python3 tools/zbscapy
>>> kbdev()
>>> pkts = kbsniff(iface="/dev/ttyACM0", channel=11, count=20)
```

### RSSI detection (this fork, CC1354P10 + CC1352P7, every page)

#### `rssi_scan.py`

Ambient RSSI scanner across every KillerBee page either firmware
supports - page 0 (2.4GHz), page 31 (915 MHz US ISM), page 28 (863-876
MHz EU/UK) - on both the CC1354P10 and CC1352P7 (`-d`), since both now
configure the exact same three pages/channel plans. RSSI only, no packet
capture/pcap - built for "is something transmitting on this channel right
now" (e.g. verifying a hop jammer running on a *different* board), not
for capturing/decoding traffic; use `scan24.py`/`subg_scan.py` for that.
Flags channels whose peak RSSI stands out well above that page's own
median as likely carrying a real signal. See the script's own module
docstring for the dwell-vs-hop-cycle aliasing caveat and the
RF-core-stuck troubleshooting note.

```
usage: rssi_scan.py [-h] -i IFACE [-d DEVTYPE] [--pages PAGES] [-c CHANNELS]
                     [-t DWELL] [--poll-interval POLL_INTERVAL]
                     [--threshold THRESHOLD] [-o OUTDIR]

  -i IFACE          serial device (required)
  -d DEVTYPE        cc1354p10 (default) or cc1352p7
  --pages           comma list of pages to scan (default: 0,28,31 - all of them)
  -c CHANNELS       restrict to these channels, clipped per page (default: full range)
  -t DWELL          seconds per channel (default: 0.5)
  --poll-interval   seconds between GET_RSSI polls within a channel (default: 0.05 -
                     keep this nonzero, see the script's module docstring)
  --threshold       dB above a page's own median to flag as a likely signal (default: 15.0)
  -o OUTDIR         also write a JSON report here
```

```sh
python3 tools/rssi_scan.py -i /dev/cu.usbmodemL45003IW1 -d cc1352p7
python3 tools/rssi_scan.py -i /dev/cu.usbmodemLS4501DC1 --pages 28 -c 9-65 -t 3
```

### Sub-1GHz tools (CC1354P10)

These target the CC1354P10's 902-928 MHz page-31 band (SUN O-QPSK Rate
Mode 0, channels 0-128, `902.2 + 0.2*channel` MHz). See
[firmware/src/kb-cc1354p10/README.md](firmware/src/kb-cc1354p10/README.md)'s
"Sub-1GHz support" section for the channel-plan history/caveats.

#### `subg_scan.py`

Scans a channel list, writes a per-channel pcap for any channel that saw
traffic, a text report, and a JSON results file. Also samples ambient RSSI
per channel independent of packet capture.

```
usage: subg_scan.py [-h] [-i IFACE] [-d DEVTYPE] [-t DWELL] [-c CHANNELS]
                    [-o OUTDIR] [--rssi-interval RSSI_INTERVAL] [--no-rssi]

  -t DWELL         seconds per channel (default varies, check -h)
  -c CHANNELS      channel spec (default: 0-9)
  -o OUTDIR        output directory
  --rssi-interval  seconds between ambient RSSI samples
  --no-rssi        skip RSSI sampling entirely
```

```sh
python3 tools/subg_scan.py -c 0-9 -t 10 -o ./subg_scan_results
```

#### `subg_track.py`

Fast repeated sweep across a channel list, locking onto and doing a
focused listen on any channel showing a steady (not just single-packet)
source, before reporting it. (Honestly framed in its own `-h`: this is
sweep-and-confirm, not true synchronized frequency-hop following - no
single half-duplex receiver can do that from RX alone.)

```
usage: subg_track.py [-h] [-i IFACE] [-d DEVTYPE] [-c CHANNELS]
                     [--sweep-dwell SWEEP_DWELL] [--confirm-min CONFIRM_MIN]
                     [--confirm-window CONFIRM_WINDOW]
                     [--quiet-timeout QUIET_TIMEOUT]
                     [--confirmed-quiet-timeout CONFIRMED_QUIET_TIMEOUT]
                     [--max-runtime MAX_RUNTIME] [-o OUTDIR]
```

```sh
python3 tools/subg_track.py -c 0-9 --sweep-dwell 0.2 --max-runtime 120 -o ./tracked
```

#### `subg_jam.py`

Rotating constant-carrier jam across sub-1GHz channels, keeping the jam
active across every hop.

```
usage: subg_jam.py [-h] [-i IFACE] [-d DEVTYPE] -c CHANNELS [--dwell DWELL]
                   [--cycles CYCLES] [--duration DURATION]
```

```sh
python3 tools/subg_jam.py -c 9,14,15,19,20,24,106 --dwell 2
python3 tools/subg_jam.py -c 0-9 --dwell 1 --duration 60
```

### Bootloader (RZUSBSTICK only)

#### `kbbootloader`

No arguments - auto-detects the device, puts it into bootloader mode, and
prints the bootloader version/signature/identifier. RZUSBSTICK-specific.

```sh
python3 tools/kbbootloader
```

### Currently broken in this fork

`zbwardrive` and `zbopenear` both fail with `ModuleNotFoundError`
(`killerbee.zbwardrive` / `killerbee.openear` don't exist in this tree -
GPS-linked wardriving and the ncurses "openear" waterfall display haven't
been ported forward). Don't rely on them without fixing the import first.

---

## Python library usage

Every tool above is a thin wrapper around the `killerbee` package. Anything
they can do, you can do directly - and directly is usually the right call
for anything beyond a one-off capture, since you get real control over
timing, filtering, and error handling.

### Library device discovery

```python
from killerbee import KillerBee, kbutils

# Auto-detect the first supported device
kb = KillerBee()

# Or address a specific device explicitly (recommended once you have >1
# board attached - auto-detect picks whichever the enumeration order finds
# first)
kb = KillerBee(device="/dev/ttyACM0", hardware="cc1354p10")

print(kb.get_dev_info())   # [device string, product string, serial number]
kb.close()

# List every attached device without opening one (what `zbid` does)
for dev, product, serial in kbutils.devlist():
    print(dev, product, serial)
```

### Capability checking

```python
from killerbee import KillerBee, KBCapabilities

kb = KillerBee(device="/dev/ttyACM2", hardware="cc1352p7")

if not kb.check_capability(KBCapabilities.PHYJAM):
    raise SystemExit("this device can't jam")

print(kb.get_capabilities())   # every capability this device reports
```

### Sniffing

```python
from killerbee import KillerBee

kb = KillerBee(device="/dev/ttyACM0", hardware="cc1354p10")
kb.set_channel(11)
kb.sniffer_on()

for _ in range(50):
    pkt = kb.pnext(timeout=1)   # seconds; None if nothing arrived in time
    if pkt is None:
        continue
    print(pkt['bytes'].hex(), "rssi=", pkt['rssi'], "crc_ok=", pkt['validcrc'])

kb.sniffer_off()
kb.close()
```

Writing straight to a pcap file (what `zbdump` does under the hood):

```python
from killerbee import KillerBee, PcapDumper, DLT_IEEE802_15_4

kb = KillerBee(device="/dev/ttyACM0", hardware="cc1354p10")
kb.set_channel(11)
kb.sniffer_on()

dumper = PcapDumper(DLT_IEEE802_15_4, "capture.pcap")
for _ in range(100):
    pkt = kb.pnext(timeout=1)
    if pkt is not None:
        dumper.pcap_dump(pkt['bytes'], ant_dbm=pkt['rssi'], freq_mhz=2405 + 5 * (11 - 11))
dumper.close()
kb.sniffer_off()
kb.close()
```

### Injecting frames

```python
from killerbee import KillerBee

kb = KillerBee(device="/dev/ttyACM2", hardware="cc1352p7")
kb.set_channel(11)

# Minimal 802.15.4 data frame: FCF (data, short addressing), seq,
# dest PAN 0xffff (broadcast), dest addr 0xffff (broadcast), payload
frame = bytes([0x01, 0x88, 0x00, 0xff, 0xff, 0xff, 0xff]) + b"hello"
kb.inject(frame)                       # single frame, hardware appends FCS
kb.inject(frame, count=10, delay=0.1)  # 10 frames, 100ms apart

kb.close()
```

Replaying a captured frame verbatim (what `zbreplay` does, minus the
ACK-frame filtering it applies automatically):

```python
from killerbee import KillerBee, PcapReader

kb = KillerBee(device="/dev/ttyACM2", hardware="cc1352p7")
kb.set_channel(11)

cap = PcapReader("capture.pcap")   # no context-manager support, close() explicitly
while True:
    frame = cap.pnext()
    if frame is None:
        break
    kb.inject(frame[1][:-2])   # strip the captured FCS; hardware re-appends its own
    import time; time.sleep(0.1)
cap.close()

kb.close()
```

### Library jamming

```python
from killerbee import KillerBee
import time

kb = KillerBee(device="/dev/ttyACM2", hardware="cc1352p7")

kb.jammer_on(channel=11, method="constant")   # or method="reflexive"
time.sleep(10)
kb.jammer_off()

kb.close()
```

Rotating across channels while jamming stays active (what
`jam24_rotate.py`/`subg_jam.py` do):

```python
from killerbee import KillerBee
import time

kb = KillerBee(device="/dev/ttyACM2", hardware="cc1352p7")
channels = [11, 25]

kb.driver.jammer_on(channel=channels[0], method="constant")
for ch in channels[1:] + channels[:1]:   # keep cycling
    time.sleep(5)
    kb.set_channel(ch)   # firmware restarts the jam on the new channel automatically

kb.jammer_off()
kb.close()
```

### RSSI sampling

Ambient channel energy, independent of packet capture (requires an active
sniffer - see each firmware README's `GET_RSSI` notes):

```python
from killerbee import KillerBee
import time

kb = KillerBee(device="/dev/ttyACM2", hardware="cc1352p7")
kb.set_channel(11)
kb.sniffer_on()
time.sleep(0.3)   # let the RX operation settle before the first sample

for _ in range(10):
    rssi = kb.driver.get_rssi()   # None if no RX operation is active
    print(rssi, "dBm")
    time.sleep(1)

kb.sniffer_off()
kb.close()
```

### Sub-1GHz (page 31)

Both `cc1352p7` and `cc1354p10` support the 902-928 MHz band as KillerBee
"page 31". Same API, just pass `page=31` and a channel 0-128:

```python
from killerbee import KillerBee

kb = KillerBee(device="/dev/ttyACM0", hardware="cc1354p10")
kb.set_channel(9, page=31)   # 902.2 + 0.2*9 = 904.0 MHz
kb.sniffer_on()

pkt = kb.pnext(timeout=2)
if pkt is not None:
    print(pkt['bytes'].hex())

kb.sniffer_off()
kb.close()
```

---

## Scapy extensions

`from killerbee.scapy_extensions import *` (or just run `zbscapy`, which
does this for you) adds KillerBee-aware helpers on top of Scapy's normal
802.15.4/ZigBee layers:

| Function | Purpose |
|---|---|
| `kbdev()` | Print attached device list |
| `kbsendp(pkt, channel=..., iface=..., count=..., inter=...)` | Send a Scapy packet over the air |
| `kbsniff(channel=..., count=..., iface=..., prn=..., timeout=...)` | Sniff into a Scapy `PacketList` |
| `kbsrp(pkt, ...)` / `kbsrp1(pkt, ...)` | Send and receive (all responses / first response) |
| `kbrdpcap(filename)` / `kbwrpcap(filename, pkts)` | Read/write pcap as Scapy packets |
| `kbrddain(filename)` / `kbwrdain(filename, pkts)` | Read/write Daintree SNA |
| `kbdecrypt(pkt, key=...)` / `kbencrypt(pkt, data, key=...)` | ZigBee APS/NWK frame crypto |
| `kbkeysearch(pkt, searchdata, ispath=True)` | Search a file for the key that decrypts `pkt` |
| `kbgetnetworkkey(pkts)` | Pull a network key out of a capture, if transported in the clear |
| `kbgetpanid(pkt)` | Extract the PAN ID from a packet |
| `kbrandmac(length=8)` | Generate a random hardware address |
| `kbtshark(*args)` | Shell out to tshark |

Example - sniff 20 frames, then decrypt one with a known key:

```python
from killerbee.scapy_extensions import kbsniff, kbdecrypt

pkts = kbsniff(iface="/dev/ttyACM0", channel=15, count=20)
plaintext, mic_ok = kbdecrypt(pkts[0], key="5a6967426565416c6c69616e63653039")
print(plaintext.hex(), "MIC valid:", mic_ok)
```

---

## Firmware recovery (CC1352P7 / CC1354P10)

Both custom firmwares can end up in a state where a command is rejected
(`STATUS_ERROR`) or the UART stops responding entirely - almost always
because a previous script run didn't exit cleanly (hard-killed process,
closed terminal, a bug) and left the RF core holding a command it never
got told to release. This is **not** a bug you need to work around in your
own scripts; it's a documented hardware/SDK characteristic (see each
firmware's README, "RF core can get stuck after heavy use"). The fix is a
JTAG-level nudge through the same debug probe you'd use to flash it -
**no physical unplug needed** for this class of fault:

```sh
# CC1352P7
/opt/ti/uniflash_sl/dslite.sh --mode memory \
    -c firmware/src/kb-cc1352p7/CC1352P7_XDS110.ccxml -r 0x0,4 -o /tmp/discard.bin -e

# CC1354P10
/opt/ti/uniflash_sl/dslite.sh --mode memory \
    -c firmware/src/kb-cc1354p10/CC1354P10_XDS110.ccxml -r 0x0,4 -o /tmp/discard.bin -e
```

This is a side-effect-free memory read whose only real purpose is forcing
DSLite to connect and run its GEL board-reset script - any DSLite operation
that connects works, this one's just minimal. Note the `--mode memory` flag
specifically: `dslite.sh` selects its mode via `--mode <name>`, not a bare
positional argument.

If instead **the debug probe itself** stops responding (`DSLite`/`xds110reset`
failing with `Error -261`/`Error -260` even for an unrelated no-op like
`--help`), that's the XDS110's own onboard firmware wedged, not the
target chip - a USB bus reset and DFU-mode toggling do **not** clear this in
practice. A real physical unplug/replug of the probe's USB cable is the
documented fix; the board's own physical reset button only resets the
target chip and won't help here.

---

## Troubleshooting

- **`ModuleNotFoundError: No module named 'killerbee'`** running a tool
  directly - set `export PYTHONPATH=/path/to/killerbee` (see
  [Setup](#setup)); the tools' `sys.path` tricks assume they're being run
  from a normal install, not this fork's plain-checkout layout.
- **`Device rejected inject()` / `sniffer_on` / `jammer_on()`** on
  CC1352P7/CC1354P10 - see [Firmware recovery](#firmware-recovery-cc1352p7--cc1354p10)
  above.
- **`zbwardrive` / `zbopenear` crash on import** - not ported forward in
  this fork; see [Currently broken](#currently-broken-in-this-fork).
- **`pip install -e .` fails a pyUSB version check** - use the
  `PYTHONPATH` approach in [Setup](#setup) instead; it's what every
  example here uses.
- **Two `/dev/ttyACM*` ports per CC1352P7/CC1354P10 board, one doesn't
  respond** - expected, one is the debug/CMSIS-DAP port. `zbid` finds the
  right one automatically.
