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

As of stage 4, it also supports the 915 MHz US ISM band (channels 0-128)
via a second, runtime-switchable radio setup - see "Sub-1GHz support"
below.

## Capabilities mapped to KillerBee

| KBCapabilities   | Supported | Notes |
|------------------|-----------|-------|
| SNIFF            | yes       | Promiscuous `CMD_IEEE_RX` (2.4GHz) / `CMD_PROP_RX` (915MHz), frame filtering off by default |
| SETCHAN          | yes       | Channels 11-26 (2.4 GHz, page 0), 0-128 (915 MHz US ISM, page 31), and 0-65 (863-876 MHz EU/UK, page 28 - CC1354P10 only) |
| INJECT           | yes       | `CMD_IEEE_TX` (2.4GHz) / `CMD_PROP_TX` (sub-1GHz, both pages), hardware auto-computes/appends the FCS |
| SELFACK          | yes*      | Extra `driver.set_selfack()` method — see caveat below; no generic KillerBee-level setter exists in this codebase for any device. Both bands - hardware auto-ACK on 2.4GHz, software reflex on sub-1GHz (no hardware ACK support in `CMD_PROP_RX` - see "Sub-1GHz support") |
| PHYJAM           | yes       | Continuous `CMD_TX_TEST` (modulated PRBS-15 garbage) - PHY-agnostic, works on either band |
| PHYJAM_REFLEX    | yes*      | Best-effort software-loop reflex — see caveat below. Both bands - see "Sub-1GHz support" for the sub-1GHz-specific implementation notes |
| PHYJAM_HOP       | yes       | `CMD_JAM_HOP_ON` - on-chip rotating jam across a cross-page sub-1GHz channel list (page 31/28), no host round trip per hop - see "On-chip channel-hop jamming" below. Sub-1GHz only on this firmware (2.4GHz on the CC1352P7 firmware instead) |
| SET_SYNC         | no        | The native IEEE 802.15.4 RX/TX commands use a fixed, standard O-QPSK preamble/SFD; there is no register here to reprogram it (unlike CC2420-style radios) |
| FREQ_2400        | yes       | |
| FREQ_915         | yes       | 915 MHz US ISM, channels 0-128 - see "Sub-1GHz support" below |
| FREQ_863         | yes       | 863-876 MHz EU/UK, channels 0-65, CC1354P10 only - see "Page 28 support" below. Project-local channel plan, does not match `kbutils.py`'s existing generic page-28 formula (written for older Silabs hardware) - see that section for why |
| FREQ_900/868/870 | no        | Not configured in this firmware |
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

## Sub-1GHz support (915 MHz US ISM, channels 0-128)

A second radio setup - **SUN O-QPSK, Rate Mode 0** (6.25 kbps, 100 kchip/s;
the mandatory/base rate per IEEE 802.15.4g, for broadest interoperability)
- alongside the native 2.4 GHz one, added in `kb_cc1354p10.syscfg` as
`RF_Settings_SUBG_OQPSK`. TI's own preset label marks this PHY **"Release
Candidate"** (see `cc1354p10_prop_pg20/param_syscfg.json` in the SDK) - not
a TI-validated production PHY, so treat capture quality/sensitivity with
appropriate caution; this has only been validated for whether the plumbing
works (no hangs, no crashes across a full scan), not for genuine
over-the-air decode accuracy against a real 915 MHz SUN O-QPSK transmitter.

`SET_CHANNEL`'s payload grew a second, optional byte for this: `[channel]`
(legacy, always page 0) or `[channel][page]`, where page 0 = 2.4 GHz
(channel 11-26, unchanged) and page 31 = 915 MHz (channel 0-128) - page 31
matches KillerBee's own `KBCapabilities.FREQ_915` page number.
`GET_CHANNEL`'s reply grew to match: `[channel][page]`.

**Channel-to-frequency mapping - corrected after initially shipping the
wrong one.** The first version of this feature used the classic IEEE
802.15.4-2006 O-QPSK channel plan (channel *n* = `906 + 2*(n-1)` MHz,
channels 1-10) - a real, standard channel plan, just for the **wrong PHY**.
This firmware runs SUN O-QPSK (IEEE 802.15.4g, folded into
802.15.4-2015/2020), which defines its own, different channel plan for the
902-928 MHz US band. Caught via user review, not internal testing - a fair
correction, and worth being explicit that the earlier plan was simply
wrong, not a simplification.

