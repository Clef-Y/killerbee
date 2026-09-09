/* KillerBee firmware for the TI CC1354P10.
 *
 * Implements the full KB wire protocol (see README.md) over
 * CONFIG_UART2_KB, driving the RF core directly with raw
 * CMD_IEEE_RX/CMD_IEEE_TX/CMD_TX_TEST commands (no ti154stack MAC).
 *
 * Brought up incrementally on real hardware:
 *   stage 1: UART framing + dispatch for PING/GET_CHANNEL/SET_CHANNEL/
 *   RESET, no RF. Hardware-validated.
 *   stage 2: + SNIFFER_ON/OFF - promiscuous CMD_IEEE_RX chained into a
 *   queue, async 0x90 packet forwarding to the host. Hardware-validated.
 *   stage 3: + INJECT (CMD_IEEE_TX), JAMMER_ON/OFF (constant CMD_TX_TEST
 *   and a reflexive listen/burst software loop), and SET_SELFACK.
 *   Hardware-validated (INJECT single/multi-frame + invalid-payload
 *   rejection; JAMMER constant and reflexive start/stop, stays responsive
 *   while active; SET_SELFACK enable/disable both idle and while
 *   sniffing). The actual over-the-air auto-ACK behavior of SET_SELFACK
 *   has not been observed - that needs a second radio sending frames
 *   addressed to this device, not just command-level round-tripping.
 *
 * All RF commands are posted with RF_postCmd() and waited on by polling
 * the command's own .status field (rfPostAndPoll()) rather than a blocking
 * RF_pendCmd(..., RF_EventLastCmdDone): on this hardware/setup that
 * blocking wait was observed to hang indefinitely even after the command
 * had already completed successfully, wedging the whole UART command loop.
 *
 * KB_CMD_RESET triggers a genuine full chip reset (SysCtrlSystemReset())
 * rather than just clearing local state: after heavy use the RF core has
 * been observed to get stuck (SNIFFER_ON etc. start returning
 * STATUS_ERROR immediately) in a way that RF_close()+RF_open() doesn't
 * reliably clear either - RF_close() pends on the RF command queue
 * internally, hitting the same unreliable-blocking-wait class of issue
 * rfPostAndPoll() exists to work around, just via a TI driver call this
 * firmware doesn't control the internals of. See README.md.
 *
 * stage 4: + sub-1GHz (915 MHz US ISM, KillerBee page 31, channels 1-10)
 * alongside the native 2.4 GHz PHY, via a second radio setup (SUN O-QPSK
 * Rate Mode 0, CMD_PROP_RX/CMD_PROP_TX) that SET_CHANNEL's new page byte
 * switches to at runtime with RF_close()+RF_open() - the exact operation
 * called out above as an unreliable blocking wait on this hardware. This
 * was a deliberate, explicit tradeoff (see git history): a second,
 * separate sub-1GHz-only firmware image would have avoided the risk
 * entirely, at the cost of needing a reflash to switch bands. Not yet
 * hardware-validated.
 */

#include <stdint.h>
#include <math.h>
#include <stdbool.h>
#include <string.h>
#include <unistd.h>
#include <pthread.h>
#include <semaphore.h>

#include <ti/drivers/UART2.h>
#include <ti/drivers/GPIO.h>
#include <ti/drivers/rf/RF.h>
#include <ti/drivers/Power.h>
#include <ti/drivers/power/PowerCC26XX.h>

#include <ti/devices/DeviceFamily.h>
#include DeviceFamily_constructPath(driverlib/rf_mailbox.h)
#include DeviceFamily_constructPath(driverlib/rf_common_cmd.h)
#include DeviceFamily_constructPath(driverlib/rf_ieee_cmd.h)
#include DeviceFamily_constructPath(driverlib/rf_ieee_mailbox.h)
#include DeviceFamily_constructPath(driverlib/rf_prop_cmd.h)
#include DeviceFamily_constructPath(driverlib/rf_prop_mailbox.h)
#include DeviceFamily_constructPath(driverlib/sys_ctrl.h)

#include "ti_drivers_config.h"
#include "ti_radio_config.h"
#include "RFQueue.h"

/* ti_radio_config.h exposes the *_ieee154 suffixed globals; alias here so
 * the rest of this file can use the command names from the README's
 * protocol table. */
#define RF_cmdIeeeRx RF_cmdIeeeRx_ieee154
#define RF_cmdIeeeTx RF_cmdIeeeTx_ieee154
#define RF_cmdFs     RF_cmdFs_ieee154

/* Sub-1GHz (915 MHz SUN O-QPSK) globals - see kb_cc1354p10.syscfg's
 * RF_Settings_SUBG_OQPSK comment for why this PHY was chosen. */
#define RF_propSubg           RF_prop_qpsk6kbpsrm0_1
#define RF_cmdPropRadioSetup  RF_cmdPropRadioDivSetup_qpsk6kbpsrm0_1
#define RF_cmdFsSubg          RF_cmdFs_qpsk6kbpsrm0_1
#define RF_cmdPropTx          RF_cmdPropTx_qpsk6kbpsrm0_1
#define RF_cmdPropRx          RF_cmdPropRx_qpsk6kbpsrm0_1

#define KB_SOF              0xA5

#define KB_CMD_PING            0x01
#define KB_CMD_GET_CHANNEL     0x02
#define KB_CMD_SET_CHANNEL     0x03
#define KB_CMD_SNIFFER_ON      0x04
#define KB_CMD_SNIFFER_OFF     0x05
#define KB_CMD_INJECT          0x06
#define KB_CMD_JAMMER_ON       0x07
#define KB_CMD_JAMMER_OFF      0x08
#define KB_CMD_SET_SELFACK     0x09
#define KB_CMD_RESET           0x0A
#define KB_CMD_GET_RSSI        0x0B

#define CMD_REPLY_BIT       0x80
#define CMD_ASYNC_PACKET    0x90

#define STATUS_OK           0x00
#define STATUS_ERROR        0x01

static const char FW_ID[] = "KB-CC1354P10 v1.0";

/* ==================== UART framing / TX serialization ==================== */

static UART2_Handle uart;
static pthread_mutex_t uartTxLock;

static void uartWriteLocked(const void *buf, size_t len)
{
    size_t written;
    pthread_mutex_lock(&uartTxLock);
    UART2_write(uart, buf, len, &written);
    pthread_mutex_unlock(&uartTxLock);
}

static void sendReply(uint8_t cmd, const uint8_t *payload, uint8_t len)
{
    /* One locked write of header+payload so a concurrent async packet
     * from rfForwardThread can't interleave between them. */
    uint8_t frame[3 + 255];
    frame[0] = KB_SOF;
    frame[1] = (uint8_t)(cmd | CMD_REPLY_BIT);
    frame[2] = len;
    if (len > 0) {
        memcpy(frame + 3, payload, len);
    }
    uartWriteLocked(frame, (size_t)3 + len);
}

static void sendStatus(uint8_t cmd, uint8_t status)
{
    sendReply(cmd, &status, 1);
}

