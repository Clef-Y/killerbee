# TI SDK & Firmware Dev Environment - macOS (CC1354P10 / CC1352P7)

How to install TI's SimpleLink toolchain and set up the environment to
**build and flash** this project's custom CC1354P10 and CC1352P7 firmware
on macOS (Apple Silicon and Intel).

> **Host-only vs. firmware dev.** You only need any of this if you are
> going to **build firmware from source** or **flash/recover a board over
> JTAG**. To just *use* an already-flashed board with the KillerBee Python
> tools, you don't need the TI SDK at all - see
> [MACOS_SETUP.md](MACOS_SETUP.md) for the host-side Python/`pip` setup,
> which is all most users ever need. This document is the firmware-toolchain
> companion to that one: it fills in the "Optional: firmware build/flash
> toolchain" section MACOS_SETUP.md deliberately leaves open.

The authoritative build/flash commands and every hardware quirk live in the
firmware READMEs
([`firmware/src/kb-cc1352p7/README.md`](firmware/src/kb-cc1352p7/README.md),
[`firmware/src/kb-cc1354p10/README.md`](firmware/src/kb-cc1354p10/README.md)).
Those READMEs are written around a Linux `/opt/ti` layout; this document is
the macOS-specific environment setup that makes the same commands work with
macOS install paths.

---

## Table of contents

