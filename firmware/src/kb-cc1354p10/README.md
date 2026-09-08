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

## Capabilities mapped to KillerBee

| KBCapabilities   | Supported | Notes |
|------------------|-----------|-------|
| SNIFF            | yes       | Promiscuous `CMD_IEEE_RX`, frame filtering off by default |
| SETCHAN          | yes       | Channels 11-26 (2.4 GHz) only — no Sub-1GHz support in this firmware |
| INJECT           | yes       | `CMD_IEEE_TX`, hardware auto-computes/appends the FCS |
| SELFACK          | yes*      | Extra `driver.set_selfack()` method — see caveat below; no generic KillerBee-level setter exists in this codebase for any device |
| PHYJAM           | yes       | Continuous `CMD_TX_TEST` (modulated PRBS-15 garbage) |
| PHYJAM_REFLEX    | yes*      | Best-effort software-loop reflex — see caveat below |
| SET_SYNC         | no        | The native IEEE 802.15.4 RX/TX commands use a fixed, standard O-QPSK preamble/SFD; there is no register here to reprogram it (unlike CC2420-style radios) |
| FREQ_2400        | yes       | |
| FREQ_900/863/868/870/915 | no | This firmware only configures the 2.4 GHz IEEE 802.15.4 PHY |
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
| 0x02 | GET_CHANNEL      | —                                                | `[channel]` |
| 0x03 | SET_CHANNEL      | `[channel]` (11-26)                             | `[status]` |
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
kb.close()
```

Auto-detection also works (no `hardware=` needed) as long as
`DEV_ENABLE_CC1354P10` is `True` in `killerbee/config.py` (it is, by default) —
`KillerBee()` will probe serial devices with `kbutils.iscc1354p10()`.

## Troubleshooting: SNIFFER_ON (or other RF commands) start returning ERROR

Observed after heavy use (many jammer start/stop and channel-change cycles):
the RF core can end up in a state where `KB_CMD_RESET` (the firmware's own
software reset, which only clears local channel/sniffer/jammer state) no
longer clears it, and commands that were working start returning
`STATUS_ERROR` immediately, with `PING` and the UART link itself still fine.
A full JTAG-level board reset through the debug probe clears it - no
reflash needed:

```sh
/opt/ti/uniflash_sl/deskdb/content/TICloudAgent/linux/ccs_base/DebugServer/bin/DSLite memory \
    -c firmware/src/kb-cc1354p10/CC1354P10_XDS110.ccxml -r 0x0,4 -o /tmp/discard.bin -e
```

(Any DSLite operation that connects and does its usual GEL-script board
reset works - this one is just a minimal, side-effect-free memory read
chosen for that reason.)