static bool readExact(uint8_t *buf, size_t n)
{
    size_t got = 0;
    while (got < n) {
        size_t chunk = 0;
        int_fast16_t rc = UART2_read(uart, buf + got, n - got, &chunk);
        if (rc != UART2_STATUS_SUCCESS) {
            return false;
        }
        got += chunk;
    }
    return true;
}

/* ==================== Diagnostic trace ring buffer ====================
 * Written continuously during normal operation, read via a plain JTAG
 * memory read (no halt needed) after a hang - unlike live register/PC
 * snapshots, this doesn't require disturbing the system to observe it, so
 * it isn't confounded by the JTAG-attach-itself-changes-state problem
 * documented in the README. Symbol addresses: `tiarmnm kb_cc1354p10.out
 * | grep trace` after building. */
#define TRACE_LOG_SIZE 64

typedef struct {
    uint32_t seq;
    uint8_t event;
    uint8_t data;
    uint16_t reserved;
} TraceEntry;

static volatile TraceEntry traceLog[TRACE_LOG_SIZE];
static volatile uint8_t traceIdx = 0;
static volatile uint32_t traceSeq = 0;

#define TR_CMD_RX            0x01  /* data = cmd byte */
#define TR_JAMMER_ON_REQ     0x02  /* data = mode (0=constant,1=reflexive) */
#define TR_JAMSTART_TUNED    0x03  /* data = 1 ok / 0 fail */
#define TR_JAMSTART_POSTED   0x04  /* data = 1 ok / 0 RF_ALLOC_ERROR */
#define TR_INJECT_TX_BEGIN   0x05  /* data = iteration index i */
#define TR_INJECT_TX_POSTED  0x06  /* data = 1 ok / 0 fail */
#define TR_INJECT_YIELDED    0x07  /* data = iteration index i */
#define TR_SET_CHANNEL_REQ   0x08  /* data = page */
#define TR_BAND_SWITCH_DONE  0x09  /* data = targetBand */
#define TR_UART_READEXACT_OK 0x0A  /* data = n bytes requested */

static void trace(uint8_t event, uint8_t data)
{
    uint8_t idx = traceIdx;
    traceIdx = (uint8_t)((idx + 1) % TRACE_LOG_SIZE);
    traceLog[idx].seq = traceSeq++;
    traceLog[idx].event = event;
    traceLog[idx].data = data;
}

/* ==================== RF / sniffer state ==================== */

#define IEEE_MAX_PSDU       127   /* max 802.15.4 PHY payload, incl. 2-byte FCS */
#define NUM_APPENDED_BYTES  6     /* RSSI(1) + CorrCrc(1) + Timestamp(4) */
#define NUM_RX_ENTRIES      2

static uint8_t rxDataEntryBuffer[RF_QUEUE_DATA_ENTRY_BUFFER_SIZE(
        NUM_RX_ENTRIES, IEEE_MAX_PSDU, NUM_APPENDED_BYTES)]
        __attribute__((aligned(4)));
static dataQueue_t rxDataQueue;
static rfc_ieeeRxOutput_t rxStatistics;

static RF_Object rfObject;
static RF_Handle rfHandle;
static RF_CmdHandle sniffCmdHandle = RF_ALLOC_ERROR;

/* BAND_24GHZ = native IEEE 802.15.4 (KillerBee page 0, channels 11-26).
 * BAND_SUBG = 915 MHz SUN O-QPSK (KillerBee page 31, channels 1-10). See
 * rfSwitchBand() for the RF_close()/RF_open() transition between them. */
#define BAND_24GHZ  0
#define BAND_SUBG   1
static uint8_t currentBand = BAND_24GHZ;
static uint8_t currentPage = 0;

static uint8_t channel = 11;
static bool snifferOn = false;
static bool jammerOn = false;
static uint8_t jamMode = 0; /* JAM_MODE_CONSTANT/JAM_MODE_REFLEXIVE, defined below */
static bool selfAckEnabled = false;

/* Captured-frame handoff ring: filled from the RF callback (Swi context,
 * must stay fast/non-blocking), drained by rfForwardThread which does the
 * actual (blocking) UART write. */
#define CAP_RING_SLOTS  4
typedef struct {
    int8_t rssi;
    bool crcOk;
    uint32_t timestamp;
    uint8_t frameLen;
    uint8_t frame[IEEE_MAX_PSDU];
} CapturedFrame;

static CapturedFrame capRing[CAP_RING_SLOTS];
static volatile uint8_t capHead = 0; /* next slot to write (producer: callback) */
static volatile uint8_t capTail = 0; /* next slot to read (consumer: fwd thread) */
static sem_t capSem;

static void rxCallback(RF_Handle h, RF_CmdHandle ch, RF_EventMask e)
{
    if (!(e & RF_EventRxEntryDone)) {
        return;
    }

    rfc_dataEntryGeneral_t *entry = RFQueue_getDataEntry();
    uint8_t totalLen = *(uint8_t *)(&entry->data);
    uint8_t *p = (uint8_t *)(&entry->data) + 1;

    if (totalLen >= NUM_APPENDED_BYTES) {
        uint8_t frameLen = (uint8_t)(totalLen - NUM_APPENDED_BYTES);
        if (frameLen > IEEE_MAX_PSDU) {
            frameLen = IEEE_MAX_PSDU;
        }

        uint8_t next = (uint8_t)((capHead + 1) % CAP_RING_SLOTS);
        if (next != capTail) { /* drop the frame if the ring is full */
            CapturedFrame *slot = &capRing[capHead];
            memcpy(slot->frame, p, frameLen);
            slot->frameLen = frameLen;
            slot->rssi = (int8_t)p[frameLen];
            if (currentBand == BAND_SUBG) {
                /* rxConf order for CMD_PROP_RX: RSSI(1), Timestamp(4),
                 * Status(1) - see rf_prop_cmd.h's rfc_CMD_PROP_RX_s.rxConf
                 * field order and rfc_propRxStatus_s (result bits 6:7,
                 * 0 = received correctly). */
                memcpy(&slot->timestamp, &p[frameLen + 1], 4);
                uint8_t status = p[frameLen + 5];
                slot->crcOk = ((status >> 6) & 0x3) == 0;
            } else {
                /* rxConfig order for CMD_IEEE_RX: RSSI(1), CorrCrc(1),
                 * Timestamp(4) - bit7 of CorrCrc is bCrcErr. */
                uint8_t corrCrc = p[frameLen + 1];
                slot->crcOk = (corrCrc & 0x80) == 0;
                memcpy(&slot->timestamp, &p[frameLen + 2], 4);
            }
            capHead = next;
            sem_post(&capSem);
        }
    }

    RFQueue_nextEntry();
}

static void *rfForwardThread(void *arg0)
{
    while (1) {
        sem_wait(&capSem);
        while (capTail != capHead) {
            CapturedFrame *slot = &capRing[capTail];

            uint8_t frame[3 + 7 + IEEE_MAX_PSDU];
            frame[0] = KB_SOF;
            frame[1] = CMD_ASYNC_PACKET;
            frame[2] = (uint8_t)(7 + slot->frameLen);
            frame[3] = (uint8_t)slot->rssi;
            frame[4] = slot->crcOk ? 1 : 0;
            memcpy(&frame[5], &slot->timestamp, 4);
            frame[9] = slot->frameLen;
            memcpy(&frame[10], slot->frame, slot->frameLen);

            uartWriteLocked(frame, (size_t)3 + 7 + slot->frameLen);

            capTail = (uint8_t)((capTail + 1) % CAP_RING_SLOTS);
        }
    }
}

