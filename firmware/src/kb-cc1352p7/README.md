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
plan (page 31) applies for tuning purposes regardless of which PHY preset
is configured for RX/TX. Page 28 (863-876 MHz EU/UK, channels 0-65, 863.0
+ 0.2*channel MHz) was added later, ported verbatim from the CC1354P10's
own page 28 for the same reason - see "On-chip channel-hop jamming" below.

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
SELFACK/FREQ_915/FREQ_863/FREQ_863_WIDE/GET_RSSI and the whole
`0x01`-`0x0B` command set (see the sub-1GHz PHY difference called out
above for the one real capability delta). Also has page 28 (863-876 MHz
EU/UK, channels 0-65), ported verbatim from the CC1354P10's own page 28 -
same channel plan, same reasoning, so the same channel number means the
same real frequency on both boards. Differs only in the firmware ID
string (`KB-CC1352P7` instead of `KB-CC1354P10`), that IEEE_DONE_OK-style
status codes on this device family come from the `cc13x2x7_cc26x2x7`
driverlib headers (no dual-PAN command fields, but this firmware never
touched those anyway), the sub-1GHz PHY itself (above), and two additions
in the other direction: `0x0C`/`KB_CMD_JAM_HOP_ON` (2.4GHz-only on-chip
hop) exists only here, not on the CC1354P10; and `0x0D`/
`KB_CMD_JAM_HOP_SUBG_ON` (sub-1GHz, cross-page on-chip hop) exists on
*both* firmwares but under different command numbers (`0x0D` here,
`0x0C` on the CC1354P10, since that one only ever needed a single hop
command) - see "On-chip channel-hop jamming" below for both.

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

## On-chip channel-hop jamming (`KBCapabilities.PHYJAM_HOP`)

This firmware has *two* on-chip hop commands - `CMD_JAM_HOP_ON` (2.4GHz,
below) and `CMD_JAM_HOP_SUBG_ON` (sub-1GHz cross-page, further down) - kept
separate since the 2.4GHz one's wire format (bare channel, no page field)
predates page 28 support and has no room to add one without breaking it.

### 2.4GHz (`CMD_JAM_HOP_ON`, 0x0C)

`CMD_JAM_HOP_ON` (0x0C) starts a constant-carrier jam that rotates across a
host-supplied 2.4GHz channel list (page 0, channels 11-26) entirely inside
the firmware - `jamHopThread()` in `main.c` loops calling the same
`rfTuneToChannel()`/`rfJamStart()`/`rfJamStop()` primitives `SET_CHANNEL`
already uses, with no host round trip per hop. Stopped the same way as
every other jam mode, `JAMMER_OFF` - no separate "off" command.

```python
kb.jam_hop_on([11, 12, 13, 14, 15, 20, 22, 26], dwell_ms=20)
time.sleep(10)
kb.jammer_off()
```

or via `tools/jam24_hop.py`:

```sh
python3 tools/jam24_hop.py -i /dev/cu.usbmodemL45003IW1 -c 11,12,13,14,15,20,22,26 --dwell 0.02
```

**Why this exists, not just `tools/jam24_rotate.py`:** driving the exact
same rotation by having the host call `SET_CHANNEL` once per hop pays a
real, fixed per-hop cost. Measured directly on this project's hardware (a
sibling board, same USB/XDS110-debug-probe architecture): a near-flat
~30ms round trip per `SET_CHANNEL` call, and critically, **the same ~30ms
whether or not any real RF retuning happens at all** - a `PING` (zero RF
work), a same-channel `SET_CHANNEL` (firmware's tune cache skips the
retune), a real cross-channel retune, and a full stop/retune/restart while
jamming all measured within noise of each other. That proves the ~30ms is
a fixed USB/debug-probe control-plane tax, not RF circuit response time -
the RF core's own work is negligible next to it. So a `--dwell` below
~30ms is meaningless with the host-driven approach (the fixed floor
dominates regardless); looping the hop entirely on-chip removes that tax,
leaving the real per-hop cost as `rfTuneToChannel()`'s own
`rfPostAndPoll()` polling grain (~1ms worst case) plus whatever dwell was
asked for - two orders of magnitude tighter, and `--dwell` values well
under 30ms (e.g. 20ms, as above) become meaningful.

**2.4GHz only, deliberately.** `MAX_HOP_CHANNELS` is 16 - the exact count
of real channels (11-26) - and every channel in the list is range-checked
before starting. Restarting after an unrelated `SET_CHANNEL` (e.g. a
sniffer/inject call that stops and later needs to resume hop-jamming) is
handled - `startJammer()`'s `JAM_MODE_HOP` branch re-arms
`jamHopThread()` from the retained channel list/dwell rather than losing
the rotation.

