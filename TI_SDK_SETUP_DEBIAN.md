# TI SDK & Firmware Dev Environment - Debian/Linux (CC1354P10 / CC1352P7)

How to install TI's SimpleLink toolchain and set up the environment to
**build and flash** this project's custom CC1354P10 and CC1352P7 firmware
on Debian (and Debian-family Linux - Ubuntu etc.).

> **Host-only vs. firmware dev.** You only need any of this if you are
> going to **build firmware from source** or **flash/recover a board over
> JTAG**. To just *use* an already-flashed board with the KillerBee Python
> tools, skip this entirely - see [USAGE_GUIDE.md](USAGE_GUIDE.md#setup)
> for the (much smaller) host-side `pip` setup. The one piece of the TI
> toolchain that host-only users occasionally still need is UniFlash, for
> the JTAG recovery nudge documented in
> [USAGE_GUIDE.md's Firmware recovery section](USAGE_GUIDE.md#firmware-recovery-cc1352p7--cc1354p10);
> that section, and [`tools/kb_jtag_recover.sh`](tools/kb_jtag_recover.sh),
> are the practical entry points for recovery.

This project's firmware READMEs
([`firmware/src/kb-cc1352p7/README.md`](firmware/src/kb-cc1352p7/README.md),
[`firmware/src/kb-cc1354p10/README.md`](firmware/src/kb-cc1354p10/README.md))
are the authoritative source for the actual build/flash commands and every
hardware quirk. This document is the one-time *environment setup* those
commands assume; it does not replace them.

---

## Table of contents

- [What you're installing](#what-youre-installing)
- [Confirmed-working versions](#confirmed-working-versions)
- [Board / MCU reference](#board--mcu-reference)
- [1. System packages](#1-system-packages)
- [2. Install the SimpleLink SDK + toolchain](#2-install-the-simplelink-sdk--toolchain)
- [3. Install UniFlash (flashing / JTAG recovery)](#3-install-uniflash-flashing--jtag-recovery)
- [4. USB / serial permissions](#4-usb--serial-permissions)
- [5. Environment configuration](#5-environment-configuration)
- [6. Build the firmware](#6-build-the-firmware)
- [7. Flash the firmware](#7-flash-the-firmware)
- [8. Recovery](#8-recovery)
- [Troubleshooting](#troubleshooting)

---

## What you're installing

The firmware is a TI-RTOS 7 application built with TI's SimpleLink stack.
Four pieces make up the build/flash environment:

| Piece | Purpose |
|---|---|
| **SimpleLink CC13xx/CC26xx SDK** | Device headers, RF driver source, `ti154stack`/`prop_rf` reference examples, the TI-RTOS 7 kernel, and the per-device linker command files this firmware builds against. |
| **TI ARM CLANG compiler (`ticlang`, a.k.a. `ti-cgt-armllvm`)** | The actual C compiler/linker the makefiles invoke (`tiarmclang`, `tiarmobjcopy`). |
| **SysConfig** | Regenerates `ti_radio_config.c/h`, `ti_drivers_config.c/h`, etc. from each firmware's `.syscfg` file at build time - nothing hand-edited. |
| **UniFlash** (provides `dslite.sh` + the `DSLite` debug server) | Flashes the built `.hex` over the XDS110 debug probe, and runs the JTAG-level board-reset nudge this project uses for RF-core recovery. |

The SDK bundles the TI-RTOS kernel and XDCtools; the compiler and
SysConfig are separate tools the SDK's `imports.mak` points at. On Linux
this project's convention is to install everything under **`/opt/ti`** (see
[Environment configuration](#5-environment-configuration)); the firmware
makefiles and READMEs assume that layout, overridable by env var.

---

## Confirmed-working versions

These are the exact versions this project's firmware has been built and
validated against - a known-good baseline, not hard requirements (newer
SDK point releases generally work, but if a build breaks, matching these
is the first thing to try):

| Component | Version |
|---|---|
| SimpleLink CC13xx/CC26xx SDK | **8.33.00.16** |
| TI ARM CLANG compiler (`ti-cgt-armllvm`) | **3.2.2.LTS** |
| SysConfig | **1.21.1** |
| XDCtools (bundled with the SDK) | **3.62.01.15** |
| UniFlash (for `dslite.sh` / `DSLite`) | **9.6.0** |
| TI-RTOS | **7** (TI-RTOS 7 / SysBIOS 7, ships in the SDK) |

An `arm-none-eabi-gcc` (e.g. 12.3.Rel1) is a supported alternative
compiler in TI's `imports.mak`, but this project builds with **`ticlang`**
- the makefiles hardcode `tiarmclang`/`tiarmobjcopy`, so install ticlang.

---

## Board / MCU reference

The two boards are different silicon and build to different targets - the
makefiles already select the right one per firmware directory, but it's
useful to know which is which:

| Board | MCU | Core | Compiler `-mcpu` | SDK device family (linker cmd) | Debug probe |
|---|---|---|---|---|---|
| **CC1354P10** (primary) | CC1354P10 | Cortex-M33 (FPU: `fpv5-sp-d16`, hard float) | `cortex-m33` | `cc13x4_cc26x4` | **standalone LP-XDS110** (separate 20-pin JTAG cable) |
| **CC1352P7** (secondary) | CC1352P7 | Cortex-M4 | `cortex-m4` | `cc13x2x7_cc26x2x7` | **onboard XDS110** (single USB cable, cJTAG) |

Both enumerate as **two** `/dev/ttyACM*` ports (one CMSIS-DAP/debug, one
the KillerBee UART). The `.ccxml` target-config files checked into each
firmware directory encode the correct JTAG mode per board - standard JTAG
for the CC1354P10's standalone probe, 2-pin cJTAG for the CC1352P7's
onboard one. Do not swap them.

---

## 1. System packages

```sh
sudo apt-get update
sudo apt-get install -y build-essential make git libusb-1.0-0 python3

# 32-bit runtime libs - some TI toolchain/DSLite binaries are 32-bit and
# will fail with a cryptic "No such file or directory" on a 64-bit-only
# system without these:
sudo dpkg --add-architecture i386
sudo apt-get update
sudo apt-get install -y libc6:i386 libusb-0.1-4:i386 libgconf-2-4:i386 \
    libncurses5:i386 libstdc++6:i386
```

`make` and a working `python3` (SysConfig shells out to it) are the only
hard host requirements beyond the TI tools themselves.

---

## 2. Install the SimpleLink SDK + toolchain

The simplest path is TI's **SimpleLink CC13xx/CC26xx SDK** installer, which
can pull in the matching ticlang compiler and SysConfig, or install
**Code Composer Studio (CCS)**, which bundles SDK + compiler + SysConfig
together. Either way, this project's convention is to end up with:

```
/opt/ti/simplelink            # the SDK (symlink or real dir)
/opt/ti/<ticlang compiler>    # ti-cgt-armllvm
/opt/ti/<sysconfig>           # sysconfig CLI
```

Download the SDK `.run` installer from TI
(`SIMPLELINK-LOWPOWER-F2-SDK`, version 8.33.00.16) and run it:

```sh
chmod +x simplelink_cc13xx_cc26xx_sdk_8_33_00_16.run
./simplelink_cc13xx_cc26xx_sdk_8_33_00_16.run   # a GUI/console installer

# Then, matching this project's /opt/ti convention, point a stable path at it:
sudo ln -s ~/ti/simplelink_cc13xx_cc26xx_sdk_8_33_00_16 /opt/ti/simplelink
```

The SDK's own **`imports.mak`** (at the SDK root) is where the paths to the
compiler, SysConfig and XDCtools are set - open it and confirm these
resolve to real directories on your machine:

```makefile
XDC_INSTALL_DIR        ?= /opt/ti/xdctools_3_62_01_15_core
SYSCONFIG_TOOL         ?= /opt/ti/sysconfig_1.21.1/sysconfig_cli.sh
TICLANG_ARMCOMPILER    ?= /opt/ti/ti-cgt-armllvm_3.2.2.LTS-0
PYTHON                 ?= python3
```

If you installed the compiler/SysConfig somewhere else (e.g. inside a CCS
install), edit these to match. The firmware makefiles `include` this
`imports.mak`, so getting it right here is what makes the build work.

Optionally create `/opt/ti/ti-env.sh` (the firmware READMEs `source` it)
to put the tools on `PATH`:

```sh
# /opt/ti/ti-env.sh
export SIMPLELINK_CC13XX_CC26XX_SDK_INSTALL_DIR=/opt/ti/simplelink
export PATH="/opt/ti/ti-cgt-armllvm_3.2.2.LTS-0/bin:$PATH"
```

---

## 3. Install UniFlash (flashing / JTAG recovery)

UniFlash provides `dslite.sh` and the `DSLite` debug-server binary this
project's flash and recovery commands use.

```sh
chmod +x uniflash_sl.9.6.0.*.run
./uniflash_sl.9.6.0.*.run
sudo ln -s ~/ti/uniflash_9.6.0 /opt/ti/uniflash_sl   # matches project convention
```

The recovery helper defaults to `/opt/ti/uniflash_sl/dslite.sh`; override
with `DSLITE=/path/to/dslite.sh` if you installed elsewhere (see
[`tools/kb_jtag_recover.sh`](tools/kb_jtag_recover.sh)).

---

## 4. USB / serial permissions

So you can talk to the boards without `sudo`:

```sh
sudo usermod -aG dialout "$USER"    # /dev/ttyACM* group access - re-login after
```

Add a udev rule for the XDS110 probe (TI vendor ID `0451`) so DSLite and
KillerBee both get non-root access:

```sh
sudo tee /etc/udev/rules.d/71-ti-xds110.rules >/dev/null <<'EOF'
# TI XDS110 debug probe (CC1352P7 onboard + standalone LP-XDS110)
SUBSYSTEM=="usb", ATTRS{idVendor}=="0451", MODE="0660", GROUP="dialout"
SUBSYSTEM=="tty", ATTRS{idVendor}=="0451", MODE="0660", GROUP="dialout"
EOF
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Unplug/replug the boards after applying. Confirm they enumerate:

```sh
ls /dev/ttyACM*        # two per board
lsusb | grep -i 0451   # "Texas Instruments" XDS110
```

---

## 5. Environment configuration

The firmware makefiles resolve the SDK via one variable, with the project
convention as its default:

```makefile
SIMPLELINK_CC13XX_CC26XX_SDK_INSTALL_DIR ?= /opt/ti/simplelink
include $(SIMPLELINK_CC13XX_CC26XX_SDK_INSTALL_DIR)/imports.mak
```

So there are two equivalent ways to build:

- **Follow the `/opt/ti` convention** (symlinks from step 2/3) and the
  defaults just work - this is what the firmware READMEs assume.
- **Point the env var at your own install** without symlinks:
  ```sh
  export SIMPLELINK_CC13XX_CC26XX_SDK_INSTALL_DIR=~/ti/simplelink_cc13xx_cc26xx_sdk_8_33_00_16
  ```
  Make sure that SDK's `imports.mak` in turn points at your real compiler /
  SysConfig / XDCtools paths (step 2).

---

## 6. Build the firmware

Same flow for both boards, just different directories (from the repo root):

```sh
source /opt/ti/ti-env.sh    # or export SIMPLELINK_..._INSTALL_DIR yourself

# CC1354P10
cd firmware/src/kb-cc1354p10/tirtos7/ticlang
make                        # -> kb_cc1354p10.hex (+ .out/.map)

# CC1352P7
cd firmware/src/kb-cc1352p7/tirtos7/ticlang
make                        # -> kb_cc1352p7.hex
```

SysConfig regenerates `ti_radio_config.*`, `ti_drivers_config.*`, etc.
from the parent directory's `.syscfg` automatically as part of `make` -
there is nothing to hand-edit in the generated files. To open the config
GUI instead: `make syscfg-gui` (needs a standalone SysConfig, not the
CCS-embedded one - the makefile prints guidance if it can't find it).

---

## 7. Flash the firmware

Uses the checked-in per-board `.ccxml` target configuration (correct JTAG
mode already set per board):

```sh
# CC1354P10 (standalone LP-XDS110, standard JTAG)
/opt/ti/uniflash_sl/dslite.sh -c firmware/src/kb-cc1354p10/CC1354P10_XDS110.ccxml \
    firmware/src/kb-cc1354p10/tirtos7/ticlang/kb_cc1354p10.hex

# CC1352P7 (onboard XDS110, cJTAG)
/opt/ti/uniflash_sl/dslite.sh -c firmware/src/kb-cc1352p7/CC1352P7_XDS110.ccxml \
    firmware/src/kb-cc1352p7/tirtos7/ticlang/kb_cc1352p7.hex
```

`dslite.sh` **defaults to flash mode** - do **not** pass a redundant
`flash` argument or it mis-parses and reports `Unable to open file: flash`.

With two XDS110 probes attached at once, DSLite still resolves the right
one from the `.ccxml`'s device platform section - verified: flashing one
board with both connected leaves the other's firmware/UART untouched.

---

## 8. Recovery

If a board's RF core gets stuck after heavy use (command rejected with
`STATUS_ERROR`, or the UART stops responding), a JTAG-level nudge through
the same probe clears it - no reflash needed:

```sh
tools/kb_jtag_recover.sh cc1354p10
tools/kb_jtag_recover.sh cc1352p7
```

Full explanation, including the separate "the debug probe itself is
wedged" case (needs a physical USB unplug/replug, not a JTAG nudge), is in
[USAGE_GUIDE.md's Firmware recovery section](USAGE_GUIDE.md#firmware-recovery-cc1352p7--cc1354p10)
and each firmware's README.

---

## Troubleshooting

- **`imports.mak: No such file or directory`** during `make` - the SDK
  path is wrong. Either the `/opt/ti/simplelink` symlink is missing/broken
  (step 2) or `SIMPLELINK_CC13XX_CC26XX_SDK_INSTALL_DIR` points at the
  wrong place (step 5).
- **`tiarmclang: not found` / compiler path errors** - the SDK's
  `imports.mak` `TICLANG_ARMCOMPILER` points at a directory that doesn't
  exist. Edit it to your real ticlang install path.
- **SysConfig step fails or hangs** - it shells out to `python3`; confirm
  `python3` runs. If it complains about the GUI framework specifically,
  that's only relevant to `make syscfg-gui`, not a normal `make`.
- **DSLite: `No such file or directory` for a binary that clearly exists** -
  a 32-bit binary on a 64-bit-only system; install the `i386` runtime
  libs from step 1.
- **DSLite: `Error -261`/`Error -260` even for `--help`** - the XDS110
  probe's own firmware is wedged (not the target chip). A USB bus reset
  and DFU toggling don't clear it in practice; physically unplug/replug
  the probe's USB cable. See the firmware READMEs.
- **`Unable to open file: flash`** - you passed a redundant `flash`
  positional arg to `dslite.sh`; remove it (see step 7).
- **Permission denied on `/dev/ttyACM*` or the probe** - you're not in
  `dialout`, haven't re-logged-in since `usermod`, or the udev rule from
  step 4 isn't applied.