/* Forward declaration - rfSniffStartSubg() needs rfTuneToChannel() (below)
 * since, unlike CMD_IEEE_RX, CMD_PROP_RX has no channel field of its own. */
static bool rfTuneToChannel(uint8_t ch);

/* Forward declaration - selfAckSubgThread() needs rfTransmitOnce() (below)
 * to send its ACK replies via the same CMD_PROP_TX path INJECT uses. */
static bool rfTransmitOnce(const uint8_t *frame, uint8_t len);

static bool rfSniffStartSubg(uint8_t ch)
{
    if (!rfTuneToChannel(ch)) {
        return false;
    }

    RF_cmdPropRx.pQueue = &rxDataQueue;
    RF_cmdPropRx.pOutput = NULL;
    RF_cmdPropRx.rxConf.bAppendRssi = 1;
    RF_cmdPropRx.rxConf.bAppendTimestamp = 1;
    RF_cmdPropRx.rxConf.bAppendStatus = 1;

    sniffCmdHandle = RF_postCmd(rfHandle, (RF_Op *)&RF_cmdPropRx,
                                 RF_PriorityNormal, rxCallback,
                                 RF_EventRxEntryDone);
    return sniffCmdHandle != RF_ALLOC_ERROR;
}

/* ==================== Sub-1GHz self-ACK ====================
 * CMD_PROP_RX/CMD_PROP_RX_ADV have no hardware auto-ACK field anywhere
 * (verified against rf_prop_cmd.h) - unlike CMD_IEEE_RX's
 * frameFiltOpt.autoAckEn, which the RF core services entirely in hardware,
 * in parallel with ongoing capture, with no CPU involvement per frame.
 * There is no equivalent hardware path for the sub-1GHz proprietary PHY on
 * this chip - this is a real command-set limitation, not a gap in this
 * firmware.
 *
 * What's implemented instead is a software reflex, structurally the same
 * as the existing reflexive jammer: a dedicated thread posts CMD_PROP_RX,
 * and - unlike the jammer - lets it run indefinitely (TRIG_NEVER, matching
 * the plain sniffer) so capture stays as close to continuous as the plain
 * sniffer's, rather than the jammer's short bursty windows. When a frame
 * lands in the shared capture ring (the same ring/rxCallback the plain
 * sniffer and pnext() already use - no separate capture path, so normal
 * frame capture keeps working exactly as before), it's inspected for the
 * IEEE 802.15.4 Ack Request bit (frame control byte 0, bit 5). If set and
 * the frame's CRC was valid, the ongoing RX is briefly cancelled (the same
 * RF_cancelCmd() already proven safe for CMD_PROP_RX across this entire
 * project's sniffer testing - the doorbell-spin hazard documented
 * elsewhere in this file was specific to cancelling CMD_TX_TEST, not
 * CMD_PROP_RX), a 3-byte immediate ACK frame (FCF 0x02 0x00 + the
 * original frame's sequence number) is sent via the existing
 * rfTransmitOnce() path, and RX is re-armed. This is NOT hardware-instant
 * like 2.4GHz's - it costs a real cancel+TX+re-arm round trip per ACK'd
 * frame (low-single-digit ms, generous relative to this PHY's 6.25 kbps
 * symbol rate and IEEE 802.15.4g's own more relaxed sub-1GHz ACK timing
 * budgets) - and it acks any frame requesting one, with no destination
 * address filtering, matching the level of address-awareness this
 * firmware's own 2.4GHz self-ACK already has (no explicit local
 * address/PAN ID is configured anywhere in this file for either band). */
static volatile bool selfAckSubgRunning = false;
static pthread_t selfAckSubgThreadHandle;
static bool selfAckSubgThreadValid = false;
static bool sniffIsSelfAckThread = false;

static void rfSendAckSubg(uint8_t seqNum)
{
    uint8_t ackFrame[3] = { 0x02, 0x00, seqNum };
    rfTransmitOnce(ackFrame, sizeof(ackFrame));
}

static void *selfAckSubgThread(void *arg0)
{
    rfTuneToChannel(channel);

    while (selfAckSubgRunning) {
        RF_cmdPropRx.pQueue = &rxDataQueue;
        RF_cmdPropRx.pOutput = NULL;
        RF_cmdPropRx.rxConf.bAppendRssi = 1;
        RF_cmdPropRx.rxConf.bAppendTimestamp = 1;
        RF_cmdPropRx.rxConf.bAppendStatus = 1;
        RF_cmdPropRx.startTrigger.triggerType = TRIG_NOW;
        RF_cmdPropRx.endTrigger.triggerType = TRIG_NEVER;

        RF_CmdHandle h = RF_postCmd(rfHandle, (RF_Op *)&RF_cmdPropRx,
                                     RF_PriorityNormal, rxCallback,
                                     RF_EventRxEntryDone);
        if (h == RF_ALLOC_ERROR) {
            break;
        }

        uint8_t capHeadBefore = capHead;
        while (selfAckSubgRunning && capHead == capHeadBefore) {
            usleep(500);
        }
        if (!selfAckSubgRunning) {
            RF_cancelCmd(rfHandle, h, 0);
            break;
        }

        RF_cancelCmd(rfHandle, h, 0);

        for (uint8_t i = capHeadBefore; i != capHead;
             i = (uint8_t)((i + 1) % CAP_RING_SLOTS)) {
            CapturedFrame *f = &capRing[i];
            if (f->crcOk && f->frameLen >= 3 && (f->frame[0] & 0x20)) {
                rfSendAckSubg(f->frame[2]);
            }
        }
    }
    return NULL;
}

static bool rfSniffStart(uint8_t ch)
{
    if (currentBand == BAND_SUBG) {
        if (selfAckEnabled) {
            selfAckSubgRunning = true;
            pthread_attr_t attrs;
            pthread_attr_init(&attrs);
            pthread_attr_setstacksize(&attrs, 1024);
            if (pthread_create(&selfAckSubgThreadHandle, &attrs,
                                selfAckSubgThread, NULL) != 0) {
                selfAckSubgRunning = false;
                return false;
            }
            selfAckSubgThreadValid = true;
            sniffIsSelfAckThread = true;
            return true;
        }
        sniffIsSelfAckThread = false;
        return rfSniffStartSubg(ch);
    }

    RF_cmdIeeeRx.channel = ch;
    RF_cmdIeeeRx.pRxQ = &rxDataQueue;
    RF_cmdIeeeRx.pOutput = &rxStatistics;
    RF_cmdIeeeRx.rxConfig.bIncludeCrc = 1;
    RF_cmdIeeeRx.rxConfig.bAppendRssi = 1;
    RF_cmdIeeeRx.rxConfig.bAppendCorrCrc = 1;
    RF_cmdIeeeRx.rxConfig.bAppendTimestamp = 1;
    /* Self-ACK requires frame filtering on - the radio must decide a frame
     * is "for us" before it can ACK it - which trades away full promiscuous
     * capture while self-ACK is active. See README.md self-ACK caveat. */
    RF_cmdIeeeRx.frameFiltOpt.frameFiltEn = selfAckEnabled ? 1 : 0;
    RF_cmdIeeeRx.frameFiltOpt.autoAckEn = selfAckEnabled ? 1 : 0;

    sniffCmdHandle = RF_postCmd(rfHandle, (RF_Op *)&RF_cmdIeeeRx,
                                 RF_PriorityNormal, rxCallback,
                                 RF_EventRxEntryDone);
    return sniffCmdHandle != RF_ALLOC_ERROR;
}

