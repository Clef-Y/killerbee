# KillerBee firmware for the TI CC1352P7

Secondary/alternate target. **The CC1354P10 + standalone LP-XDS110 pairing
(`../kb-cc1354p10`) is this project's default, primary hardware** — see that
firmware's README for the fuller design writeup (RF command handling,
brown-out investigation, jammer-stop-path fix, etc.) - all of it applies
here too, since this target's `main.c` is a straight, near-unmodified copy
of `../kb-cc1354p10/main.c` kept in sync with it, not an independent
implementation. This target exists for people who have a CC1352P7
LaunchPad instead.

**One real, non-cosmetic difference: sub-1GHz uses a different PHY than
the CC1354P10.** CC1354P10 uses a SUN O-QPSK Rate Mode 0 preset; CC1352P7's
SysConfig radio-config database (`cc1352p7_prop_pg10`, checked directly
against the installed SDK, not assumed) has no O-QPSK phyType868 option at
all - confirmed by enumerating every valid option via a failed build
attempt with the O-QPSK preset name, which listed every real alternative.
This is a genuine difference between the CC13x2 (this chip) and CC13x4
(CC1354P10) RF core generations' SysConfig package, not a config mistake.
Ported to `2gfsk50kbps25dev915wsun1b` instead - Wi-SUN mode #1b, a real,
standards-compliant IEEE 802.15.4g SUN PHY for the 915 MHz region, just
FSK-modulated rather than O-QPSK. **Practical effect:** sub-1GHz jamming
(`CMD_TX_TEST` is modulation-agnostic) and ambient RSSI (`CMD_GET_RSSI`, a
raw RF-core energy read) work identically to the CC1354P10. Sub-1GHz
sniffing/decode only works against other FSK-modulated Wi-SUN 1b traffic,
**not** the O-QPSK traffic the CC1354P10 targets - a real capability
difference, not something papered over. See `kb_cc1352p7.syscfg`'s
`RF_Settings_SUBG_OQPSK` comment (name kept for git-history continuity,
content documents the FSK PHY) for the full detail, and `main.c`'s
sub-1GHz globals comment for the one code-level consequence: this preset
generates a `CMD_PROP_TX_ADV` command instead of the CC1354P10's plain
`CMD_PROP_TX` - field-compatible for what this firmware actually uses
(`.pktLen`/`.pPkt`), so no call-site changes were needed beyond a macro
alias.

The sub-1GHz channel-to-frequency math (`rfTuneToChannel()`) is unchanged
from the CC1354P10's - `CMD_FS` tunes frequency directly and is
modulation-agnostic, so the same 902.2 + 0.2*channel MHz / 0-128 channel
plan applies for tuning purposes regardless of which PHY preset is
configured for RX/TX.

## Hardware setup

Runs on a `LP-CC1352P7-1`/`-4` LaunchPad, using the LaunchPad's own
**onboard** XDS110 debug probe — unlike the CC1354P10 target, there is no
standalone external probe or separate target board here; it's all one PCB.

The same USB connection carries both JTAG and the probe's UART backchannel,
enumerating as two `/dev/ttyACM*` ports — check both, since one is the
debug/CMSIS-DAP interface and the other is the actual UART (empirically,
interface `00` was the UART and interface `03` was CMSIS-DAP on the unit
this was brought up on; don't assume symmetry with the CC1354P10 probe's
interface numbering — verify which is which with a `PING`, see below).

**This board has RXD<< and TXD>> jumpers near the XDS110 that are OPEN by
default and must be CLOSED for the backchannel UART to reach the target
chip at all** (see TI's `LP_CC1352P7_1` `Board.html`, "Jumper Settings" —
without this, the firmware boots fine and both LEDs stay off, since nothing
in the init path fails, but not a single byte crosses the wire in either
direction).

The onboard XDS110 needs 2-pin cJTAG mode, not standard JTAG — see this
directory's `CC1352P7_XDS110.ccxml`'s `SWD Mode Settings` property
(`Value="4"`, `"cJTAG (1149.7) 2-pin advanced modes"`, with `XDS110 Aux
Port` set). This is the CC1352P7 device's own documented default (see
`cc1352p7.xml` in the TI tooling) and differs from the CC1354P10's
standalone-probe setup, which needs standard JTAG instead.

The RF core, wire protocol, and KillerBee capabilities are otherwise
identical to the CC1354P10 firmware's — see `../kb-cc1354p10/README.md`'s
"Capabilities mapped to KillerBee" and "Wire protocol" sections in full,
including 2.4GHz + sub-1GHz SNIFF/INJECT/SETCHAN/PHYJAM/PHYJAM_REFLEX/
SELFACK/FREQ_915/GET_RSSI and the whole `0x01`-`0x0B` command set (see the
sub-1GHz PHY difference called out above for the one real capability
delta). Differs only in the firmware ID string (`KB-CC1352P7` instead of
`KB-CC1354P10`), that IEEE_DONE_OK-style status codes on this device
family come from the `cc13x2x7_cc26x2x7` driverlib headers (no dual-PAN
command fields, but this firmware never touched those anyway), and the
sub-1GHz PHY itself (above).

