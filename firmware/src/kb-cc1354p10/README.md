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
options). Constant-carrier jamming (`CMD_TX_TEST`) and plain sniff/inject
work on both bands.

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
but the target chip (and therefore its UART) briefly goes away while it
reboots. The debug probe's own USB connection to the host is unaffected
(it's a separate USB device from the target chip), so on Linux this has
been observed to *not* require replugging/reconnecting - just a short
pause before the target responds again.

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
/opt/ti/uniflash_sl/deskdb/content/TICloudAgent/linux/ccs_base/DebugServer/bin/DSLite memory \
    -c firmware/src/kb-cc1354p10/CC1354P10_XDS110.ccxml -r 0x0,4 -o /tmp/discard.bin -e
```

(Any DSLite operation that connects and does its usual GEL-script board
reset works - this one is just a minimal, side-effect-free memory read
chosen for that reason.)