static void rfSniffStop(void)
{
    if (sniffIsSelfAckThread) {
        if (selfAckSubgThreadValid) {
            selfAckSubgRunning = false;
            pthread_join(selfAckSubgThreadHandle, NULL);
            selfAckSubgThreadValid = false;
        }
        sniffIsSelfAckThread = false;
        return;
    }
    if (sniffCmdHandle != RF_ALLOC_ERROR) {
        RF_cancelCmd(rfHandle, sniffCmdHandle, 0);
        sniffCmdHandle = RF_ALLOC_ERROR;
    }
}

/* ==================== TX / inject ==================== */

/* Bounded poll of a posted command's status field instead of an indefinite
 * RF_pendCmd(..., RF_EventLastCmdDone) wait: on this hardware/setup that
 * blocking wait was observed to hang forever after CMD_FS/CMD_IEEE_TX had
 * already completed successfully (status showed DONE_OK/IEEE_DONE_OK per
 * rf_mailbox.h/rf_ieee_mailbox.h), so polling the status field directly is
 * both simpler and avoids ever wedging the UART command loop even if a
 * future command genuinely never finishes. `okStatus` is the command
 * family's own "done ok" code (generic DONE_OK for CMD_FS/CMD_TX_TEST,
 * IEEE_DONE_OK for CMD_IEEE_TX/CMD_IEEE_RX). */
/* Tried a variant of this function that registers a real
 * RF_EventLastCmdDone callback + bounded sem_timedwait() instead of
 * polling op->status directly, on the theory that the driver's internal
 * queue bookkeeping might need its own ISR-driven event processing before
 * it's safe to post the next command, and that polling the status field
 * directly might race ahead of that. Tested directly against the
 * reliable count=3 sub-1GHz repro: no improvement, hung identically.
 * Reverted to plain status polling - simpler, and already validated
 * across extensive 2.4GHz testing this session. See README/git history. */
static bool rfPostAndPoll(RF_Op *op, uint16_t okStatus)
{
    RF_CmdHandle h = RF_postCmd(rfHandle, op, RF_PriorityNormal, NULL, 0);
    if (h == RF_ALLOC_ERROR) {
        return false;
    }

    for (uint32_t waitedUs = 0; waitedUs < 200000; waitedUs += 1000) {
        uint16_t st = op->status;
        if (st >= 0x0400) {
            return (st == okStatus);
        }
        usleep(1000);
    }

    RF_cancelCmd(rfHandle, h, 0);
    return false;
}

/* Neither CMD_IEEE_TX/CMD_PROP_TX nor CMD_TX_TEST carries its own channel
 * field - all transmit (and, for BAND_SUBG, CMD_PROP_RX also receives) on
 * whatever frequency the synth is currently tuned to, so an explicit
 * CMD_FS is required before any of them, regardless of prior state.
 *
 * Channel-to-frequency mapping for BAND_SUBG: an earlier version of this
 * function used the classic IEEE 802.15.4-2006 O-QPSK band plan (channels
 * 1-10, 906 + 2*(ch-1) MHz, 2 MHz spacing) - wrong for the PHY actually in
 * use here. This firmware runs SUN O-QPSK Rate Mode 0 (IEEE 802.15.4g,
 * folded into 802.15.4-2015/2020), which has its own, different, real
 * standard channel plan for the 902-928 MHz US band - verified directly
 * against TI's own ti154stack (their real IEEE 802.15.4g/SUN protocol
 * stack implementation, source-available in the installed SDK) rather
 * than assumed:
 *   ti/ti154stack/high_level/mac_pib.h:
 *     MAC_5KBPS_915MHZ_BAND_MODE_1_CENTER_FREQ_KHZ  = 902200
 *     MAC_5KBPS_915MHZ_BAND_MODE_1_CHAN_SPACING_KHZ = 200
 *     MAC_5KBPS_915MHZ_BAND_MODE_1_TOTAL_CHANNELS   = 129
 * ("5KBPS_915MHZ" is TI's own name for this exact rate mode - the same
 * one SysConfig's radioconfig tool calls "qpsk6kbpsrm0"/Rate Mode 0).
 * So: channel n (0-128) -> 902.2 + 0.2*n MHz, spanning 902.2-927.8 MHz -
 * these are the real, standard channel numbers a real SUN/802.15.4g
 * device would use, not firmware-invented ones. Sub-MHz precision needs
 * CMD_FS's fractFreq field (frequency = integer MHz, fractFreq = the
 * fractional part as a 16-bit fraction of 1 MHz, i.e. actual tuned
 * frequency = frequency + fractFreq/65536 MHz - verified against TI's own
 * SysConfig radioconfig code generator,
 * ti/devices/radioconfig/.meta/cmd_handler.js, which computes exactly
 * this and additionally rounds to the nearest multiple of 51.2 to match
 * the synth's native step size, replicated here for the same fidelity).
 *
 * KillerBee's own kbutils.py page-31 frequency() helper uses yet another,
 * different formula - this mapping intentionally does not match it either;
 * see README.md's Sub-1GHz support section for that caveat, still true. */
static bool rfTuneToChannel(uint8_t ch)
{
    if (currentBand == BAND_SUBG) {
        /* Floating point (this core has an FPU; this runs once per channel
         * change, not a hot loop) to mirror TI's own conversion exactly
         * rather than approximate it with integer rounding tricks. */
        double freqMHz = (902200.0 + 200.0 * (double)ch) / 1000.0;
        double intPart = floor(freqMHz);
        double fractRaw = (freqMHz - intPart) * 65536.0;
        double fractCmd = ceil(round(fractRaw / 51.2) * 51.2);

        RF_cmdFsSubg.frequency = (uint16_t)intPart;
        RF_cmdFsSubg.fractFreq = (uint16_t)fractCmd;
        return rfPostAndPoll((RF_Op *)&RF_cmdFsSubg, DONE_OK);
    }

    RF_cmdFs.frequency = (uint16_t)(2405 + 5 * (ch - 11));
    RF_cmdFs.fractFreq = 0;
    return rfPostAndPoll((RF_Op *)&RF_cmdFs, DONE_OK);
}