Verified this time directly against TI's own source, not assumed: the
installed SDK ships `ti154stack`, TI's actual IEEE 802.15.4g/SUN protocol
stack implementation, and its `high_level/mac_pib.h` hard-codes the real
values for this exact rate mode (TI calls it "5KBPS_915MHZ", the same one
SysConfig's radioconfig tool calls `qpsk6kbpsrm0`/Rate Mode 0):

```
MAC_5KBPS_915MHZ_BAND_MODE_1_CENTER_FREQ_KHZ  = 902200   (902.2 MHz)
MAC_5KBPS_915MHZ_BAND_MODE_1_CHAN_SPACING_KHZ = 200      (0.2 MHz)
MAC_5KBPS_915MHZ_BAND_MODE_1_TOTAL_CHANNELS   = 129      (channels 0-128)
```

So: **channel *n* (0-128) = `902.2 + 0.2*n` MHz**, spanning 902.2-927.8 MHz
- 129 real, standard channel numbers a genuine SUN/802.15.4g device would
actually use (channel 64 lands on exactly 915.0 MHz, a handy sanity check).
Sub-MHz precision needs `CMD_FS`'s `fractFreq` field (a 16-bit fraction of
1 MHz: actual tuned frequency = `frequency + fractFreq/65536` MHz) rather
than the plain integer-MHz `frequency` field alone, which is all the
earlier, wrong 2 MHz-spaced plan ever needed - verified against TI's own
SysConfig radioconfig code generator
(`ti/devices/radioconfig/.meta/cmd_handler.js`), including replicating its
rounding to the synth's native ~51.2 step size for full fidelity.

This still does **not** match KillerBee's own `kbutils.py` `frequency()`
helper's page-31 formula, which uses yet another, different, generic
spacing not specific to this PHY - `kb.frequency(channel, page=31)` (used
by tools like `zbdump` to print a human-readable frequency banner) will
still report an inaccurate frequency for this device. That divergence is
still deliberate and documented, unlike the channel-plan bug above, which
was a real, corrected mistake.

The generic cross-device `KBCapabilities.is_valid_channel()` check in
`killerbee/kbutils.py` also needed updating - its page-31 case had a
hard-coded `channel > 26` upper bound (fine for the old, wrong 1-10 plan,
but it silently rejected valid channels once the real 0-128 range was
implemented). Raised to `channel > 128`; each device's own driver still
does the authoritative, tighter check for its actual hardware regardless,
so this is a defense-in-depth first-pass filter, not the sole gate.

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

**Reflexive jamming and self-ACK are both supported on the sub-1GHz band**,
implemented after the brown-out fix above (they'd have been unusable
before it - both do sustained TX/RX cycling, exactly the pattern that
triggered the brown-out at the old TX power). Real, working, tested
implementations, not just capability flags left on:

- **Reflexive jamming** (`jammer_on(method="reflexive")`): `reflexJamThread()`
  is band-aware - posts `CMD_PROP_RX` for the listen window on `BAND_SUBG`
  instead of `CMD_IEEE_RX`, and (unlike the 2.4 GHz path, where
  `CMD_IEEE_RX.channel` carries its own tuning) does an explicit one-time
  `rfTuneToChannel()` before the loop starts, since `CMD_PROP_RX` has no
  channel field of its own - missing this was a real bug caught during
  testing (first attempt hung immediately; the RF synth was left on
  whatever frequency it was previously at). `rxCallback()`'s existing
  band-aware append-byte parsing (already needed for plain sniffing) just
  works here too, no changes needed. Tested: 10+ seconds of sustained
  reflexive jamming, clean start and stop, no reboot needed on stop
  (constant-carrier jamming's stop path used to force a reboot too - see
  the brown-out writeup below for that whole story, including the later
  fix that removed it).