**Known shared risk, confirmed present on this chip too:** the 2.4GHz <->
sub-1GHz band switch (`RF_close()`+`RF_open()` in `rfSwitchBand()`) is the
same "unreliable blocking wait" the CC1354P10's README documents as an
accepted risk, not a solved problem. Reproduced directly on this board
during validation: one `SET_CHANNEL` call switching bands right after a
2.4GHz reflexive-jam cycle hung the board solid (no self-recovery, cleared
by the same `dslite.sh --mode memory` JTAG nudge documented in the
CC1354P10 README's troubleshooting section) - but a retry of the identical
command immediately after recovery succeeded cleanly, and every other band
switch performed during validation worked fine. Intermittent, not
deterministic, consistent with the CC1354P10's own characterization of
this specific operation - not a new bug introduced by this port, and not
independently root-caused here (that would need the same depth of live
JTAG investigation the CC1354P10's brown-out writeup went through, which
wasn't repeated for this secondary target).

## Building

```sh
source /opt/ti/ti-env.sh
cd firmware/src/kb-cc1352p7/tirtos7/ticlang
make
```

Produces `kb_cc1352p7.hex`. SysConfig regenerates `ti_radio_config.c/h`,
`ti_drivers_config.c/h`, etc. from `../../kb_cc1352p7.syscfg` automatically
as part of the build.

## Flashing

```sh
/opt/ti/uniflash_sl/dslite.sh -c firmware/src/kb-cc1352p7/CC1352P7_XDS110.ccxml \
    firmware/src/kb-cc1352p7/tirtos7/ticlang/kb_cc1352p7.hex
```

If two XDS110 probes are connected at once (e.g. this board plus a
CC1354P10 setup), DSLite still resolves the right one from the ccxml's
device platform section — this was verified: flashing the CC1352P7 target
with both probes attached left the CC1354P10 board's firmware and UART
untouched.

## Using it from KillerBee

```python
from killerbee import KillerBee

kb = KillerBee(device="/dev/ttyACM2", hardware="cc1352p7")
kb.set_channel(15)
kb.sniffer_on()
pkt = kb.pnext(timeout=2)
kb.inject(b"\x01\x02\x03...")
kb.driver.set_selfack(True)       # extra, non-standard KillerBee method
kb.jammer_on(method="reflexive")  # or method=None / "constant"
kb.jammer_off()

kb.set_channel(9, page=31)        # sub-1GHz (915 MHz), channels 0-128
kb.sniffer_on()                   # decodes Wi-SUN 1b FSK traffic, not O-QPSK - see above
print(kb.driver.get_rssi())       # ambient RSSI - works regardless of PHY match
kb.close()
```

Auto-detection also works (no `hardware=` needed) as long as
`DEV_ENABLE_CC1352P7` is `True` in `killerbee/config.py` (it is, by
default) — `KillerBee()` will probe serial devices with
`kbutils.iscc1352p7()`.

## Validation status

**2.4GHz (original port):** hardware-validated by direct wire-protocol
testing: `PING`/`GET_CHANNEL`/`SET_CHANNEL`, `SNIFFER_ON`/`OFF`, `INJECT`
(single frame), `JAMMER_ON`/`OFF` (constant), `RESET` — all clean, no
hangs. Not independently re-exercised in the sub-1GHz validation pass
below (multi-frame `INJECT`, invalid-payload rejection - code-identical to
the CC1354P10's already-validated versions).

**Sub-1GHz (this pass, real hardware, same LaunchPad):**
`SET_CHANNEL`/`GET_CHANNEL` (page 31, channel 9/14/19/24), `SNIFFER_ON/OFF`
(stays responsive while sniffing, verified via `PING`/`GET_CHANNEL` mid-
sniff), `GET_RSSI` while sniffing (returned a plausible ~-100 dBm noise
floor after one anomalously high first sample - likely AGC not yet
settled immediately after `SNIFFER_ON`, not re-investigated further),
`INJECT` (single frame), `SET_SELFACK` (enable/disable, command-level
only - no second sub-1GHz device available to confirm real over-the-air
ACK behavior, same caveat as the CC1354P10), `JAMMER_ON`/`OFF` reflexive
and constant (both stay responsive, `JAMMER_OFF` on constant-carrier does
**not** reboot - confirms the CC1354P10's jammer-stop-path fix carries
over correctly), and a 3-channel `SET_CHANNEL` rotation while
constant-carrier jamming stayed active (9→14→19→24, all clean, no hangs) -
mirrors the CC1354P10's own rotating-jam validation. `RESET` also
verified, same "needs a `dslite.sh --mode memory` nudge afterward"
behavior as the CC1354P10.

**Found during this pass, not a regression:** the 2.4GHz<->sub-1GHz band
switch hung once (see "Known shared risk" above) - recovered via the
documented JTAG nudge, and a retry succeeded. Intermittent behavior
inherited from shared `rfSwitchBand()` code, not unique to this chip.

Not yet validated end-to-end through the `killerbee` Python package's
`pnext()`/`sniffer_on()` layer with real captured Wi-SUN traffic (no
second sub-1GHz Wi-SUN-speaking device was available) — only raw
wire-level protocol testing plus the driver-level smoke test in
`killerbee/dev_cc1352p7.py`'s existing test coverage.