static bool rfTransmitOnce(const uint8_t *frame, uint8_t len)
{
    if (currentBand == BAND_SUBG) {
        /* CMD_PROP_TX completes with PROP_DONE_OK (0x3400), not the
         * generic DONE_OK (0x0400) - rfPostAndPoll() checks status for an
         * exact match, so using the wrong constant here made every
         * sub-1GHz inject() fail immediately (not hang) despite the
         * transmit actually completing on the wire. */
        RF_cmdPropTx.pktLen = len;
        RF_cmdPropTx.pPkt = (uint8_t *)frame;
        return rfPostAndPoll((RF_Op *)&RF_cmdPropTx, PROP_DONE_OK);
    }

    RF_cmdIeeeTx.payloadLen = len;
    RF_cmdIeeeTx.pPayload = (uint8_t *)frame;
    return rfPostAndPoll((RF_Op *)&RF_cmdIeeeTx, IEEE_DONE_OK);
}

/* ==================== Jammer ==================== */

#define JAM_MODE_CONSTANT   0x00
#define JAM_MODE_REFLEXIVE  0x01

/* HISTORICAL, now fixed - kept because the diagnosis is worth keeping
 * nearby to stopJammer()'s comment: sub-1GHz constant-carrier jamming used
 * to break UART communication as soon as it STARTED (a plain GET_CHANNEL
 * sent right after a successful sub-1GHz JAMMER_ON ack got no reply, no
 * self-recovery even polled 40s). Root-caused to a hardware brown-out
 * (VDDS_LOSS, confirmed via AON_PMCTL.RESETCTL - see README.md) from the
 * PA's current inrush on TX key-up, not a software/UART-link problem -
 * fixed by reducing sub-1GHz TX power to 0 dBm in kb_cc1354p10.syscfg.
 * Retested live post-fix: JAMMER_ON(constant) on sub-1GHz followed
 * immediately by GET_CHANNEL/PING now gets clean, correct replies, and
 * stays alive through 5+ seconds of continuous transmission. See
 * stopJammer()'s comment for the corresponding stop-path fix. */
static rfc_CMD_TX_TEST_t rfCmdTxTest;
static RF_CmdHandle jamCmdHandle = RF_ALLOC_ERROR;

static bool rfJamStart(uint8_t ch)
{
    if (!rfTuneToChannel(ch)) {
        trace(TR_JAMSTART_TUNED, 0);
        return false;
    }
    trace(TR_JAMSTART_TUNED, 1);

    memset(&rfCmdTxTest, 0, sizeof(rfCmdTxTest));
    rfCmdTxTest.commandNo = CMD_TX_TEST;
    rfCmdTxTest.startTrigger.triggerType = TRIG_NOW;
    rfCmdTxTest.condition.rule = 1;
    rfCmdTxTest.config.bUseCw = 0;      /* modulated, not a pure carrier */
    rfCmdTxTest.config.bFsOff = 0;
    rfCmdTxTest.config.whitenMode = 2;  /* PRBS-15 garbage, per README */
    rfCmdTxTest.txWord = 0xABCD;
    rfCmdTxTest.endTrigger.triggerType = TRIG_NEVER; /* runs until cancelled */
    rfCmdTxTest.syncWord = 0x930B51DE;

    jamCmdHandle = RF_postCmd(rfHandle, (RF_Op *)&rfCmdTxTest, RF_PriorityNormal, NULL, 0);
    trace(TR_JAMSTART_POSTED, jamCmdHandle != RF_ALLOC_ERROR);
    return jamCmdHandle != RF_ALLOC_ERROR;
}

/* An earlier version of this function tried to bound RF_cancelCmd() with a
 * watchdog thread + sem_timedwait(), on the theory it might hang like
 * RF_close() does elsewhere in this file. That "fix" was dead code: reading
 * the actual (open-source, on this SDK) RF driver
 * (ti/drivers/rf/RFCC26X2_multiMode.c) shows RF_cancelCmd() -> RF_abortCmd()
 * wraps everything in Hwi_disable() before calling driverlib's
 * RFCDoorbellSendTo() (ti/devices/.../driverlib/rfc.c), which is a raw
 * register spin loop:
 *
 *   while(HWREG(RFC_DBELL_BASE + RFC_DBELL_O_CMDR) != 0);
 *   ...
 *   while(!HWREG(RFC_DBELL_BASE + RFC_DBELL_O_RFACKIFG));
 *
 * waiting for the separate embedded CM0 RF-core co-processor to acknowledge
 * the abort. Hwi_disable() masks interrupts globally, including the RTOS
 * scheduler tick, so if that CM0 core never raises the ack flag, the entire
 * M33 application core freezes solid - no thread (a watchdog included) can
 * ever be scheduled to notice or recover. Verified directly: triggering
 * this via the sub-1GHz constant jammer's stop path and polling PING for
 * 40s with zero external intervention showed no self-recovery at all.
 *
 * There is no software fix for a hang already in progress inside that
 * spin - the only real fix is to never enter it for the specific case
 * proven to trigger it. See stopJammer() below. */
static void rfJamStop(void)
{
    if (jamCmdHandle != RF_ALLOC_ERROR) {
        RF_cancelCmd(rfHandle, jamCmdHandle, 0);
        jamCmdHandle = RF_ALLOC_ERROR;
    }
}

/* Reflexive jam: a software-loop reflex, not RF-core-instant - see the
 * README's reflexive jam caveat. RAT runs at 4 MHz (4 ticks/us). */
#define REFLEX_LISTEN_TICKS  4000  /* ~1ms listen window */
#define REFLEX_BURST_TICKS   2000  /* ~0.5ms garbage burst */

static volatile bool reflexRunning = false;
static pthread_t reflexThreadHandle;
static bool reflexThreadValid = false;

static void rfReflexBurst(void)
{
    memset(&rfCmdTxTest, 0, sizeof(rfCmdTxTest));
    rfCmdTxTest.commandNo = CMD_TX_TEST;
    rfCmdTxTest.startTrigger.triggerType = TRIG_NOW;
    rfCmdTxTest.condition.rule = 1;
    rfCmdTxTest.config.bUseCw = 0;
    rfCmdTxTest.config.bFsOff = 0;
    rfCmdTxTest.config.whitenMode = 2;
    rfCmdTxTest.txWord = 0xABCD;
    rfCmdTxTest.endTrigger.triggerType = TRIG_REL_START;
    rfCmdTxTest.endTime = REFLEX_BURST_TICKS;
    rfCmdTxTest.syncWord = 0x930B51DE;

    rfPostAndPoll((RF_Op *)&rfCmdTxTest, DONE_OK);
}

