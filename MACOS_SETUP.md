# macOS Setup Guide (CC1354P10 / CC1352P7)

A practical setup guide for running this KillerBee fork's Python library
and CLI tools (`tools/zbid`, `tools/zbdump`, `tools/jam24_rotate.py`,
`tools/scan24.py`, etc.) against the CC1354P10 and CC1352P7 LaunchPad
boards from macOS. Written and verified against the Linux setup used to
develop and test this fork - the package list and version behavior below
comes from that actual working environment (see the exact `pip list` in
[Verifying against a known-good baseline](#verifying-against-a-known-good-baseline)),
not generic advice.

This covers the **runtime path** (talking to already-flashed boards over
USB serial) as the main goal. Building or reflashing the custom firmware
itself needs TI's full SimpleLink toolchain, which is a much bigger,
separate install - see [Optional: firmware build/flash toolchain](#optional-firmware-buildflash-toolchain-only-if-youll-build-or-reflash)
if you'll need that too.

> One honest caveat up front: this guide is written from a Linux
> development environment, cross-checked against known macOS/IOKit/pip
> behavior. I don't have a real macOS 26.2 machine to test on directly -
> the core mechanics here (Python packaging, USB CDC-ACM serial, libusb)
> are mature and stable across macOS versions, but if any exact command
> or path has moved, treat this as the strong starting point it is rather
> than a guarantee, and let me know what needed adjusting.

---

## 1. Install Homebrew (if you don't have it)

```sh
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

## 2. Install Python 3 and libusb

```sh
brew install python@3.12 libusb
```

- **`libusb`** is required even though these two boards only need plain
  USB-serial (CDC-ACM) - `pyusb` is imported unconditionally at the top of
  `killerbee/kbutils.py`, so the whole package fails to import without a
  working libusb backend, regardless of which specific device you're
  actually using.
- Use whichever recent Python 3 you like (3.10-3.12 all work fine with
  this project); just be consistent about which `python3` you use for the
  venv below.

## 3. Clone and set up the virtualenv

```sh
git clone <this-repo-url> killerbee
cd killerbee
python3 -m venv .venv
source .venv/bin/activate
```

## 4. Install Python dependencies

```sh
pip install pyserial pyusb scapy pycryptodome
```

One deliberate substitution from what you might see referenced elsewhere
in KillerBee's own docs/history: **`pycryptodome`, not `pycrypto`.**
`killerbee/dot154decode.py` does `from Crypto.Cipher import AES` - the
classic `pycrypto` package providing that `Crypto.*` namespace has been
unmaintained since ~2013 and is a common, frustrating build failure on
modern macOS (missing headers, deprecated `Python.h` assumptions, no
compatible wheels). `pycryptodome` is the maintained, drop-in replacement
that installs the same `Crypto.*` import namespace via a normal `pip
install` with prebuilt wheels - no compiler needed, no code changes
needed anywhere in this project.

## 5. Set `PYTHONPATH` instead of `pip install -e .`

```sh
export PYTHONPATH="$(pwd)"
```

Add that to your shell profile (`~/.zshrc` on any current macOS, since
`zsh` is the default shell) if you don't want to re-export it every
session:

```sh
echo 'export PYTHONPATH="'"$(pwd)"'"' >> ~/.zshrc
```

`setup.py`'s `pip install -e .` path runs its own pyUSB version probe at
build time that's easy to trip even with a perfectly good pyUSB 1.x
install (it's stricter than the actual runtime requirement) - the
`PYTHONPATH` approach above is what every example in
[`USAGE_GUIDE.md`](USAGE_GUIDE.md) assumes, and is the faster, more
reliable path on any OS, macOS included.

## 6. Plug in the boards - no driver install needed for normal use

Both the CC1354P10 (behind a standalone LP-XDS110 probe) and the CC1352P7
LaunchPad (onboard XDS110) expose their KillerBee-firmware UART backchannel
as a standard **USB CDC-ACM** serial port - a generic, driver-less device
class every modern OS (macOS included) has supported natively for years.
Plug a board in and it should appear as:

```sh
ls /dev/cu.usbmodem*
```

Each board enumerates as **two** ports (matching this project's own
`/dev/ttyACM0`/`ACM1` pairing on Linux) - one is the actual KillerBee UART,
the other is the XDS110's own debug/CMSIS-DAP channel and won't respond to
`zbid`/KillerBee at all. `zbid` figures out which is which automatically:

```sh
python3 tools/zbid
```

**Unlike Linux, macOS does not use a `dialout`-group permission model** -
the logged-in user typically has read/write access to `/dev/cu.usbmodem*`
devices immediately on plug-in, with no group membership or udev-rule
equivalent to configure. If a port doesn't appear at all (not a permission
error, just nothing in `/dev/cu.usbmodem*`), that's the one case worth
installing TI's official XDS110 driver package for - search "XDS110
drivers" on TI's site, or install alongside UniFlash/CCS (see below), which
bundles it.

If macOS shows a "System Extension Blocked" or similar prompt the first
time a TI driver package tries to load, approve it under **System
Settings → Privacy & Security** - this is standard macOS gatekeeping for
any third-party kernel/system extension, not specific to TI's driver.

## 7. Verify it works

```sh
source .venv/bin/activate
export PYTHONPATH="$(pwd)"

python3 tools/zbid
# expect to see both boards' KillerBee UART ports listed as
# "TI CC1354P10" / "TI CC1352P7"

python3 tools/zbdump -i /dev/cu.usbmodemXXXX -d cc1354p10 -c 11 -n 20 -w test.pcap
```

(Substitute the actual port `zbid` reported - the exact `usbmodemNNNN`
number is assigned by macOS per-port and isn't predictable in advance.)

---

## Optional: firmware build/flash toolchain (only if you'll build or reflash)

Everything above is enough to **use** already-flashed boards. If you also
want to build this project's custom firmware from source, or recover a
board that's gotten into a stuck RF-core state (see
[USAGE_GUIDE.md's Firmware recovery section](USAGE_GUIDE.md#firmware-recovery-cc1352p7--cc1354p10)),
you need TI's SimpleLink toolchain instead - a much larger, separate
install:

- **TI UniFlash** (standalone) - provides `dslite.sh` and the `DSLite`
  debug-server binary this project's firmware recovery/flash commands use.
  TI publishes a macOS build; get it from TI's UniFlash download page
  directly rather than a path I'd have to guess at for a specific macOS
  version.
- **SimpleLink CC13xx/CC26xx SDK** - the device headers, RF driver source,
  and `ti154stack`/`prop_rf` reference examples this project's firmware
  builds against.
- **TI CLANG-based ARM compiler (ticlang)** and **SysConfig** - the actual
  build toolchain; SysConfig also regenerates the radio/pin configuration
  headers from each firmware's `.syscfg` file.

All three are normally installed together via **Code Composer Studio
(CCS)**'s installer, which handles the toolchain/SDK/SysConfig bundling
for you - that's the simplest path if you're setting this up from scratch,
rather than assembling the pieces UniFlash-standalone-style as this
Linux environment does. Once installed, the firmware build/flash commands
in each firmware's own README
([`firmware/src/kb-cc1352p7/README.md`](firmware/src/kb-cc1352p7/README.md),
[`firmware/src/kb-cc1354p10/README.md`](firmware/src/kb-cc1354p10/README.md))
work the same way, just pointed at wherever CCS installed the SDK/tools on
your Mac instead of `/opt/ti/...`.

---

## Verifying against a known-good baseline

The exact package versions confirmed working in this project's own Linux
development environment (Python 3.12):

```
pip         26.2.1
pycrypto    2.6.1   <- use pycryptodome on macOS instead, see step 4
pyserial    3.5
pyusb       1.3.1
scapy       2.7.0
```

If something on macOS behaves differently, checking your installed
versions against this list is a reasonable first troubleshooting step
before assuming it's a macOS-specific issue.

---

## Troubleshooting

- **`ModuleNotFoundError: No module named 'killerbee'`** - `PYTHONPATH`
  isn't set, or isn't set in the shell you're actually running from (a
  new terminal tab doesn't inherit an `export` from a different tab
  unless it's in `~/.zshrc`). Re-run step 5's `export` line.
- **`ImportError` building/importing `Crypto`** - you installed `pycrypto`
  instead of `pycryptodome`, or have both installed and they're
  conflicting (`pip uninstall pycrypto` first, then `pip install
  pycryptodome`).
- **No `/dev/cu.usbmodem*` shows up at all** - try a different USB cable
  (some are charge-only) and a different port; if still nothing, install
  TI's XDS110 driver package (see step 6).
- **Everything above installed fine, but `zbid` sees nothing** - check
  `system_profiler SPUSBDataType` for the board's entry to confirm macOS
  sees it at the USB level at all before troubleshooting the Python side.
- **Board becomes unresponsive after heavy use** - not macOS-specific;
  this is a documented hardware/firmware characteristic of both boards,
  see [USAGE_GUIDE.md's Firmware recovery section](USAGE_GUIDE.md#firmware-recovery-cc1352p7--cc1354p10).
  The JTAG-nudge recovery commands there need the optional TI toolchain
  above; without it, a physical unplug/replug is the fallback.