- **Self-ACK** (`SET_SELFACK` / `driver.set_selfack()`): a genuine hardware
  limitation, not a gap - `CMD_PROP_RX`/`CMD_PROP_RX_ADV` (checked in
  `rf_prop_cmd.h`) have no auto-ACK field anywhere, unlike `CMD_IEEE_RX`'s
  `frameFiltOpt.autoAckEn`, which the RF core services entirely in
  hardware, in parallel with ongoing capture. There's no sub-1GHz
  equivalent hardware path on this chip. Implemented instead as a software
  reflex (`selfAckSubgThread()`): a dedicated thread posts `CMD_PROP_RX`
  indefinitely (as close to continuous as the plain sniffer, not the
  jammer's short bursts) into the same shared capture ring `pnext()`
  already reads from - normal capture keeps working unchanged. When a
  frame lands with the IEEE 802.15.4 Ack Request bit set (frame control
  byte 0, bit 5) and a valid CRC, the ongoing RX is briefly cancelled (safe
  for `CMD_PROP_RX` - the doorbell-spin hazard documented elsewhere in this
  file was specific to cancelling `CMD_TX_TEST`, verified extensively
  through sniffer testing all session), a 3-byte immediate ACK (FCF
  `0x02 0x00` + the frame's sequence number) is sent, and RX is re-armed.
  This costs a real cancel+TX+re-arm round trip per ACK (not
  hardware-instant like 2.4 GHz), and acks any request with no destination
  address filtering - matching the level of address-awareness this
  firmware's 2.4 GHz self-ACK already has, since no local address/PAN ID is
  configured anywhere in this file for either band. Tested for mechanical
  soundness (start/stop stability, sustained operation, mid-sniff toggling,
  repeated cycling - all clean, no hangs) but **not validated against a
  real over-the-air ACK-requesting transmitter** - no second sub-1GHz
  device was available this session to confirm a real node actually
  recognizes the ACK format as valid and stops retransmitting. Treat the
  mechanism as sound and the frame format as correct per the 802.15.4
  spec, but genuinely unverified end-to-end, same caveat as the PHY
  preset's decode accuracy elsewhere in this document.
- **Ambient RSSI** (`GET_RSSI` / `driver.get_rssi()`): direct RF-core
  energy sampling via TI's `RF_getRssi()` API
  (`RFCC26X2_multiMode.c`) - a single `RF_runDirectImmediateCmd()` call
  (`CMDR_DIR_CMD(CMD_GET_RSSI)`), decoding bits `[23:16]` of the raw
  status as a signed dBm value. Deliberately minimal by design: no new
  `RF_postCmd`, no state changes - it only reads a live value off
  whatever RX operation (plain sniff or the self-ACK thread's `CMD_PROP_RX`)
  is already running, reusing the extensively-tested continuous RX path
  rather than adding new RF command posting logic, consistent with this
  firmware's general bias toward minimal new RF operations given the
  brown-out history documented below. Requires `SNIFFER_ON` (or self-ACK)
  to already be active; with no RX operation running it returns TI's own
  documented error sentinel, `RF_GET_RSSI_ERROR_VAL = -128` (see
  `RFCC26X2.h`) - `driver.get_rssi()` maps this to `None` rather than
  passing the raw, misleading -128 through. Unlike per-packet RSSI (only
  available when something is actually captured), this reports real
  channel energy even on a channel with zero decodable 802.15.4 traffic -
  used by `tools/subg_scan.py` to distinguish a genuinely quiet channel
  from one with real RF energy but no decodable packets. Tested
  repeatedly on real hardware: correct `-128` with no RX active, and
  stable, physically plausible ambient readings (~-109 to -120 dBm, the
  expected noise floor) across dozens of consecutive calls while
  `SNIFFER_ON`, no hangs or instability.

**The sub-1GHz PHY was meaningfully less reliable than 2.4GHz under
sustained use, traced to a real hardware brown-out reset and now
mitigated** - found by a full regression pass exercising sniff/inject/
jammer/reset on both bands plus repeated band switching, then root-caused
via a live JTAG debug session down to the chip's own hardware reset-cause
register (`AON_PMCTL.RESETCTL` reading `RESET_SRC=VDDS_LOSS`) during a
reproduced failure - see the detailed writeup below for the full
investigation, including two dead ends that were built up with real
evidence and explicitly retracted after failing control tests, and the
working fix (reduced sub-1GHz TX power) that followed from the diagnosis.

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
  masked) can recover from this once entered. The fix at the time was to
  never call `RF_cancelCmd()` for the specific case proven to trigger it:
  `stopJammer()` called `SysCtrlSystemReset()` directly instead of
  `rfJamStop()` when stopping constant-carrier jamming on `BAND_SUBG`,
  skipping the hazardous call entirely. **Superseded below** once the
  actual brown-out root cause was found and fixed - see "Follow-up fix"
  further down, which restores the normal `rfJamStop()` path since the
  condition that made it hazardous is gone.
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
  - Also tried the specific idea (raised independently, matching a common
    TI troubleshooting recommendation) that `rfPostAndPoll()` polling
    `op->status` directly, instead of waiting on a real driver callback,
    might let the RF driver's own internal queue bookkeeping get raced
    ahead of - i.e. posting the next command before the driver's ISR has
    actually finished processing the previous command's completion event.
    Rewrote it to register a genuine `RF_EventLastCmdDone` callback and
    wait on a `sem_timedwait()`-bounded semaphore instead of polling.
    Tested directly against the reliable `count=3` sub-1GHz repro: **no
    change, hung identically.** Reverted to the simpler status-polling
    version, which is already validated across extensive 2.4 GHz testing
    this session and doesn't carry the added complexity for no benefit.

  - Got a genuine live, halted debug session working (something an earlier
    pass of this investigation said wasn't available) via TI's Debug Server
    Scripting (DSS, `ccs_base/scripting/run.sh` - a real JS API distinct
    from the plain DSLite CLI used everywhere else in this project:
    `session.target.connect()/halt()/run()`, `session.registers.read()`,
    `session.memory.readOne()`). Pointed it at the same no-reset scratchpad
    ccxml already built for the read-only register work above.
  - First capture during a reproduced hang: `PC=0x329a`, one instruction
    past a `wfi` inside `PowerCC26XX_standbyPolicy` (confirmed via
    disassembly - the specific branch taken there requires both
    `PowerCC26XX_DISALLOW_STANDBY` and `_DISALLOW_IDLE` to be set, i.e. the
    constraint added earlier in this investigation). `XPSR`'s exception
    field read `0` (Thread mode), independently corroborating the earlier
    `ICSR.VECTACTIVE=0` finding via a different register. This looked like
    strong confirmation of "CPU legitimately parked in WFI, waiting for an
    interrupt that never arrives" - consistent with why a bare debug attach
    (which can generate a wake event on this architecture) fixes it.
  - **Ran the identical halt-and-inspect against a healthy, idle board as a
    control - the same discipline that caught the earlier `RFC_ON`
    misdiagnosis - and it produced the exact same `PC=0x329a`.** A
    perfectly normal, unhung board looks identical at this level of
    inspection. That specific finding is retracted as diagnostic evidence
    for the same reason as before: it's what any idle CPU looks like, hang
    or not.
  - Added NVIC inspection (`ISER`/`ISPR`/`IABR` at `0xE000E100`-`0xE000E304`)
    to check enabled/pending/active state for the RF and UART interrupt
    lines (`RFC_CPE_0`=25, `RFC_CPE_1`=18, `RFC_HW_COMB`=26,
    `RFC_CMD_ACK`=27, `UART2_COMB`=56, `AON_RTC_COMB`=20, per
    `hw_ints.h`). A second hang capture showed something more striking -
    `PC` inside ROM (`0x10001366`, not this firmware's flash) with **every
    NVIC interrupt-enable bit cleared** - versus the healthy control's
    `ISER0=0x12000631` (several real interrupts enabled, `RFC_CPE_0`
    included). A CPU with zero enabled interrupts would explain why nothing
    could wake it. **Not treating this as confirmed root cause**, though:
    the two hang captures are inconsistent with each other (different PC,
    different NVIC state), and connecting via JTAG is itself established as
    capable of waking a WFI-parked CPU - so a `halt()` a few instructions
    into whatever ROM wake/power-reconfiguration routine that triggered,
    which commonly disable interrupts as their own internal critical
    section, is indistinguishable from a genuine "nothing was ever going to
    wake this" state using this tooling. This is a real methodological
    wall, not a lack of effort: getting a clean, undisturbed snapshot would
    need non-invasive execution tracing (an ETM trace probe), which isn't
    available here - every tool this project has requires an attach that's
    already shown itself capable of changing the outcome.

  - **Root cause found and confirmed: this is a hardware brown-out reset,
    not a software/RF-driver hang.** A diagnostic RAM trace ring buffer
    (written continuously during normal operation, readable via a plain
    JTAG memory read after a hang - unlike live register snapshots, this
    doesn't require disturbing the system to observe it, sidestepping the
    observation-changes-the-outcome problem above) showed the firmware's
    own application state reading as **pre-C-runtime-init defaults**
    during a hang (`channel=0`, not its real static initializer `11`;
    `traceSeq=0`, never incremented) with `PC` parked in TI's on-chip boot
    ROM (`0x10001366`/`0x10000891`, address range `0x10000000+`, not this
    firmware's flash) - reproduced identically twice. That signature means
    the chip had undergone an actual hardware reset and stalled very early
    in its own boot sequence, before the application ever ran again.
    Confirmed directly (not inferred) by reading `AON_PMCTL.RESETCTL`
    (`0x58090028`, `RESET_SRC` field at bits `[3:1]`) live during a third
    reproduction: **`RESET_SRC = 2 = VDDS_LOSS`** - a genuine, hardware-
    latched brown-out-on-the-main-supply-rail reset cause, recorded by the
    chip's own fault-latching circuit, read immediately after the hang and
    before any recovery action touched the board. The CC1354P10's
    integrated PA can hit +20dBm; the current step when sub-1GHz
    continuous-carrier TX keys up is a real, fast edge that this board's
    power delivery (LP_EM_CC1354P10_1 + standalone LP-XDS110, often
    powered over a jumper/ribbon connection) evidently can't sustain
    without the rail dipping below the brown-out threshold. This also
    explains why a bare JTAG attach (no target reset) reliably "fixes" a
    hang: the debug port forcing its own power domain active is plausibly
    exactly the missing signal the boot ROM's own stalled init sequence
    needed, not anything to do with waking a sleeping CPU as earlier,
    retracted theories in this document proposed.
  - This is a **hardware power-delivery issue, not a firmware bug** -
    nothing in this project's C code caused it or can reliably prevent it.
    It explains, after the fact, why every purely software-side fix tried
    above (status codes, `RF_yield()`, standby/idle constraints, callback-
    based command completion) never changed the outcome: none of them
    touch the actual cause. Mitigation belongs on the hardware side -
    powering the board from a clean bench supply rather than through the
    debug probe's jumper/ribbon path, and/or reducing sub-1GHz TX output
    power in `kb_cc1354p10.syscfg`'s PHY config, are the concrete next
    experiments, not yet tried in this project.

  - **Fix applied and empirically confirmed: lowering sub-1GHz TX power to
    0 dBm.** The PHY preset's TX power was never actually at the chip's
    extreme +20 dBm high-PA mode (`highPA` defaults `false` in SysConfig's
    radioconfig module) - it was already at a comparatively modest 12-14
    dBm default (`RF_TxPowerTable_CC13x4Sub1GHz_DEFAULT_PA_ENTRY`, not the
    high-PA table), yet still brown-out-reset the board. Set an explicit
    `txPower: "0"` in `kb_cc1354p10.syscfg`'s sub-1GHz PHY args - confirmed
    in the generated code as `TXSUB1_POWER_OVERRIDE(0x013297)`, the exact
    0 dBm table entry, replacing the previous `0x00BE33` (12 dBm) entry.
    Retested against every previously-reliable failure case: the
    jammer-on-then-immediate-`GET_CHANNEL` hang (100% reproducible before,
    at every power level and every other fix tried) now stays alive
    through 5+ full seconds of continuous transmission; the 3+
    back-to-back `inject()` hang (also 100% reproducible before `RF_yield`,
    still frequently failing after it) now passes cleanly up to `count=20`,
    across 5 repeated rounds (100 transmissions total, zero failures). This
    is the first change in the entire investigation that measurably,
    reliably fixed the symptom rather than reducing its frequency or
    failing outright - strong retroactive confirmation of the brown-out
    diagnosis. 2.4 GHz (already unaffected by any of this) re-verified
    unaffected by the change.
  - `DCDC_ACTIVE` in CCFG was checked too (`0` = DC/DC enabled during
    active/RF operation, already TI's documented default/recommended
    setting - `hw_ccfg.h`) and left as-is. Whether GLDO-only would respond
    faster to the PA's initial current edge than the DC/DC's inductor-based
    delivery is a genuine open tradeoff this project doesn't have enough
    data to resolve, and the TX-power fix already worked - no need to
    speculatively change a setting TI already recommends.
  - **Follow-up fix: stopping the sub-1GHz constant jammer no longer forces
    a reboot.** For a while after the TX-power fix above, `stopJammer()`
    still deliberately triggered `SysCtrlSystemReset()` for sub-1GHz
    constant-carrier jamming rather than risk `RF_cancelCmd()`'s
    `RFCDoorbellSendTo()` spin (see that function's comment) - a leftover
    of defense-in-depth caution, not a retested requirement. Revisited and
    fixed: since the brown-out (the RF core dying mid-command, unable to
    ack an abort) was the actual reason that spin was ever a risk, and the
    brown-out is now gone, the normal cancel-based stop was retested and
    found safe. Verified live, repeatedly: `JAMMER_ON(constant)` on
    sub-1GHz -> `JAMMER_OFF` -> `PING`, all clean, no reboot; and a full
    `SET_CHANNEL` rotation across 6 channels *while jamming stayed active*
    (`JAMMER_ON` once, then repeated `SET_CHANNEL` calls - the existing
    `KB_CMD_SET_CHANNEL` handler already stops/retunes/resumes whatever
    jam mode was running around a channel change, see its comment) - no
    hangs, no reboots, jamming followed every hop. This also fixed a
    latent double-reply protocol bug: the old `KB_CMD_JAMMER_OFF` handler
    sent an early "best-effort" status reply anticipating a dead UART link
    before this fix, which would have desynced the host's reply parser now
    that the reply actually arrives normally. `KB_CMD_RESET` is unrelated
    and still always reboots (that's its whole job).
  - **Still just a mitigation, not a fix to the board's actual power
    delivery:** 0 dBm was chosen as a conservative, clearly-safe-margin
    starting point, not precisely tuned to the actual brown-out threshold -
    it's plausible a higher power (e.g. 5-10 dBm) would also be safe on
    this specific board, were someone to characterize the actual margin
    (e.g. with a bench supply and a scope on the rail, per the suggested
    next steps above). Sub-1GHz range/link budget is correspondingly
    reduced from the preset's original default. If a board with a less
    conservative power margin regresses on the now-un-forced stop path,
    the previous always-reboot `stopJammer()` behavior is the right first
    response - see git history for that version.

  **Bottom line:** the sub-1GHz radio's brown-out-reset problem, confirmed
  via the chip's own hardware reset-cause register, is now mitigated by
  reducing TX power - retested extensively and no longer reproduces under
  any of the failure cases that were 100% reliable before. This remains a
  firmware-side mitigation for a hardware power-delivery limitation, not a
  fix to the underlying limitation itself.

## Page 28 support (863-876 MHz EU/UK, channels 0-65, CC1354P10 only)

Added as `stage 5` (see the file header comment). Reuses the exact same
SUN O-QPSK Rate Mode 0 radio setup as page 31 above - no new
`kb_cc1354p10.syscfg` radio config, no new RF driver setup - `rfSwitchBand()`
treats page 28 and page 31 as the same `BAND_SUBG`, only `rfTuneToChannel()`
picks a different frequency formula based on `currentPage`. Deliberately
**CC1354P10-only**, not added to the CC1352P7 firmware or to the generic
`KBCapabilities`/`kbutils.py` page-28 handling (see below for why).

**Channel plan:** channel *n* (0-65) = `863.0 + 0.2*n` MHz, 66 channels
spanning 863-876 MHz end to end (channel 65 lands on exactly 876.0 MHz).
Deliberately reuses page 31's exact 0.2 MHz channel spacing - TI's own
real SUN O-QPSK Rate Mode 0 spacing at 915 MHz, not an arbitrary choice -
for finer frequency resolution and more accurate scanning than an earlier
version of this same page, which used a coarser 27-channel/0.5 MHz plan
(see git history). Channel *numbering* is still **project-local**, not a
real external standard's - unlike page 31's plan (which also mirrors TI's
own SUN/802.15.4g channel count for its band), no single standard channel
plan covers this specific 863-876 MHz span with exactly 66 channels, so
this firmware picked a clean, full-range sweep at the same resolution
instead of trying to match one.

**Deliberately does not match `killerbee/kbutils.py`'s existing, generic
page-28 `KBCapabilities.frequency()` formula.** That generic helper already
had a page-28 case before this firmware existed - written for older
Silabs hardware (`dev_sl_beehive.py`/`dev_sl_nodetest.py`, both real
`FREQ_863`-capable devices), with its own different formula (`863.25 +
0.2*ch` MHz, spanning only ~5.2 MHz). `kb.frequency(channel, page=28)`
will still report the wrong (Silabs) frequency for this device, same
caveat as page 31's `frequency()` divergence above - `rfTuneToChannel()`
does not call or share it at all.

**`is_valid_channel()` needed a real fix, not just a bypass, once this
went from 27 to 66 channels.** `KillerBee.set_channel()` (the top-level
method every tool actually calls, not `driver.set_channel()` directly)
gates every call through `KBCapabilities.is_valid_channel()` *before* the
driver ever sees it - discovered the hard way when `subg_scan.py -p 28`
hit channel 27 and got rejected with `ValueError` despite
`dev_cc1354p10.py`'s own range check already correctly allowing 0-65.
Simply raising `is_valid_channel()`'s shared page-28 upper bound from 26
to 65 was **not** safe: `dev_sl_beehive.py`/`dev_sl_nodetest.py` pack the
channel into a 5-bit protocol field (`channel & 0x1f`) and would silently
*wrap*, not error, above channel 31 (channel 65 would send as channel 1).
Fixed instead with a new, additive `KBCapabilities.FREQ_863_WIDE` flag -
only this firmware sets it, `is_valid_channel()`'s page-28 case checks it
first and allows 0-65 only when set, otherwise falls back to the original
`FREQ_863`/channel-26 bound unchanged. Zero behavior change for existing
`FREQ_863` hardware.

**Same band-switch cost/risk as page 0<->31** - switching between page 0
(2.4 GHz) and page 28 tears down and rebuilds the RF driver connection
exactly like page 0<->31 does (see "Switching bands is a real, deliberate
risk" above); switching *between* page 28 and page 31 does not (`BAND_SUBG`
in both cases), just a fast `CMD_FS` retune.

Not yet hardware-validated against real over-the-air 863-876 MHz traffic -
only that `SET_CHANNEL`/scanning/jamming complete without hanging, the
same "plumbing works" bar page 31 was held to before its own
validation. The `LP-EM-CC1354P10` boosterpack's antenna matching network
was very likely tuned for 915 MHz (US ISM), not this range - real-world
range/sensitivity here may be materially worse than at 915 MHz even though
the channel plan and RF core setup are both correct; that would show up as
weak/noisy capture, not as a wrong frequency.

## On-chip channel-hop jamming (`KBCapabilities.PHYJAM_HOP`, sub-1GHz only)

`CMD_JAM_HOP_ON` (`0x0C`) starts a constant-carrier jam that rotates across
a host-supplied, **cross-page** sub-1GHz channel list (page 31 and/or page
28) entirely inside the firmware - `jamHopThread()` in `main.c` loops
calling the same `rfTuneToChannel()`/`rfJamStart()`/`rfJamStop()` primitives
`SET_CHANNEL` already uses, with no host round trip per hop. Ported from
the CC1352P7 firmware's same-named, 2.4GHz-only version (see that
firmware's README for the ~30ms-per-host-round-trip measurement that
motivates this) - **deliberately not offered on 2.4GHz/page 0 on this
firmware**, sub-1GHz only.

**Each hop entry is an inclusive `[page][chStart][chEnd]` range, not a
bare channel** - `payload = [dwell_ms lo][dwell_ms hi][rangeCount]
[page0][chStart0][chEnd0]...[page(n-1)][chStart(n-1)][chEnd(n-1)]`, each
range either `(31, 0<=start<=end<=128)` or `(28, 0<=start<=end<=65)`. Two
real design differences from the CC1352P7 version:

1. **Ranges, not one entry per channel.** The outer wire protocol's
   single-byte payload `LEN` field caps any command's payload at 255
   bytes - a 3-byte header plus 2 bytes per explicit `[page][channel]`
   hop (the very first version of this command) tops out around ~126
   hops. A real, useful request - e.g. page 31 channels 9-128 plus page
   28 channels 9-65, 177 channels total - genuinely exceeds that with an
   explicit list, but both spans are contiguous, so encoding each as one
   `[page][chStart][chEnd]` range (3 bytes, regardless of how many
   channels it spans) sidesteps the ceiling entirely. A scattered,
   non-contiguous request still works fine - it just becomes several
   length-1 ranges (`chStart == chEnd`), at the same per-entry cost as
   the old explicit-list format. `dev_cc1354p10.py`'s `jam_hop_on()`
   does this collapsing (`_collapse_to_ranges()`) on the host side before
   sending - the firmware only ever sees already-collapsed ranges, and
   `tools/subg_jam_hop.py` needed no changes at all to gain this.
2. **Each range carries its own page, not a bare channel span** - page 31
   and page 28 share the exact same `BAND_SUBG` radio setup while using
   *different* `rfTuneToChannel()` frequency formulas at the same raw
   channel number (channel 20 is valid on both pages, at very different
   real frequencies: 906.2 MHz on page 31, 867.0 MHz on page 28), so a
   bare channel span would be ambiguous - same reasoning as the
   CC1352P7 version's single-page `[page][channel]` pairs, just extended
   to a range's start/end.

**A real bug this surfaced and fixed:** `rfTuneToChannel()`'s tune cache
(`lastTuneValid`/`lastTunedChannel`) only ever compared the raw channel
number, never the page - the exact same class of bug `KB_CMD_SET_CHANNEL`
already needed fixing for (see the "Page 28 support" section above).
`jamHopThread()` explicitly invalidates `lastTuneValid` whenever the
*page* changes between consecutive hops, even when the raw channel number
happens to differ too (which would have masked the bug by accident) -
without this, two hops landing on the same raw channel number across a
page change (e.g. ...->page 31 ch 20 -> page 28 ch 20->...) would have
silently skipped the retune, jamming the wrong frequency. Also fixed a
smaller, cosmetic-but-real gap: `jamHopThread()` (both here and in the
CC1352P7 firmware) never updated the shared `channel` global, so
`GET_CHANNEL` issued mid-hop reported a stale channel from before hopping
started even though `currentPage` was correct - now updated on every hop.

Python: `kb.jam_hop_on([(31, 9), (31, 14), (28, 10), ...], dwell_ms=20)` -
a flat `(page, channel)` list, same shape as before; `jam_hop_on()`
collapses it into ranges internally. Stop with `kb.jammer_off()` - the
same command that stops every other jam mode. CLI: `tools/subg_jam_hop.py`,
using the same `--page-channels PAGE:CHANNELS` syntax as
`tools/subg_jam.py`'s cross-page host-driven version (channel specs
accept `-` ranges, e.g. `9-128`):

```sh
python3 tools/subg_jam_hop.py -i /dev/cu.usbmodemLS4501DC1 --dwell 0.02 \
    --page-channels 31:9,14,15,19,20,24,106 \
    --page-channels 28:10,12,20,41,51,57

# A large contiguous request - 177 channels total, collapses to just 2
# wire-protocol ranges (2 * 3 = 6 payload bytes), impossible to express
# as an explicit per-channel list under the 255-byte payload ceiling:
python3 tools/subg_jam_hop.py -i /dev/cu.usbmodemLS4501DC1 --dwell 0.02 \
    --page-channels 31:9-128 \
    --page-channels 28:9-65
```

**Hardware-validated with real RF evidence, not just command-level
success.** Command-level: `JAM_HOP_ON` accepted for both a 13-entry
scattered cross-page list and the 177-channel/2-range contiguous request
above, device stays fully responsive to `PING`/`GET_CHANNEL` while hopping
(`GET_CHANNEL` mid-hop correctly reported real in-progress channel/page
values from the requested span, including the exact boundary channel 128
on page 31, confirmed with a deterministic single-channel-range hop, not
just statistical sampling luck), `JAMMER_OFF` stops it cleanly, and all
three invalid-payload cases (page 0, out-of-range channel for page 28,
out-of-range channel for page 31) correctly rejected with `STATUS_ERROR`.
Also verified the 177-channel run's `GET_CHANNEL` samples span almost the
entire requested range on both pages (page 28: the full 9-65; page 31:
9-127 directly sampled, 128 confirmed separately as above) over repeated
polling across a full ~3.5s hop cycle, ruling out silent truncation.
Beyond that: verified with an independent RF energy check using a second
board (CC1352P7) doing ambient RSSI sampling (`GET_RSSI` while sniffing)
on each page-31 target channel while this one hopped the (13-entry)
cross-page list at 20ms dwell - every page-31 channel in the list showed a
clear spike (~-77 to -87 dBm) against a much quieter ~-105 to -119 dBm
baseline on channels *not* in the list, confirming real, on-air hopping.

**The page-28 gap this left open has since been closed**: the CC1352P7
firmware gained its own page 28 support (ported verbatim from this one)
specifically so it could serve as an independent RF monitor here too. With
this firmware hopping page 28 channels 9-65, the CC1352P7's `GET_RSSI`
showed the same kind of clear, repeatable spike (~-62 to -64 dBm) on every
sampled in-range channel (9, 20, 30, 40, 50, 65 - the full range including
both endpoints) against a much quieter baseline on channels outside the
jammed range - see `../kb-cc1352p7/README.md`'s own "On-chip channel-hop
jamming" section for the full writeup.

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
| 0x03 | SET_CHANNEL      | `[channel]` or `[channel][page]` (page 0: 11-26, page 31: 0-128, page 28: 0-65) | `[status]` |
| 0x04 | SNIFFER_ON       | —                                                | `[status]` |
| 0x05 | SNIFFER_OFF      | —                                                | `[status]` |
| 0x06 | INJECT           | `[count][delay_ms lo][delay_ms hi][frame...]`   | `[status]` |
| 0x07 | JAMMER_ON        | `[mode]` (0=constant carrier, 1=reflexive)      | `[status]` |
| 0x08 | JAMMER_OFF       | —                                                | `[status]` |
| 0x09 | SET_SELFACK      | `[enable]`                                      | `[status]` |
| 0x0A | RESET            | —                                                | `[status]` |
| 0x0B | GET_RSSI         | —                                                | `[rssi int8]` |
| 0x0C | JAM_HOP_ON       | `[dwell_ms lo][dwell_ms hi][rangeCount][page0][chStart0][chEnd0]...[page(n-1)][chStart(n-1)][chEnd(n-1)]` (sub-1GHz only: each range (31, 0<=start<=end<=128) or (28, 0<=start<=end<=65)) | `[status]` |
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

# 915 MHz US ISM band (page 31, channels 0-128) - see "Sub-1GHz support"
kb.set_channel(64, page=31)  # 902.2 + 0.2*64 = 915.0 MHz exactly
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

## CMD_INJECT exhausts the RF driver's command pool after repeated calls

Root-caused on real hardware: every `CMD_INJECT` posts at least one RF
command (`CMD_IEEE_TX`/`CMD_PROP_TX`, via `rfPostAndPoll()`) and, before
this was fixed, an unconditional channel re-tune (`CMD_FS`) as well, even
when already tuned to the requested channel. `rfPostAndPoll()` polls the
command's raw `.status` field directly and never calls `RF_pendCmd()` -
deliberately, per the header comment above `rfPostAndPoll()`: that call was
observed to hang indefinitely on this hardware/SDK combination, even
called *after* the command had already completed (confirmed again during
this investigation - see below). But `RF_pendCmd()` (or the completion
callback it drives) is also the only thing that reclaims a slot in the TI
RF driver's fixed 8-entry command pool (`N_CMD_POOL` in
`RFCC26X2_multiMode.c`). Never calling it means every `rfPostAndPoll()`
call permanently leaks one pool slot - so before the fix, `CMD_INJECT`
(2 posts each: retune + TX) hard-failed with `STATUS_ERROR` after exactly
4 real over-the-air injects, and every other RF command after that, with
`PING`/`GET_CHANNEL` still responsive throughout (not the same failure
class as the RESET-related wedge above - no JTAG nudge needed to tell
them apart, but one is needed to clear either).

Tried and rejected: calling `RF_pendCmd(rfHandle, h, 0)` immediately after
`rfPostAndPoll()`'s own poll already confirms the command is terminal.
Per the TI driver's documented contract, a call against an
already-finished command should just return `RF_EventLastCmdDone`
immediately rather than block. Tested directly on this hardware anyway:
it hangs the same way, every time - the whole UART command loop wedges
(same symptom as the RESET issue above, same JTAG-nudge recovery). So the
documented "no-op on a finished command" contract doesn't hold here,
consistent with `RF_pendCmd()` already being off the table for this
firmware for the same underlying reason.

First shipped as a partial mitigation: `rfTuneToChannel()` caches the last
channel it actually tuned to and skips the `CMD_FS` post entirely when
asked to retune to the same channel (invalidated on band switch, where the
same channel number means a different frequency). This doesn't reclaim any
pool slots - it just avoids wasting one on redundant retunes - but it
halved `CMD_INJECT`'s cost from 2 posts to 1 for the common repeated-same-
channel case, doubling the number of real over-the-air injects available
per boot/JTAG-nudge from 4 to 8. Kept - it's a real, free efficiency win
regardless of the fix below.

**Actual fix, found afterward:** `RF_cancelCmd(rfHandle, h, 0)` called
right after `rfPostAndPoll()`'s own poll confirms the command is terminal
- i.e. the same call already used lower down on the genuine-timeout path,
just also applied to the normal-completion path. Per the TI driver's docs
this "has no effect" on an already-finished command, same wording that
turned out to be false for `RF_pendCmd()` above - but tested directly on
real hardware, `RF_cancelCmd()` behaves differently: no hang, and it does
reclaim the pool slot. Confirmed with 100 sequential real over-the-air
injects with zero failures (was capped at 8), repeated on both the
CC1354P10 and CC1352P7, with no timing regression (~30-40ms/call
throughout, no growth). This looks like a full fix rather than a bigger
but still-finite budget - worth staying skeptical of until it's seen more
runtime, but nothing in this session's testing contradicts that.