static void *reflexJamThread(void *arg0)
{
    if (currentBand == BAND_SUBG) {
        /* Unlike CMD_IEEE_RX (which carries its own .channel field, set
         * per-iteration below), CMD_PROP_RX has no channel field - the
         * synth needs an explicit CMD_FS tune first, same as every other
         * sub-1GHz RF op in this file (see rfTuneToChannel()'s comment).
         * Missing this left the synth on whatever frequency it was
         * previously at - a real correctness bug, not just a hang risk. */
        rfTuneToChannel(channel);
    }

    while (reflexRunning) {
        /* Arm a short promiscuous listen window on the current channel and
         * band. rxCallback() (see its own comment) already branches on
         * currentBand to correctly parse the append-bytes for either
         * command family, so no change needed there - only which RX
         * command gets posted, and which struct's .status field gets
         * polled, differ here. */
        volatile uint16_t *statusField;
        RF_CmdHandle h;
        uint8_t capHeadBefore = capHead;

        if (currentBand == BAND_SUBG) {
            RF_cmdPropRx.pQueue = &rxDataQueue;
            RF_cmdPropRx.pOutput = NULL;
            RF_cmdPropRx.rxConf.bAppendRssi = 1;
            RF_cmdPropRx.rxConf.bAppendTimestamp = 1;
            RF_cmdPropRx.rxConf.bAppendStatus = 1;
            RF_cmdPropRx.startTrigger.triggerType = TRIG_NOW;
            RF_cmdPropRx.endTrigger.triggerType = TRIG_REL_START;
            RF_cmdPropRx.endTime = REFLEX_LISTEN_TICKS;

            h = RF_postCmd(rfHandle, (RF_Op *)&RF_cmdPropRx,
                            RF_PriorityNormal, rxCallback,
                            RF_EventRxEntryDone);
            statusField = &RF_cmdPropRx.status;
        } else {
            RF_cmdIeeeRx.channel = channel;
            RF_cmdIeeeRx.pRxQ = &rxDataQueue;
            RF_cmdIeeeRx.pOutput = &rxStatistics;
            RF_cmdIeeeRx.rxConfig.bIncludeCrc = 1;
            RF_cmdIeeeRx.rxConfig.bAppendRssi = 1;
            RF_cmdIeeeRx.rxConfig.bAppendCorrCrc = 1;
            RF_cmdIeeeRx.rxConfig.bAppendTimestamp = 1;
            RF_cmdIeeeRx.frameFiltOpt.frameFiltEn = 0;
            RF_cmdIeeeRx.startTrigger.triggerType = TRIG_NOW;
            RF_cmdIeeeRx.endTrigger.triggerType = TRIG_REL_START;
            RF_cmdIeeeRx.endTime = REFLEX_LISTEN_TICKS;

            h = RF_postCmd(rfHandle, (RF_Op *)&RF_cmdIeeeRx,
                            RF_PriorityNormal, rxCallback,
                            RF_EventRxEntryDone);
            statusField = &RF_cmdIeeeRx.status;
        }

        if (h == RF_ALLOC_ERROR) {
            break;
        }

        /* Poll for the listen window to end rather than a blocking
         * RF_pendCmd() - see rfPostAndPoll()'s comment for why. The window
         * is ~1ms (REFLEX_LISTEN_TICKS), so give it a generous margin. */
        for (uint32_t waitedUs = 0; waitedUs < 5000; waitedUs += 200) {
            if (*statusField >= 0x0400) {
                break;
            }
            usleep(200);
        }
        if (*statusField < 0x0400) {
            RF_cancelCmd(rfHandle, h, 0);
        }

        if (capHead != capHeadBefore) {
            /* Activity seen within the window - fire the reflex burst.
             * Best-effort: hundreds of us to a few ms of reaction latency,
             * short frames may finish before the jam lands. */
            rfReflexBurst();
        }
        /* else: window timed out with nothing seen - loop and re-arm. */
    }
    return NULL;
}

/* Stops whichever jam mode is currently active.
 *
 * Constant-carrier jamming on the sub-1GHz PHY: the original hazard here
 * (UART communication going dead as soon as JAMMER_ON started, no
 * self-recovery - see the brown-out investigation in README.md) was root-
 * caused to a hardware brown-out (VDDS_LOSS, confirmed via
 * AON_PMCTL.RESETCTL) from the PA's current inrush on TX key-up, and fixed
 * by reducing sub-1GHz TX power to 0 dBm in kb_cc1354p10.syscfg. Retested
 * live on real hardware after that fix: JAMMER_ON(constant) on sub-1GHz
 * followed immediately by GET_CHANNEL/PING now gets clean, correct replies
 * (matching README's own "5+ full seconds of continuous transmission"
 * retest), where before it was 100% reproducible dead air. With the
 * brown-out gone, RF_cancelCmd()'s RFCDoorbellSendTo() spin (see
 * rfJamStop()'s comment) is no longer expected to hang either - it was the
 * RF core dying mid-command from the same power-rail dip that would have
 * left it unable to ack an abort request; a live core that never browned
 * out has no such reason to fail to ack. Verified directly:
 * JAMMER_ON(constant,ch9) -> JAMMER_OFF -> PING all succeeded with no
 * reboot, repeated across multiple channels via SET_CHANNEL while jamming
 * (see handleCommand's KB_CMD_SET_CHANNEL, which already stops/retunes/
 * resumes jam mode around a channel change). No longer forcing a reboot
 * here - if this regresses on a board with a less conservative power
 * margin than the one this was tested on, the forced-reboot fallback this
 * replaced is still the right first response; see git history. */
static void stopJammer(void)
{
    if (jamMode == JAM_MODE_REFLEXIVE) {
        if (reflexThreadValid) {
            reflexRunning = false;
            pthread_join(reflexThreadHandle, NULL);
            reflexThreadValid = false;
        }
    } else {
        rfJamStop();
    }
    jammerOn = false;
}

/* Starts jamming in the given mode; caller must ensure any previous
 * jammer/sniffer is already stopped. */
static bool startJammer(uint8_t mode, uint8_t ch)
{
    jamMode = mode;
    if (mode == JAM_MODE_REFLEXIVE) {
        /* reflexJamThread() is band-aware (posts CMD_PROP_RX or
         * CMD_IEEE_RX depending on currentBand - see its own comment). */
        reflexRunning = true;
        pthread_attr_t attrs;
        pthread_attr_init(&attrs);
        pthread_attr_setstacksize(&attrs, 1024);
        if (pthread_create(&reflexThreadHandle, &attrs, reflexJamThread, NULL) != 0) {
            reflexRunning = false;
            return false;
        }
        reflexThreadValid = true;
        return true;
    }
    return rfJamStart(ch);
}

/* ==================== Band switching (2.4 GHz <-> sub-1GHz) ==================== */

/* Switches the RF core between the native 2.4 GHz IEEE 802.15.4 setup and
 * the 915 MHz SUN O-QPSK setup via RF_close()+RF_open() - see this file's
 * header comment (stage 4) for why this specific operation was an
 * explicit, accepted risk rather than the safer separate-firmware
 * alternative. Caller must have already stopped any sniffer/jammer
 * (handleCommand's KB_CMD_SET_CHANNEL does this before calling in). */
static bool rfSwitchBand(uint8_t targetBand)
{
    if (targetBand == currentBand) {
        return true;
    }

    RF_close(rfHandle);

    RF_Params rfParams;
    RF_Params_init(&rfParams);
    if (targetBand == BAND_SUBG) {
        rfHandle = RF_open(&rfObject, &RF_propSubg,
                            (RF_RadioSetup *)&RF_cmdPropRadioSetup, &rfParams);
    } else {
        rfHandle = RF_open(&rfObject, &RF_prop_ieee154,
                            (RF_RadioSetup *)&RF_cmdRadioSetup_ieee154, &rfParams);
    }

    if (rfHandle == NULL) {
        /* Same fault convention as mainThread's own RF_open() failure
         * path - no working RF handle to serve any further RF command
         * with, so don't risk passing a NULL handle into RF_postCmd(). */
        while (1) {
            GPIO_toggle(CONFIG_GPIO_RLED);
            usleep(500000);
        }
    }

    currentBand = targetBand;
    trace(TR_BAND_SWITCH_DONE, targetBand);
    return true;
}

