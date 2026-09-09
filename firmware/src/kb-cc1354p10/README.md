# KillerBee firmware for the TI CC1354P10

## Standard hardware setup

Runs on a CC1354P10 target (the `LP-EM-CC1354P10-1`/`-6` EM boosterpack)
connected via its 20-pin debug connector **directly** to a standalone
**LP-XDS110** debug probe — no LaunchPad, no separate USB-serial adapter.
That single connector carries both full JTAG (TMS/TCK/TDO/TDI/RST) for
flashing/debugging, and the probe's UART backchannel (RXD/TXD) for talking
to the running firmware. This is the only supported/tested hardware
configuration — treat it as fixed, not one option among several.

The host reaches the firmware over the same serial port the XDS110
exposes as its auxiliary "backchannel" UART (`/dev/ttyACM0` on Linux —
check both `/dev/ttyACM*` ports the probe enumerates, since one is the
debug/CMSIS-DAP interface and the other is the actual UART), wired to the
CC1354P10's `UART2` (`CONFIG_UART2_KB`, DIO12/13 in this project's
`.syscfg`).

A standalone LP-XDS110 needs standard JTAG mode, not cJTAG - see this
directory's `CC1354P10_XDS110.ccxml`'s `SWD Mode Settings` property (`Value="0"`,
`"JTAG (1149.1), SWD and cJTAG are disabled"`). This differs from a
LaunchPad's onboard XDS110, which typically defaults to cJTAG.

It drives the RF core directly with raw `CMD_IEEE_RX` / `CMD_IEEE_TX` /
`CMD_TX_TEST` commands on the native 2.4 GHz IEEE 802.15.4 PHY (channels
11-26) instead of going through TI's 15.4 MAC stack, so KillerBee gets full
promiscuous capture, arbitrary frame injection, and PHY-level jamming — the
things a MAC-filtered "coprocessor" firmware would take away.

As of stage 4, it also supports the 915 MHz US ISM band (channels 1-10)
via a second, runtime-switchable radio setup - see "Sub-1GHz support"
below.

## Capabilities mapped to KillerBee

| KBCapabilities   | Supported | Notes |
|------------------|-----------|-------|
| SNIFF            | yes       | Promiscuous `CMD_IEEE_RX` (2.4GHz) / `CMD_PROP_RX` (915MHz), frame filtering off by default |
| SETCHAN          | yes       | Channels 11-26 (2.4 GHz, page 0) and 1-10 (915 MHz, page 31) |
| INJECT           | yes       | `CMD_IEEE_TX` (2.4GHz) / `CMD_PROP_TX` (915MHz), hardware auto-computes/appends the FCS |
| SELFACK          | yes*      | Extra `driver.set_selfack()` method — see caveat below; no generic KillerBee-level setter exists in this codebase for any device. 2.4 GHz only - see Sub-1GHz support |
| PHYJAM           | yes       | Continuous `CMD_TX_TEST` (modulated PRBS-15 garbage) - PHY-agnostic, works on either band |
| PHYJAM_REFLEX    | yes*      | Best-effort software-loop reflex — see caveat below. 2.4 GHz only - see Sub-1GHz support |
| SET_SYNC         | no        | The native IEEE 802.15.4 RX/TX commands use a fixed, standard O-QPSK preamble/SFD; there is no register here to reprogram it (unlike CC2420-style radios) |
| FREQ_2400        | yes       | |
| FREQ_915         | yes       | 915 MHz US ISM, channels 1-10 - see "Sub-1GHz support" below |
| FREQ_900/863/868/870 | no    | Only 2.4 GHz and 915 MHz US ISM are configured in this firmware |
| BOOT             | no        | No bootloader protocol exposed over this UART; reflash via the debug probe (see below) |

\* **Self-ACK caveat:** enabling auto-ACK (`RF_cmdIeeeRx.frameFiltOpt.autoAckEn`)
requires frame filtering to be turned on too (`frameFiltEn=1`) — the radio has
to decide a frame is "for us" before it can ACK it. So turning self-ack on
trades away full promiscuous capture while it's active.

