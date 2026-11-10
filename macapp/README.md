# KillerBee Toolkit - macOS GUI (receive-only)

A small Tkinter GUI front-end for the two **receive-only** helper tools in
this repo:

- **Devices** - list attached CC1354P10 / CC1352P7 boards (`zbid`).
- **RSSI Scan** - passive ambient RF-energy monitoring across pages 0
  (2.4GHz), 28 and 31 (sub-1GHz) (`tools/rssi_scan.py`), with live output.

The GUI never transmits. It shells out to the existing CLI tools with the
same Python interpreter, so behavior matches the command line exactly.

## Why no jamming in the GUI

This app intentionally does **not** wrap the jamming tools
(`subg_jam_hop.py`, `jam24_rotate.py`). Those transmit a continuous
carrier that denies a radio channel - a DoS transmitter. RF jammers are
illegal to *operate* in many jurisdictions (in the US the FCC prohibits
operating, marketing, or selling them, with no personal-use exception), so
a one-click, broadly-shareable "jam" button is not something to ship in a
convenience app. The jam scripts remain available on the command line for
supervised, authorized testing by people who understand the legal
constraints - that's a deliberate bit of friction, not an oversight.

## Run it (simplest - from the repo)

```sh
cd killerbee
source .venv/bin/activate          # the host-side venv from MACOS_SETUP.md
python3 macapp/kb_toolkit.py
```

Tkinter ships with Python, so there's no extra GUI dependency. The host
setup in [../MACOS_SETUP.md](../MACOS_SETUP.md) (libusb, pyserial, pyusb,
scapy) is what the underlying tools need.

## Build a double-clickable .app (optional)

To hand someone a `.app` they can run without a terminal, bundle with
[PyInstaller](https://pyinstaller.org):

```sh
source .venv/bin/activate
pip install pyinstaller
cd macapp
pyinstaller --windowed --name "KillerBee Toolkit" \
    --collect-submodules killerbee \
    --paths .. \
    kb_toolkit.py
# -> dist/KillerBee Toolkit.app
```

Notes and caveats (be honest with whoever you share it with):

- The `.app` still needs the boards' USB drivers and `libusb` present on
  the target Mac - see [../MACOS_SETUP.md](../MACOS_SETUP.md). PyInstaller
  bundles the Python code, not system USB libraries.
- **Apple Silicon:** build on the same CPU architecture you'll distribute
  to, or build a universal2 Python environment; an x86_64 bundle needs
  Rosetta 2 on Apple Silicon.
- **Gatekeeper:** an unsigned `.app` triggers "cannot be opened because the
  developer cannot be verified." Recipients approve it in System Settings →
  Privacy & Security, or you code-sign/notarize it with an Apple Developer
  ID if you have one.
- This GUI has not been visually regression-tested as a bundled `.app` in
  this repo's CI; treat the PyInstaller step as a starting recipe and test
  the bundle on a clean Mac before distributing.