/* ==================== Command dispatch ==================== */

static void handleCommand(uint8_t cmd, const uint8_t *payload, uint8_t len)
{
    trace(TR_CMD_RX, cmd);
    switch (cmd) {
    case KB_CMD_PING:
        sendReply(cmd, (const uint8_t *)FW_ID, (uint8_t)(sizeof(FW_ID) - 1));
        break;

    case KB_CMD_GET_CHANNEL: {
        uint8_t reply[2] = { channel, currentPage };
        sendReply(cmd, reply, sizeof(reply));
        break;
    }

    case KB_CMD_SET_CHANNEL: {
        /* payload = [channel] (legacy, always page 0/2.4GHz) or
         * [channel][page] (page 0 = 2.4GHz 11-26, page 31 = 915MHz SUN
         * O-QPSK 1-10, matching KillerBee's own FREQ_915 page number). */
        if (len < 1 || len > 2) {
            sendStatus(cmd, STATUS_ERROR);
            break;
        }
        uint8_t newChannel = payload[0];
        uint8_t newPage = (len == 2) ? payload[1] : 0;
        trace(TR_SET_CHANNEL_REQ, newPage);
        /* page 31 range is 0-128 (129 channels) - the real SUN O-QPSK
         * Rate Mode 0 channel plan, see rfTuneToChannel()'s comment. */
        bool validRange = (newPage == 0 && newChannel >= 11 && newChannel <= 26)
                        || (newPage == 31 && newChannel <= 128);
        if (!validRange) {
            sendStatus(cmd, STATUS_ERROR);
            break;
        }

        bool wasSniffing = snifferOn;
        bool wasJamming = jammerOn;
        uint8_t prevJamMode = jamMode;
        if (wasSniffing) {
            rfSniffStop();
        }
        if (wasJamming) {
            stopJammer();
        }

        bool ok = rfSwitchBand(newPage == 31 ? BAND_SUBG : BAND_24GHZ);
        channel = newChannel;
        currentPage = newPage;

        if (ok && wasSniffing) {
            snifferOn = rfSniffStart(channel);
            ok = snifferOn;
        } else if (ok && wasJamming) {
            jammerOn = startJammer(prevJamMode, channel);
            ok = jammerOn;
        }
        sendStatus(cmd, ok ? STATUS_OK : STATUS_ERROR);
        break;
    }

    case KB_CMD_SNIFFER_ON:
        if (jammerOn) {
            stopJammer();
        }
        if (!snifferOn) {
            snifferOn = rfSniffStart(channel);
        }
        sendStatus(cmd, snifferOn ? STATUS_OK : STATUS_ERROR);
        break;

    case KB_CMD_SNIFFER_OFF:
        rfSniffStop();
        snifferOn = false;
        sendStatus(cmd, STATUS_OK);
        break;

    case KB_CMD_INJECT: {
        /* payload = [count][delay_ms lo][delay_ms hi][frame...] */
        if (len < 3) {
            sendStatus(cmd, STATUS_ERROR);
            break;
        }
        uint8_t count = payload[0];
        uint16_t delayMs;
        memcpy(&delayMs, &payload[1], 2);
        const uint8_t *frame = &payload[3];
        uint8_t frameLen = (uint8_t)(len - 3);

        if (count == 0 || frameLen == 0 || frameLen > IEEE_MAX_PSDU - 2) {
            sendStatus(cmd, STATUS_ERROR);
            break;
        }

        bool wasSniffing = snifferOn;
        if (wasSniffing) {
            rfSniffStop();
        }
        if (jammerOn) {
            /* Radio can't jam and transmit at once; leave it stopped after
             * injecting rather than silently resuming a disruptive state. */
            stopJammer();
        }

        bool ok = rfTuneToChannel(channel);
        for (uint8_t i = 0; ok && i < count; i++) {
            trace(TR_INJECT_TX_BEGIN, i);
            ok = rfTransmitOnce(frame, frameLen);
            trace(TR_INJECT_TX_POSTED, ok);
            /* TI's own rfPacketTx reference example (prop_rf) calls
             * RF_yield() after every single transmit, before preparing the
             * next one - releasing the RF core's "client active" hold
             * rather than leaving it continuously powered between posts.
             * This firmware never did that anywhere. Empirically, 3+
             * back-to-back sub-1GHz CMD_PROP_TX posts without ever
             * yielding in between reliably froze UART communication
             * (count=2 always worked, count=3 never did); 2.4GHz was
             * unaffected. Matching TI's idiom here specifically to test
             * and (if it holds) fix that. */
            RF_yield(rfHandle);
            trace(TR_INJECT_YIELDED, i);
            if (ok && (uint8_t)(i + 1) < count && delayMs > 0) {
                usleep((unsigned int)delayMs * 1000);
            }
        }

        if (wasSniffing) {
            snifferOn = rfSniffStart(channel);
        }

        sendStatus(cmd, ok ? STATUS_OK : STATUS_ERROR);
        break;
    }

    case KB_CMD_JAMMER_ON: {
        uint8_t mode = (len >= 1) ? payload[0] : JAM_MODE_CONSTANT;
        trace(TR_JAMMER_ON_REQ, mode);
        if (mode != JAM_MODE_CONSTANT && mode != JAM_MODE_REFLEXIVE) {
            sendStatus(cmd, STATUS_ERROR);
            break;
        }
        if (snifferOn) {
            rfSniffStop();
            snifferOn = false;
        }
        if (jammerOn && jamMode != mode) {
            stopJammer();
        }
        if (!jammerOn) {
            jammerOn = startJammer(mode, channel);
        }
        sendStatus(cmd, jammerOn ? STATUS_OK : STATUS_ERROR);
        break;
    }

    case KB_CMD_JAMMER_OFF:
        /* Used to special-case sub-1GHz constant-carrier with an early,
         * best-effort status reply before stopJammer()'s forced reboot -
         * UART was expected dead already at that point. Now that the
         * brown-out root cause is fixed (see stopJammer()'s comment) and
         * stopJammer() no longer reboots, that pre-emptive reply would be a
         * real protocol bug (two status frames for one command, desyncing
         * the host's reply parser) rather than a harmless no-op. Single
         * normal reply, same as every other command. */
        stopJammer();
        sendStatus(cmd, STATUS_OK);
        break;

    case KB_CMD_RESET:
        /* A software-level stop (cancelling known commands via
         * rfSniffStop()/stopJammer()) isn't always enough: after heavy use
         * (many jammer/channel-change cycles) the RF core has been observed
         * to end up in a state where RF commands like SNIFFER_ON start
         * returning STATUS_ERROR immediately even though this UART command
         * loop and PING are fine. An external JTAG-level board reset was
         * found to reliably clear it. RF_close()+RF_open() (the natural
         * in-firmware equivalent) was tried first, but RF_close() pends on
         * the RF command queue internally, and that pend was observed to
         * hang indefinitely on this hardware/SDK combination - the same
         * unreliable-blocking-wait class of issue rfPostAndPoll() (see its
         * comment above) already had to work around for ordinary RF
         * commands, just hit here via a TI driver call this firmware
         * doesn't control the internals of. A genuine full chip reset
         * (SysCtrlSystemReset(), never returns) sidesteps that class of
         * issue entirely by not calling into the RF driver's shutdown path
         * at all, and is the truest available equivalent of the external
         * JTAG reset that was observed to work. This means the UART/USB
         * link itself drops and re-enumerates, same as after a reflash -
         * send the OK reply and give it a moment to actually drain out the
         * wire first so the host sees it before the link goes away. */
        sendStatus(cmd, STATUS_OK);
        usleep(50000);
        SysCtrlSystemReset();
        /* unreachable */
        break;

    case KB_CMD_SET_SELFACK:
        if (len == 1) {
            selfAckEnabled = (payload[0] != 0);
            bool ok = true;
            if (snifferOn) {
                /* Frame filtering/auto-ACK only take effect on a fresh
                 * CMD_IEEE_RX, so restart sniffing to apply the new mode. */
                rfSniffStop();
                snifferOn = rfSniffStart(channel);
                ok = snifferOn;
            }
            sendStatus(cmd, ok ? STATUS_OK : STATUS_ERROR);
        } else {
            sendStatus(cmd, STATUS_ERROR);
        }
        break;

    case KB_CMD_GET_RSSI: {
        /* Deliberately minimal and low-risk: a single direct/immediate
         * TI driver call (RF_getRssi() -> CMD_GET_RSSI), no new RF_postCmd
         * of our own, no state changes. Per TI's driver
         * (ti/drivers/rf/RFCC26X2_multiMode.c's RF_getRssi() and its
         * header doc), this only returns a real reading while some RX
         * operation is actively running on the RF core - the caller is
         * expected to already have SNIFFER_ON active (any band); if not,
         * or if the read genuinely fails, RF_getRssi() returns its own
         * documented sentinel RF_GET_RSSI_ERROR_VAL (-128, a value real
         * readings on this hardware essentially never hit), which is
         * passed straight through rather than guessed at or hidden. */
        int8_t rssi = RF_getRssi(rfHandle);
        uint8_t reply[1] = { (uint8_t)rssi };
        sendReply(cmd, reply, sizeof(reply));
        break;
    }

    default:
        /* Unknown command: no reply, just resync on the next SOF. */
        break;
    }
}