**Hardware-validated with real RF evidence, not just command-level
success.** Command-level: `JAM_HOP_ON` accepted, device stays fully
responsive to `PING`/`GET_CHANNEL` while hopping, `JAMMER_OFF` stops it
cleanly, and all three invalid-payload cases (out-of-range channel,
`dwell_ms=0`, channel-count/length mismatch) correctly rejected with
`STATUS_ERROR`. Beyond that: verified with an independent RF energy check
using a second board (CC1354P10) doing ambient RSSI sampling
(`GET_RSSI` while sniffing) on each target channel while this one hopped
`[11,12,13,14,15,20,22,26]` at 20ms dwell - every listed channel showed a
strong, repeatable spike (~-59 to -70 dBm) against a ~-106 to -112 dBm
noise floor, while channels *not* in the list stayed much closer to
baseline. Confirms real, on-air hopping across exactly the given channels,
not just a firmware state machine that doesn't crash. Also ran a
sustained 20-second/~125-cycle continuous hop with no degradation and a
healthy board immediately after.

### Sub-1GHz, cross-page (`CMD_JAM_HOP_SUBG_ON`, 0x0D)

Ported verbatim from the CC1354P10 firmware's `CMD_JAM_HOP_ON` (its only
hop command, since it has no 2.4GHz hop to collide with) - same
cross-page design (page 31 and/or page 28), same reason each entry is an
inclusive `[page][chStart][chEnd]` range rather than a bare channel or
`[page][channel]` pair: the two pages share `BAND_SUBG` but use different
`rfTuneToChannel()` frequency formulas at the same raw channel number, and
ranges (not one entry per channel) are what let a large contiguous
request fit the wire protocol's single-byte payload-length ceiling at
all. See `../kb-cc1354p10/README.md`'s own "On-chip channel-hop jamming"
section for the full design writeup - it applies here unchanged, just
under command number `0x0D` instead of `0x0C` (this firmware's `0x0C` is
already taken by the 2.4GHz hop above).

```python
kb.jam_hop_subg_on([(31, 9), (31, 14), (28, 10), ...], dwell_ms=20)
time.sleep(10)
kb.jammer_off()
```

or via `tools/subg_jam_hop.py` (same tool the CC1354P10 uses - just pass
`-d cc1352p7`):

```sh
python3 tools/subg_jam_hop.py -i /dev/cu.usbmodemL45003IW1 -d cc1352p7 --dwell 0.02 \
    --page-channels 28:9-65
```

`MAX_HOP_RANGES` is 84 here too - the actual wire-protocol ceiling (a
255-byte payload cap, 3-byte header + 3 bytes/range), not an arbitrary
pick; a single-range full-page request only ever needs 1 of the 84.

**Hardware-validated with real RF evidence, including closing a gap left
open on the CC1354P10 side.** Command-level: `JAM_HOP_SUBG_ON` accepted
for a `(28, 9, 65)` range, device stays fully responsive to
`PING`/`GET_CHANNEL` while hopping (`GET_CHANNEL` mid-hop correctly
reported a real in-progress channel within the range), `JAMMER_OFF` stops
it cleanly, and an out-of-range page (page 0) is correctly rejected with
`STATUS_ERROR`. Beyond that: this board's own new page 28 support (above)
was used as the independent RF monitor for the CC1354P10's page 28
`JAM_HOP_ON` - something the CC1354P10 README's own validation explicitly
couldn't do yet ("no second board here can tune there"). With the
CC1354P10 hopping page 28 channels 9-65, this board's `GET_RSSI` (via
`SET_CHANNEL`/`SNIFFER_ON` at page 28) showed a strong, repeatable spike
(~-62 to -64 dBm) on every sampled in-range channel (9, 20, 30, 40, 50,
65 - spanning the full requested range including both endpoints) against
a ~-108 to -115 dBm noise floor on channels 0 and 5 (valid page 28
channels, but outside the jammed 9-65 range) - confirming genuine on-air
hopping across the full range on both boards' page 28 implementations at
once.

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

**CMD_INJECT command-pool exhaustion:** same root cause and same fix as
the CC1354P10 - see that README's "CMD_INJECT exhausts the RF driver's
command pool after repeated calls" section, including the real fix
(`RF_cancelCmd()` after normal completion, not just the retune-cache
mitigation). Confirmed independently on this board at every stage: 4
before the retune-cache fix, 8 after, 100/100 with zero failures after the
`RF_cancelCmd()` fix - all matching the CC1354P10 exactly.

**Found during this pass, not a regression:** the 2.4GHz<->sub-1GHz band
switch hung once (see "Known shared risk" above) - recovered via the
documented JTAG nudge, and a retry succeeded. Intermittent behavior
inherited from shared `rfSwitchBand()` code, not unique to this chip.

Not yet validated end-to-end through the `killerbee` Python package's
`pnext()`/`sniffer_on()` layer with real captured Wi-SUN traffic (no
second sub-1GHz Wi-SUN-speaking device was available) — only raw
wire-level protocol testing plus the driver-level smoke test in
`killerbee/dev_cc1352p7.py`'s existing test coverage.
