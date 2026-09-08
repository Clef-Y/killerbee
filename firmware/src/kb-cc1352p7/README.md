# KillerBee firmware for the TI CC1352P7

Secondary/alternate target. **The CC1354P10 + standalone LP-XDS110 pairing
(`../kb-cc1354p10`) is this project's default, primary hardware** — see that
firmware's README for the fuller design writeup. This target exists for
people who have a CC1352P7 LaunchPad instead. Source is nearly identical
(`main.c` is a straight copy of `../kb-cc1354p10/main.c`, unmodified except
for the header comment and firmware ID string) — see that file's header for
why it ports cleanly across the two chip families.

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

The RF core, wire protocol, and KillerBee capabilities are identical to the
CC1354P10 firmware's — see `../kb-cc1354p10/README.md`'s "Capabilities
mapped to KillerBee" and "Wire protocol" sections; nothing here differs
except the firmware ID string (`KB-CC1352P7` instead of `KB-CC1354P10`) and
that IEEE_DONE_OK-style status codes on this device family come from the
`cc13x2x7_cc26x2x7` driverlib headers (no dual-PAN command fields, but this
firmware never touched those anyway).

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
kb.close()
```

Auto-detection also works (no `hardware=` needed) as long as
`DEV_ENABLE_CC1352P7` is `True` in `killerbee/config.py` (it is, by
default) — `KillerBee()` will probe serial devices with
`kbutils.iscc1352p7()`.

## Validation status

Hardware-validated by direct wire-protocol testing on a real CC1352P7
LaunchPad: `PING`/`GET_CHANNEL`/`SET_CHANNEL`, `SNIFFER_ON`/`OFF` (stays
responsive while sniffing), `INJECT` (single frame), `JAMMER_ON`/`OFF`
(constant mode, stays responsive while jamming), and `RESET` — all returned
correct status codes with no hangs, using the same `rfPostAndPoll()`
bounded-wait RF command handling validated on the CC1354P10. Not yet
independently exercised: multi-frame `INJECT`, invalid-payload rejection,
reflexive jam, and `SET_SELFACK` (all code-identical to the CC1354P10's
already-validated versions, but not re-run on this specific chip). Not yet
validated end-to-end through the `killerbee` Python package itself (the
`pyusb` dependency `kbutils.py` imports at module load time isn't
installed in the environment this was brought up in) — only raw wire-level
protocol testing.