\* **Reflexive jam caveat:** `jammer_on(method="reflexive")` repeatedly arms a
~1ms `CMD_IEEE_RX` listen window and, if it reports RX activity, aborts and
fires a short garbage burst on the same channel. This is a **software-loop**
reflex (on the order of hundreds of microseconds to a few ms of reaction
latency), not an RF-core-instant one — TI does not expose a lower-latency
hook for this over the public radio command API. Short frames may finish
before the jam lands; it is most effective against longer frames or ARQ
retransmissions.

## Sub-1GHz support (915 MHz US ISM, channels 1-10)

A second radio setup - **SUN O-QPSK, Rate Mode 0** (6.25 kbps, 100 kchip/s;
the mandatory/base rate per IEEE 802.15.4g, for broadest interoperability)
- alongside the native 2.4 GHz one, added in `kb_cc1354p10.syscfg` as
`RF_Settings_SUBG_OQPSK`. TI's own preset label marks this PHY **"Release
Candidate"** (see `cc1354p10_prop_pg20/param_syscfg.json` in the SDK) - not
a TI-validated production PHY, so treat capture quality/sensitivity with
appropriate caution; this has only been validated for whether the plumbing
works (no hangs, no crashes across a full 10-channel/20s-each scan), not
for genuine over-the-air decode accuracy against a real 915 MHz SUN O-QPSK
transmitter.

`SET_CHANNEL`'s payload grew a second, optional byte for this: `[channel]`
(legacy, always page 0) or `[channel][page]`, where page 0 = 2.4 GHz
(channel 11-26, unchanged) and page 31 = 915 MHz (channel 1-10) - page 31
matches KillerBee's own `KBCapabilities.FREQ_915` page number.
`GET_CHANNEL`'s reply grew to match: `[channel][page]`.

**Channel-to-frequency mapping** for the 915 MHz band follows the classic
IEEE 802.15.4-2006 US ISM band plan - channel *n* is `906 + 2*(n-1)` MHz
(906, 908, ..., 924 MHz) - **not** KillerBee's own `kbutils.py`
`frequency()` helper's page-31 formula, which uses a denser/different
spacing more suited to a generic SUN-PHY channel plan. Channels 1-10 on
the real, well-known standard channel numbering was the explicit target
this was built for; `kb.frequency(channel, page=31)` (used by tools like
`zbdump` to print a human-readable frequency banner) will report a
different, **not accurate**, frequency for this device as a result - the
mismatch is deliberate and documented, not an oversight, but worth knowing
if a printed banner frequency looks off.

