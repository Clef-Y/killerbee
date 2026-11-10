# Build Your Own KillerBee TI Board

A reproducible recipe for setting up a **CC1354P10** and/or **CC1352P7** as
a KillerBee device with this fork's firmware - so you can go from a bare TI
LaunchPad to a working board that runs the same capture / scan / RSSI (and,
for authorized use, jam) tooling this project provides.

This page is the "share it with others" overview. It ties together the
docs that already exist in this repo rather than duplicating them.

---

## What you need

**Hardware (one or both):**

| Board | Notes |
|---|---|
| TI **CC1354P10** + standalone **LP-XDS110** debug probe | Primary/default hardware for this fork. |
| TI **CC1352P7** LaunchPad | Has an onboard XDS110 - single USB cable. |

**Software:**

- **TI UniFlash** (free, from TI) - to flash the prebuilt firmware. You
  download this directly from TI; it is **not** redistributed in this repo
  (it's TI-proprietary). See [TI_SDK_SETUP_DEBIAN.md](TI_SDK_SETUP_DEBIAN.md)
  / [TI_SDK_SETUP_MACOS.md](TI_SDK_SETUP_MACOS.md).
- **Python 3 + the KillerBee host tools** - see
  [USAGE_GUIDE.md](USAGE_GUIDE.md#setup) (Linux) or
  [MACOS_SETUP.md](MACOS_SETUP.md) (macOS).

You only need the full TI **SDK + compiler** if you want to *rebuild* the
firmware from source. To just flash the prebuilt `.hex` files shipped here,
UniFlash alone is enough.

---

## Step 1 - Flash the prebuilt firmware

Prebuilt firmware images live in the repo:

```
firmware/src/kb-cc1354p10/tirtos7/ticlang/kb_cc1354p10.hex
firmware/src/kb-cc1352p7/tirtos7/ticlang/kb_cc1352p7.hex
```

Flash with UniFlash's `dslite.sh`, using the per-board target config
checked in alongside each firmware (correct JTAG mode already set):

```sh
# CC1354P10 (standalone LP-XDS110, standard JTAG)
<uniflash>/dslite.sh -c firmware/src/kb-cc1354p10/CC1354P10_XDS110.ccxml \
    firmware/src/kb-cc1354p10/tirtos7/ticlang/kb_cc1354p10.hex

# CC1352P7 (onboard XDS110, cJTAG)
<uniflash>/dslite.sh -c firmware/src/kb-cc1352p7/CC1352P7_XDS110.ccxml \
    firmware/src/kb-cc1352p7/tirtos7/ticlang/kb_cc1352p7.hex
```

Replace `<uniflash>` with your UniFlash path (`/opt/ti/uniflash_sl` on
Linux, `/Applications/ti/uniflash_<ver>` on macOS). `dslite.sh` defaults to
flash mode - don't add a `flash` argument. Full details, per-OS paths, and
the Apple-Silicon Rosetta 2 note are in the TI SDK setup docs linked above.

*(Rebuilding from source instead? See each firmware's own README:
[kb-cc1354p10](firmware/src/kb-cc1354p10/README.md),
[kb-cc1352p7](firmware/src/kb-cc1352p7/README.md).)*

---

## Step 2 - Set up the host tools

Follow [USAGE_GUIDE.md § Setup](USAGE_GUIDE.md#setup) (or
[MACOS_SETUP.md](MACOS_SETUP.md) on a Mac): create the Python venv, install
`pyserial pyusb scapy`, and set `PYTHONPATH`.

---

## Step 3 - Verify the board

```sh
python3 tools/zbid                               # should list your board(s)
python3 tools/rssi_scan.py -i <port> -d <board>  # passive RF scan, receive-only
```

On macOS you can also use the GUI: [macapp/](macapp/README.md) -
`python3 macapp/kb_toolkit.py` gives a window for device discovery and RSSI
scanning.

See [USAGE_GUIDE.md](USAGE_GUIDE.md) for every tool and the Python library
API.

---

## Making a shareable bundle

`scripts/make_release.sh` assembles a zip containing the prebuilt firmware,
the `.ccxml` flash configs, the Python host tools, the `killerbee` package,
and the docs - everything a recipient needs **except** TI's own tools,
which they download from TI (licensing). Run it from the repo root:

```sh
scripts/make_release.sh
# -> dist/killerbee-ti-boards-<date>.zip
```

---

## A note on the jamming tools

This project includes RF jamming tools (`tools/subg_jam_hop.py`,
`tools/jam24_rotate.py`, etc.). Jammers transmit a continuous carrier that
denies a radio channel, and **operating one is illegal in many
jurisdictions** (in the US, the FCC prohibits operating, marketing, or
selling jammers outright). They are included for **supervised security
research and authorized testing on equipment and spectrum you own or are
explicitly permitted to test** - e.g. a shielded/anechoic setup or a
licensed test range. They are command-line only and are deliberately not
part of the GUI app. Only use them where you are legally authorized to
transmit. You are responsible for compliance with local law.