static void *uartCommandThread(void *arg0)
{
    uint8_t payload[256];

    while (1) {
        uint8_t sof;
        if (!readExact(&sof, 1) || sof != KB_SOF) {
            continue;
        }
        trace(TR_UART_READEXACT_OK, 1);

        uint8_t hdr[2];
        if (!readExact(hdr, sizeof(hdr))) {
            continue;
        }
        uint8_t cmd = hdr[0];
        uint8_t len = hdr[1];

        if (len > 0 && !readExact(payload, len)) {
            continue;
        }

        GPIO_toggle(CONFIG_GPIO_GLED);
        handleCommand(cmd, payload, len);
    }
}

void *mainThread(void *arg0)
{
    GPIO_setConfig(CONFIG_GPIO_RLED, GPIO_CFG_OUT_STD | GPIO_CFG_OUT_LOW);
    GPIO_setConfig(CONFIG_GPIO_GLED, GPIO_CFG_OUT_STD | GPIO_CFG_OUT_LOW);

    pthread_mutex_init(&uartTxLock, NULL);
    sem_init(&capSem, 0, 0);

    UART2_Params uartParams;
    UART2_Params_init(&uartParams);
    uartParams.baudRate = 921600;
    uartParams.readMode = UART2_Mode_BLOCKING;
    uartParams.writeMode = UART2_Mode_BLOCKING;
    uart = UART2_open(CONFIG_UART2_KB, &uartParams);

    if (uart == NULL) {
        while (1) {
            GPIO_toggle(CONFIG_GPIO_RLED);
            usleep(100000);
        }
    }

    if (RFQueue_defineQueue(&rxDataQueue, rxDataEntryBuffer,
                             sizeof(rxDataEntryBuffer), NUM_RX_ENTRIES,
                             IEEE_MAX_PSDU + NUM_APPENDED_BYTES) != 0) {
        while (1) {
            GPIO_toggle(CONFIG_GPIO_RLED);
            usleep(250000);
        }
    }

    RF_Params rfParams;
    RF_Params_init(&rfParams);
    rfHandle = RF_open(&rfObject, &RF_prop_ieee154,
                        (RF_RadioSetup *)&RF_cmdRadioSetup_ieee154, &rfParams);
    if (rfHandle == NULL) {
        while (1) {
            GPIO_toggle(CONFIG_GPIO_RLED);
            usleep(500000);
        }
    }

    /* Permanently disallow standby/idle power-down for this firmware's
     * entire lifetime. This board is always USB/debug-probe-tethered for
     * this tool's use case (never battery-powered), so there is no real
     * downside to giving up power savings entirely - in exchange for
     * ruling out an entire class of RF-core reliability bugs found via
     * hardware testing: the sub-1GHz (LO-divider) radio path was observed
     * to freeze solid (RFC power domain reporting OFF mid-transmission,
     * UART dead, no self-recovery even after 40s - but reliably cleared
     * by a bare JTAG debug-probe attach, which is consistent with the
     * chip having dropped into standby while RF activity was still
     * logically active rather than a genuine CPU lockup) during sustained
     * sub-1GHz TX activity (constant-carrier jamming, 3+ back-to-back
     * transmissions). TI's own reference project template
     * (examples/rtos/LP_EM_CC1354P10_1/prop_rf/rfCarrierWave/tirtos7/
     * main_tirtos.c) sets exactly these two constraints - gated behind a
     * different board's config flag (CONFIG_LP_CC2674R10_FPGA) rather than
     * applied generally - confirming this is a real, TI-acknowledged
     * necessity for continuous prop-RF activity on this chip family, not
     * a guess. The default idle policy (PowerCC26XX_standbyPolicy, see
     * ti_drivers_config.c) is what these constraints block. */
    Power_setConstraint(PowerCC26XX_SB_DISALLOW);
    Power_setConstraint(PowerCC26XX_IDLE_PD_DISALLOW);

    pthread_t fwdThread;
    pthread_attr_t attrs;
    struct sched_param priParam;
    pthread_attr_init(&attrs);
    priParam.sched_priority = 1;
    pthread_attr_setdetachstate(&attrs, PTHREAD_CREATE_DETACHED);
    pthread_attr_setschedparam(&attrs, &priParam);
    pthread_attr_setstacksize(&attrs, 1024);
    pthread_create(&fwdThread, &attrs, rfForwardThread, NULL);

    return uartCommandThread(NULL);
}