- [What you're installing](#what-youre-installing)
- [Confirmed-working versions](#confirmed-working-versions)
- [Board / MCU reference](#board--mcu-reference)
- [1. Prerequisites (Homebrew, Rosetta 2)](#1-prerequisites-homebrew-rosetta-2)
- [2. Install the toolchain via CCS](#2-install-the-toolchain-via-ccs)
- [3. Install UniFlash (flashing / JTAG recovery)](#3-install-uniflash-flashing--jtag-recovery)
- [4. Point the SDK's imports.mak at your tools](#4-point-the-sdks-importsmak-at-your-tools)
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
| **TI ARM CLANG compiler (`ticlang` / `ti-cgt-armllvm`)** | The C compiler/linker the makefiles invoke (`tiarmclang`, `tiarmobjcopy`). |
| **SysConfig** | Regenerates `ti_radio_config.c/h`, `ti_drivers_config.c/h`, etc. from each firmware's `.syscfg` at build time. |
| **UniFlash** (`dslite.sh` + `DSLite`) | Flashes the built `.hex` over the XDS110 probe, and runs the JTAG board-reset nudge used for RF-core recovery. |

On macOS the cleanest way to get the first three is **Code Composer Studio
(CCS)**, whose installer bundles the compiler and SysConfig alongside (or
findable by) the SDK. UniFlash is a separate download. Unlike the Linux
`/opt/ti` convention, macOS installs land under `/Applications/ti/` (tools)
and `~/ti/` (SDKs) by default - so on macOS you configure paths rather than
rely on the makefiles' `/opt/ti` defaults (see step 4/5).

---

## Confirmed-working versions

Exact versions this project's firmware has been built/flashed with on
macOS - a known-good baseline, not hard requirements:

| Component | Version | Default location on this project's Mac |
|---|---|---|
| SimpleLink CC13xx/CC26xx SDK | **8.33.00.16** | `~/ti/simplelink_cc13xx_cc26xx_sdk_8_33_00_16/` |
| Code Composer Studio (bundles compiler + SysConfig) | **12.7.0** | `/Applications/ti/ccs1270/` |
| TI ARM CLANG compiler (`ti-cgt-armllvm`) | **3.2.2.LTS** | `/Applications/ti/ccs1270/ccs/tools/compiler/ti-cgt-armllvm_3.2.2.LTS-0/` |
| SysConfig | **1.21.1** | `/Applications/ti/ccs1270/ccs/utils/sysconfig_1.21.1/` |
| XDCtools | **3.62.01.15** | `/Applications/ti/xdctools_3_62_01_15_core/` |
| UniFlash | **9.6.0** | `/Applications/ti/uniflash_9.6.0/` |

Your version-suffixed paths will differ; the ones above are what this
project's `imports.mak` and recovery commands are set to. Adjust to match.

---

## Board / MCU reference

| Board | MCU | Core | Compiler `-mcpu` | SDK device family | Debug probe |
|---|---|---|---|---|---|
| **CC1354P10** (primary) | CC1354P10 | Cortex-M33 (FPU `fpv5-sp-d16`, hard float) | `cortex-m33` | `cc13x4_cc26x4` | **standalone LP-XDS110** (separate 20-pin JTAG) |
| **CC1352P7** (secondary) | CC1352P7 | Cortex-M4 | `cortex-m4` | `cc13x2x7_cc26x2x7` | **onboard XDS110** (single USB, cJTAG) |

Both boards enumerate as **two** ports on macOS -
`/dev/cu.usbmodem*` / `/dev/tty.usbmodem*` (one CMSIS-DAP/debug, one the
KillerBee UART). Each firmware directory's `.ccxml` encodes the correct
JTAG mode per board; don't swap them.

---

## 1. Prerequisites (Homebrew, Rosetta 2)

If you haven't already done the host-side setup, do
[MACOS_SETUP.md](MACOS_SETUP.md) first (Homebrew, `libusb`, the Python
virtualenv). Beyond that, for the TI toolchain:

```sh
# Apple Silicon (M1/M2/M3/...) only: TI's DSLite debug-server binary is
# x86_64, so it needs Rosetta 2. Without this, dslite.sh fails with
# "Bad CPU type in executable" and no flash/recovery works.
softwareupdate --install-rosetta --agree-to-license
```

You can check your Mac's architecture with `uname -m` (`arm64` = Apple
Silicon, needs Rosetta; `x86_64` = Intel, doesn't). `make` and `python3`
come with the Xcode Command Line Tools (`xcode-select --install`).

---

## 2. Install the toolchain via CCS

Download **Code Composer Studio** (the version above, or current) from TI
and run the macOS installer. During install, select support for the
**CC13xx/CC26xx** device family - this pulls in the ticlang compiler and
SysConfig, and offers to install the **SimpleLink CC13xx/CC26xx SDK**.

If you'd rather install the SDK standalone (without the full IDE), download
the **SimpleLink CC13xx/CC26xx SDK 8.33.00.16** macOS installer from TI
separately - it lands in `~/ti/` by default. You still need the ticlang
compiler and SysConfig from somewhere (CCS is the easiest source of both
on macOS).

After install you should have, adjusting versions to match yours:

```
~/ti/simplelink_cc13xx_cc26xx_sdk_8_33_00_16/          # the SDK
/Applications/ti/ccs1270/ccs/tools/compiler/ti-cgt-armllvm_3.2.2.LTS-0/
/Applications/ti/ccs1270/ccs/utils/sysconfig_1.21.1/
/Applications/ti/xdctools_3_62_01_15_core/
```

> **Gatekeeper.** macOS may quarantine freshly-downloaded TI binaries the
> first time they run ("cannot be opened because the developer cannot be
> verified"). Approve them in **System Settings -> Privacy & Security**, or
> clear the quarantine flag: `xattr -dr com.apple.quarantine /Applications/ti/uniflash_9.6.0`.

---

## 3. Install UniFlash (flashing / JTAG recovery)

Download **UniFlash** (standalone, version above) for macOS and install it
to `/Applications/ti/uniflash_9.6.0/`. It provides `dslite.sh` and the
`DSLite` debug server used for flashing and JTAG recovery.

This project's recovery helper defaults to the Linux
`/opt/ti/uniflash_sl/dslite.sh` path, so on macOS pass the real path via
the `DSLITE` env var:

```sh
DSLITE=/Applications/ti/uniflash_9.6.0/dslite.sh tools/kb_jtag_recover.sh cc1354p10
```

(Set `export DSLITE=/Applications/ti/uniflash_9.6.0/dslite.sh` in your
`~/.zshrc` to avoid typing it each time.)

---

## 4. Point the SDK's imports.mak at your tools

The firmware makefiles `include` the SDK's **`imports.mak`** (at the SDK
root) to find the compiler, SysConfig and XDCtools. On macOS these paths
default to macOS locations already if you installed via CCS, but confirm
they match your actual install - this is the file to edit if a build can't
find a tool:

```makefile
# ~/ti/simplelink_cc13xx_cc26xx_sdk_8_33_00_16/imports.mak
XDC_INSTALL_DIR        ?= /Applications/ti/xdctools_3_62_01_15_core
SYSCONFIG_TOOL         ?= /Applications/ti/ccs1270/ccs/utils/sysconfig_1.21.1/sysconfig_cli.sh
TICLANG_ARMCOMPILER    ?= /Applications/ti/ccs1270/ccs/tools/compiler/ti-cgt-armllvm_3.2.2.LTS-0
GCC_ARMCOMPILER        ?= /Applications/arm-none-eabi-gcc/12.3.Rel1-0
PYTHON                 ?= python3
```

This project builds with **`ticlang`** (the makefiles hardcode
`tiarmclang`/`tiarmobjcopy`), so `TICLANG_ARMCOMPILER` is the one that must
be right; `GCC_ARMCOMPILER` is not used here.

---

## 5. Environment configuration

The firmware makefiles resolve the SDK via one variable. Its default is the
Linux `/opt/ti/simplelink` path, which does **not** exist on a normal macOS
install - so on macOS you set it explicitly:

```sh
export SIMPLELINK_CC13XX_CC26XX_SDK_INSTALL_DIR=~/ti/simplelink_cc13xx_cc26xx_sdk_8_33_00_16
```

Add that to `~/.zshrc` (along with the `DSLITE` line from step 3) so every
new terminal has it. With this set, the firmware makefiles find the SDK,
and the SDK's `imports.mak` (step 4) finds the rest.

You can skip the separate `source /opt/ti/ti-env.sh` step the firmware
READMEs mention - that's the Linux convention; the single env var above is
its macOS equivalent.

---

## 6. Build the firmware

From the repo root, with the env var from step 5 set:

```sh
# CC1354P10
cd firmware/src/kb-cc1354p10/tirtos7/ticlang
make                        # -> kb_cc1354p10.hex (+ .out/.map)

# CC1352P7
cd firmware/src/kb-cc1352p7/tirtos7/ticlang
make                        # -> kb_cc1352p7.hex
```

SysConfig regenerates the `ti_radio_config.*` / `ti_drivers_config.*` files
from the parent directory's `.syscfg` automatically - nothing to hand-edit.
`make syscfg-gui` opens the config GUI (needs the standalone SysConfig the
makefile points at, not one embedded in a different CCS).

---

## 7. Flash the firmware

Using the checked-in per-board `.ccxml` (correct JTAG mode already set), and
the macOS UniFlash path:

```sh
DSLITE=/Applications/ti/uniflash_9.6.0/dslite.sh

# CC1354P10 (standalone LP-XDS110, standard JTAG)
"$DSLITE" -c firmware/src/kb-cc1354p10/CC1354P10_XDS110.ccxml \
    firmware/src/kb-cc1354p10/tirtos7/ticlang/kb_cc1354p10.hex

# CC1352P7 (onboard XDS110, cJTAG)
"$DSLITE" -c firmware/src/kb-cc1352p7/CC1352P7_XDS110.ccxml \
    firmware/src/kb-cc1352p7/tirtos7/ticlang/kb_cc1352p7.hex
```

`dslite.sh` **defaults to flash mode** - do **not** add a `flash` positional
argument or it reports `Unable to open file: flash`. With both probes
attached, DSLite still resolves the right one from the `.ccxml`.

---

## 8. Recovery

If a board's RF core gets stuck after heavy use (command rejected, or UART
stops responding), the JTAG-level nudge clears it without reflashing:

```sh
DSLITE=/Applications/ti/uniflash_9.6.0/dslite.sh tools/kb_jtag_recover.sh cc1354p10
DSLITE=/Applications/ti/uniflash_9.6.0/dslite.sh tools/kb_jtag_recover.sh cc1352p7
```

Full detail (including the "probe itself wedged -> physical unplug/replug"
case) is in
[USAGE_GUIDE.md's Firmware recovery section](USAGE_GUIDE.md#firmware-recovery-cc1352p7--cc1354p10)
and each firmware's README.

---

## Troubleshooting

- **`dslite.sh` / `DSLite`: `Bad CPU type in executable`** - Apple Silicon
  without Rosetta 2. Run `softwareupdate --install-rosetta --agree-to-license`
  (step 1). This is the single most common macOS-only failure.
- **`imports.mak: No such file or directory`** during `make` -
  `SIMPLELINK_CC13XX_CC26XX_SDK_INSTALL_DIR` isn't set or points at the
  wrong place. On macOS it must be set explicitly (step 5); the `/opt/ti`
  default doesn't exist here.
- **`tiarmclang: not found` / compiler errors** - the SDK's `imports.mak`
  `TICLANG_ARMCOMPILER` points at a path that doesn't exist. Edit it to
  your real CCS compiler path (step 4).
- **"cannot be opened because the developer cannot be verified"** -
  Gatekeeper quarantine on a TI binary. Approve it in System Settings ->
  Privacy & Security, or `xattr -dr com.apple.quarantine /Applications/ti/<tool>`.
- **No `/dev/cu.usbmodem*` appears** - try another USB cable (some are
  charge-only) and port; confirm the board is visible at the USB level with
  `system_profiler SPUSBDataType | grep -i xds110` before troubleshooting
  higher up. See [MACOS_SETUP.md](MACOS_SETUP.md).
- **`Unable to open file: flash`** - redundant `flash` arg passed to
  `dslite.sh`; remove it (step 7).
- **DSLite `Error -261`/`Error -260` even for `--help`** - the XDS110
  probe's own firmware is wedged (not the target). Physically unplug/replug
  the probe's USB cable; a bus reset alone doesn't clear it.