**Switching bands is a real, deliberate risk, not a free operation.**
Unlike the 2.4 GHz-only channel changes, switching *between* page 0 and
page 31 tears down and rebuilds the entire RF driver connection
(`RF_close()`+`RF_open()` with a different radio setup) because the two
PHYs are fundamentally different RF core configurations. `RF_close()`
pends on the RF command queue internally - the same unreliable-blocking-
wait class of issue documented elsewhere in `main.c` (see `rfPostAndPoll()`
and `KB_CMD_RESET`'s comments) for other TI driver calls on this hardware.
This was an explicit, accepted tradeoff: a second, separate sub-1GHz-only
firmware image would have avoided the risk entirely, at the cost of
needing a reflash to switch bands. In testing (a full 10-channel scan plus
repeated band switches) this did not hang, but if it ever does, the same
recovery used throughout this project's bring-up works: a JTAG-level board
reset through the debug probe (see the RF-core-stuck troubleshooting
section above), no reflash needed.

**Not yet supported on the sub-1GHz band:** reflexive jamming
(`jammer_on(method="reflexive")` - its listen loop posts `CMD_IEEE_RX`
unconditionally and isn't band-aware; `startJammer()` explicitly rejects
it while on `BAND_SUBG` rather than silently misbehave) and self-ACK
(`SET_SELFACK` only takes effect through `CMD_IEEE_RX`'s frame-filter
options).

**The sub-1GHz PHY is meaningfully less reliable than 2.4GHz under
sustained use** - found by a full regression pass exercising sniff/inject/
jammer/reset on both bands plus repeated band switching. Two things here
were genuine firmware bugs and are fixed; a third is a hardware/SDK-level
reliability limit of the sub-1GHz radio path itself, not something this
firmware can fix.

- **Fixed:** `rfTransmitOnce()`'s sub-1GHz branch checked the TX command's
  completion status against the generic `DONE_OK` (0x0400), but
  `CMD_PROP_TX` completes with `PROP_DONE_OK` (0x3400) - a different exact
  value per `rf_prop_mailbox.h`. `rfPostAndPoll()` does an exact match, so
  every sub-1GHz `inject()` was rejected immediately (not hung - the
  status was already `>= 0x0400`, just not equal to the wrong constant
  being checked). Fixed by including `rf_prop_mailbox.h` and checking
  `PROP_DONE_OK`.
- **Fixed (by avoidance, not a real fix):** `RF_cancelCmd()` -
  `RF_abortCmd()` in the RF driver (`ti/drivers/rf/RFCC26X2_multiMode.c`,
  which - correction from an earlier draft of this doc - is open source on
  this SDK, not a black box) wraps everything in `Hwi_disable()` before
  calling driverlib's `RFCDoorbellSendTo()`
  (`ti/devices/.../driverlib/rfc.c`), which is a raw register spin loop
  waiting for the separate embedded CM0 RF-core co-processor to
  acknowledge the abort. `Hwi_disable()` masks interrupts globally,
  including the RTOS scheduler tick, so if the CM0 core never raises that
  flag, **the entire M33 application core freezes solid - verified
  directly by polling PING for 40s with zero external intervention and
  seeing no self-recovery.** No software timeout (a watchdog thread
  included - an earlier version of this fix tried exactly that and was
  dead code, since a timer interrupt can't fire while all interrupts are
  masked) can recover from this once entered. The only real fix is to
  never call `RF_cancelCmd()` for the specific case proven to trigger it:
  `stopJammer()` now calls `SysCtrlSystemReset()` directly instead of
  `rfJamStop()` when stopping constant-carrier jamming on `BAND_SUBG`,
  skipping the hazardous call entirely.
- **Not fixed - a sub-1GHz radio-path reliability limit, investigated in
  depth but not resolved:** further testing after the above fix found that
  UART communication actually breaks **as soon as sub-1GHz constant-carrier
  jamming starts**, not specifically when it's stopped - a plain
  `GET_CHANNEL` (no RF interaction at all) sent immediately after a
  successful `JAMMER_ON` ack already gets no reply, with no self-recovery.
  The identical test on the 2.4 GHz jammer works immediately and correctly.
  Separately, **repeated sub-1GHz transmissions** (e.g. `inject(...,
  count=N)`) can reproduce the same class of freeze.

  Real effort went into fixing this properly, not just documenting around
  it:
  - Compared this firmware's RF command usage against TI's own official
    reference examples for this exact board
    (`examples/nortos/LP_EM_CC1354P10_1/prop_rf/rfPacketTx` and
    `rfCarrierWave`, part of the installed SDK). TI's `rfPacketTx.c` calls
    `RF_yield(rfHandle)` after every single transmit, before preparing the
    next one - releasing the RF core's "client active" hold between posts.
    This firmware never did that anywhere. Added it to the `INJECT` TX
    loop to match. This is a genuine, correct improvement (it's what TI's
    own validated example does) and measurably changed the failure
    pattern - a repeated `count=3` test that failed 3/3 times before the
    change passed cleanly, with the board verified healthy afterward, on
    the first re-test. **However**, further testing (3 independent fresh
    reflashes, `count=3` each) showed it still fails most of the time -
    the failure became less deterministic, not eliminated. This fix is
    kept (it's correct per TI's own reference and doesn't regress anything
    that was working), but it should not be relied on as a resolution.
  - Searched TI's official documentation and e2e support forum. Found a
    real, TI-acknowledged thread specific to this exact chip:
    ["CC1354P10: RF transmit Hangs RF_runScheduleCmd() in
    CC1354P10"](https://e2e.ti.com/support/wireless-connectivity/sub-1-ghz-group/sub-1-ghz/f/sub-1-ghz-forum/1324353/cc1354p10-rf-transmit-hangs-rf_runschedulecmd-in-cc1354p10)
    (TI E2E, Sub-1 GHz forum) - another user hitting RF transmit hangs on
    this same device, described as one of the RF core's termination events
    simply never firing, leaving a blocking wait (there, `RF_pendCmd()`'s
    semaphore; here, `RFCDoorbellSendTo()`'s register spin) stuck forever.
    This independently confirms the failure class is a real, known
    characteristic of this chip's RF core - not something specific to or
    caused by this project's firmware. TI's own suggested diagnostic in
    that thread is reading the `RFCPEIFG` register
    (`RFC_DBELL_BASE + 0x10` = `0x40041010` on this device, per
    `hw_rfc_dbell.h`) to see the CPE's exact state during a hang.
  - Initially concluded that reading `RFCPEIFG` live during a hang was
    impossible, because any DSLite connection runs the target's GEL startup
    script, which resets the board on attach. Found a real way around this
    instead of giving up: the GEL script's own comment says the reset is
    gated by `if(!GEL_IsConnected())`, with an explicit note on how to skip
    it. Built a self-contained, scratchpad-only mirror of the relevant
    TI device-config/GEL files (symlinked back to the shared SDK install
    for everything *except* the one modified file, so nothing outside the
    project was touched) with that reset disabled, and used it to connect
    without resetting the target.
  - This actually worked and produced a real, surprising result: **a bare
    JTAG connect with no reset reliably restored UART communication after
    a reproduced hang**, when 15-40+ seconds of passive polling alone never
    did. ARM core fault registers (`CFSR`, `HFSR` at `0xE000ED28`/`0xE000ED2C`)
    read as `0` and `ICSR`'s `VECTACTIVE` field showed `0` (normal thread
    mode) during the hang - so the M33 core was not in any fault handler.
  - Reading `RFCPEIFG` itself (`0x40041010`) failed with a driver-level
    "Invalid parameter" error both during the hang and, it turned out,
    also just failed outright as a read target regardless of device state.
    A companion read of the RFC power-domain status bit (`PRCM.PDSTAT1`,
    `0x58082194`) showed the RF core reporting powered off during the hang
    - which looked like a strong lead (RF domain collapsing mid-transmit)
    and briefly drove the belief that the RTOS's default standby policy
    was letting the chip sleep while sub-1GHz TX was still logically
    active. Added a permanent `Power_setConstraint(PowerCC26XX_SB_DISALLOW)`
    + `PowerCC26XX_IDLE_PD_DISALLOW` at boot to rule this out - a real fix
    if it were the cause, and validated against TI's own reference: the
    installed SDK's `examples/rtos/LP_EM_CC1354P10_1/prop_rf/rfCarrierWave/
    tirtos7/main_tirtos.c` sets exactly these two constraints (gated behind
    a different board's config flag). **Tested directly and it did not
    help** - the identical jammer-on-then-`GET_CHANNEL` hang reproduced
    instantly, unchanged. The constraints are harmless (this board is
    always tethered, never battery-powered) and are kept as a reasonable
    default, but they are not the fix.
  - Went further to check whether the `RFC_ON=0` / `RFCPEIFG` read failure
    actually meant anything, rather than trusting it. Built and flashed
    TI's own **unmodified** `rfCarrierWave` example (from the SDK's
    `examples/rtos/LP_EM_CC1354P10_1/prop_rf/rfCarrierWave`, zero of this
    project's code involved) and took the same live register readings
    while it was running and, per its own design, continuously
    transmitting. **The exact same readings came back** - `RFCPEIFG`
    "Invalid parameter" and `RFC_ON=0` - on TI's own reference firmware,
    running normally. This means those specific readings are not a
    reliable signal of anything broken; they most likely reflect a
    limitation of reading power-domain-gated peripheral registers through
    a bare DSLite memory read (rather than a live, halted debug session
    with correct power-domain sequencing), not a genuine RF-core fault.
    **That theory and the `RFC_ON=0` evidence behind it are retracted.**
    The CPU fault-register findings (no fault, thread mode) are unaffected
    by this correction, since those are always-on core-debug registers,
    not power-domain-gated peripheral ones - so that part of the picture
    still stands.

  **Honest bottom line:** the sub-1GHz radio path on this chip has a real,
  reproducible reliability limitation under sustained TX activity, and a
  real, TI-acknowledged forum report of the same failure class exists for
  this exact chip - so this isn't specific to this project's firmware.
  Root cause is still not identified. Two genuine improvements were made
  and kept (`RF_yield()`, permanent standby/idle disallow) because they're
  correct per TI's own reference code, but neither fixed the underlying
  issue, and one promising lead (RF power domain collapsing) turned out to
  be a misread diagnostic, caught and retracted by cross-checking against
  TI's own unmodified reference firmware rather than left standing.
  Treat any sustained sub-1GHz TX activity (constant jam, or several
  back-to-back inject packets) as something that can still end the session
  and require the JTAG UART-resync step above to recover. A real fix, if
  one exists, most likely needs TI's direct engineering support or a live,
  halted JTAG/GDB debug session capable of correctly sequencing
  power-domain access to peripheral registers - neither of which this
  project has available.

## Wire protocol

921600 baud, 8N1, no flow control, no CRC (short USB-serial link, matches the
precedent of KillerBee's other text/binary serial backends).

```
Host -> Device:  [0xA5][CMD]      [LEN][LEN bytes payload]
Device -> Host:  [0xA5][CMD|0x80] [LEN][LEN bytes payload]   (reply to CMD)
                 [0xA5][0x90]     [LEN][LEN bytes payload]   (async RX frame)
```

| CMD  | Name             | Payload (host->device)                        | Reply payload |
|------|------------------|------------------------------------------------|---------------|
| 0x01 | PING             | —                                                | ASCII firmware ID, e.g. `KB-CC1354P10 v1.0` |
| 0x02 | GET_CHANNEL      | —                                                | `[channel][page]` |
| 0x03 | SET_CHANNEL      | `[channel]` or `[channel][page]` (page 0: 11-26, page 31: 1-10) | `[status]` |
| 0x04 | SNIFFER_ON       | —                                                | `[status]` |
| 0x05 | SNIFFER_OFF      | —                                                | `[status]` |
| 0x06 | INJECT           | `[count][delay_ms lo][delay_ms hi][frame...]`   | `[status]` |
| 0x07 | JAMMER_ON        | `[mode]` (0=constant carrier, 1=reflexive)      | `[status]` |
| 0x08 | JAMMER_OFF       | —                                                | `[status]` |
| 0x09 | SET_SELFACK      | `[enable]`                                      | `[status]` |
| 0x0A | RESET            | —                                                | `[status]` |
| 0x90 | (async) PACKET   | n/a — device-initiated                          | `[rssi int8][crc_ok u8][timestamp u32 LE][framelen u8][frame...]` |

`status`: `0x00` = OK, `0x01` = ERROR. `frame` is the PSDU including the
2-byte FCS as received/to-transmit is FCS-less (hardware computes it on TX,
and includes the real received FCS bytes on RX since `rxConfig.bIncludeCrc=1`).

**RESET triggers a full chip reboot** (`SysCtrlSystemReset()`), not just a
logical state reset - see the troubleshooting section below for why. The
`[status]` reply is sent immediately before the reboot, so it does arrive,
but the target chip (and therefore its UART) then goes away while it
reboots. The debug probe's own USB connection to the host is unaffected
(it's a separate USB device from the target chip), **but a full 2.4GHz +
sub-1GHz regression pass found the UART backchannel does *not* reliably
come back on its own** after the reboot when talking through the
standalone LP-XDS110 probe - PING got no response even after several
seconds of polling, and raw serial reads on the port started blocking past
their configured timeout. The same external JTAG-level board reset
described below reliably brings it back (~1-2s) and is required after
*every* full chip reboot, not just occasionally - this includes both an
explicit `RESET` command and the automatic hang-fail-safe reboot described
in "Sub-1GHz support" below. Earlier testing in this repo's history
suggested this reconnect wasn't needed; that turned out not to hold up
under a full, repeated test pass and the docs here are corrected
accordingly. Practically: a `KillerBee` session cannot recover from calling
`driver.reset()` (or from triggering the jammer-cancel fail-safe) purely
over the serial port - a debug probe and DSLite (or a physical power
cycle) are required to restore communication afterward.

## Building

Requires the TI toolchain already set up at `/opt/ti` (see `/opt/ti/ti-env.sh`):

```sh
source /opt/ti/ti-env.sh
cd firmware/src/kb-cc1354p10/tirtos7/ticlang
make
```

Produces `kb_cc1354p10.hex` (and `.out`/`.map`). SysConfig regenerates
`ti_radio_config.c/h`, `ti_drivers_config.c/h`, etc. from `../../kb_cc1354p10.syscfg`
automatically as part of the build — nothing to hand-edit there.

## Flashing

Using the standalone LP-XDS110 target configuration checked into this
directory (`CC1354P10_XDS110.ccxml`, configured for standard JTAG over the
probe's 20-pin connector):

```sh
/opt/ti/uniflash_sl/dslite.sh -c firmware/src/kb-cc1354p10/CC1354P10_XDS110.ccxml \
    firmware/src/kb-cc1354p10/tirtos7/ticlang/kb_cc1354p10.hex
```

(Note: `dslite.sh` already defaults to flash mode — don't pass a redundant
`flash` argument or it mis-parses and reports `Unable to open file: flash`.)

## Using it from KillerBee

```python
from killerbee import KillerBee

kb = KillerBee(device="/dev/ttyACM0", hardware="cc1354p10")
kb.set_channel(15)
kb.sniffer_on()
pkt = kb.pnext(timeout=2)
kb.inject(b"\x01\x02\x03...")
kb.driver.set_selfack(True)       # extra, non-standard KillerBee method
kb.jammer_on(method="reflexive")  # or method=None / "constant"
kb.jammer_off()

# 915 MHz US ISM band (page 31, channels 1-10) - see "Sub-1GHz support"
kb.set_channel(1, page=31)
kb.sniffer_on()
pkt = kb.pnext(timeout=2)
kb.set_channel(15)  # page defaults back to 0 (2.4 GHz)

kb.close()
```

Auto-detection also works (no `hardware=` needed) as long as
`DEV_ENABLE_CC1354P10` is `True` in `killerbee/config.py` (it is, by default) —
`KillerBee()` will probe serial devices with `kbutils.iscc1354p10()`.

## RF core can get stuck after heavy use - why RESET is a full reboot

Observed after heavy use (many jammer start/stop and channel-change cycles):
the RF core can end up in a state where RF commands like `SNIFFER_ON` start
returning `STATUS_ERROR` immediately, with `PING` and the UART link itself
still fine. Two things were tried and rejected before landing on the
current fix:

- A logical reset that only cancels known commands and clears local state
  (channel/sniffer/jammer flags) - does **not** clear it.
- `RF_close()` + `RF_open()` (the natural in-firmware equivalent of "power
  cycle the RF core") - `RF_close()` pends on the RF command queue
  internally, and that pend was observed to hang indefinitely on this
  hardware/SDK combination. This is the same unreliable-blocking-wait
  problem `rfPostAndPoll()` already works around for ordinary RF commands
  (see its comment in `main.c`) - it just resurfaces here through a TI
  driver call this firmware doesn't control the internals of.

What was found to reliably work, both externally and now built into the
firmware's own `RESET` command: a genuine full chip reset
(`SysCtrlSystemReset()`), which never returns and bypasses the RF driver's
shutdown path entirely. If you're on firmware old enough not to have this,
an external JTAG-level board reset through the debug probe has the same
effect and needs no reflash:

```sh
/opt/ti/uniflash_sl/dslite.sh --mode memory \
    -c firmware/src/kb-cc1354p10/CC1354P10_XDS110.ccxml -r 0x0,4 -o /tmp/discard.bin -e
```

(Any DSLite operation that connects and does its usual GEL-script board
reset works - this one is just a minimal, side-effect-free memory read
chosen for that reason. Note the `--mode memory` flag: `dslite.sh` selects
its mode via `--mode <name>`, not a bare positional argument - passing
`memory` as a positional arg is silently ignored and the tool falls back
to its default flash-mode help text, which looks like it worked but never
actually touches the target.)

If the debug probe itself stops responding entirely (`DSLite`/`xds110reset`
failing with `Error -261: Invalid response was received from the XDS110`,
even for an unrelated no-op like `--help`), that's the LP-XDS110's own
onboard firmware wedged, not the target - a USB bus reset
(`USBDEVFS_RESET`) and toggling DFU mode were not sufficient to clear it in
practice, but a real physical unplug/replug of the probe's USB cable to
the host was. The physical reset button on top of the XDS110 board only
resets the *target* chip and does not help with this class of fault.
