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
#include <stdbool.h>
#include <string.h>
#include <unistd.h>
#include <pthread.h>
#include <semaphore.h>
#include <time.h>

#include <ti/drivers/UART2.h>
#include <ti/drivers/GPIO.h>
#include <ti/drivers/rf/RF.h>

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

/* Forward declaration - rfSniffStop() needs the bounded RF_cancelCmd()
 * wrapper defined down in the jammer section (shared with rfJamStop()). */
static void rfCancelCmdBounded(RF_CmdHandle h);

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

static bool rfSniffStart(uint8_t ch)
{
    if (currentBand == BAND_SUBG) {
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
    if (sniffCmdHandle != RF_ALLOC_ERROR) {
        RF_CmdHandle h = sniffCmdHandle;
        sniffCmdHandle = RF_ALLOC_ERROR;
        rfCancelCmdBounded(h);
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
 * Channel-to-frequency mapping for BAND_SUBG follows the classic IEEE
 * 802.15.4-2006 915 MHz US ISM band plan (channels 1-10, 906 + 2*(ch-1)
 * MHz - 906..924 MHz) rather than KillerBee's own kbutils.py page-31
 * frequency() helper, which uses a denser/different spacing more suited
 * to a generic SUN-PHY channel plan - channels 1-10 specifically was the
 * explicit ask this was built for, and matches the real, well-known
 * standard channel numbering. */
static bool rfTuneToChannel(uint8_t ch)
{
    if (currentBand == BAND_SUBG) {
        RF_cmdFsSubg.frequency = (uint16_t)(906 + 2 * (ch - 1));
        RF_cmdFsSubg.fractFreq = 0;
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

static rfc_CMD_TX_TEST_t rfCmdTxTest;
static RF_CmdHandle jamCmdHandle = RF_ALLOC_ERROR;

static bool rfJamStart(uint8_t ch)
{
    if (!rfTuneToChannel(ch)) {
        return false;
    }

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
    return jamCmdHandle != RF_ALLOC_ERROR;
}

/* RF_cancelCmd() is documented as a synchronous call that waits for the RF
 * core to actually abort the command. Empirically (full 2.4GHz + sub-1GHz
 * regression test, see README) it can hang indefinitely just like
 * RF_pendCmd()/RF_close() were already found to under this hardware's
 * "stuck RF core" failure mode - reproduced specifically via the sub-1GHz
 * CMD_TX_TEST constant jammer's stop path. Bound the wait in a helper
 * thread and, if RF_cancelCmd doesn't return promptly, fail safe exactly
 * like KB_CMD_RESET does: a full SysCtrlSystemReset() reboot rather than
 * hanging forever. The watchdog thread is simply abandoned in the timeout
 * case (moot - the whole chip reboots). */
static sem_t jamCancelDoneSem;

static void *jamCancelThread(void *arg)
{
    RF_CmdHandle h = (RF_CmdHandle)(intptr_t)arg;
    RF_cancelCmd(rfHandle, h, 0);
    sem_post(&jamCancelDoneSem);
    return NULL;
}

static void rfCancelCmdBounded(RF_CmdHandle h)
{
    sem_init(&jamCancelDoneSem, 0, 0);

    pthread_t t;
    pthread_attr_t attrs;
    pthread_attr_init(&attrs);
    pthread_attr_setdetachstate(&attrs, PTHREAD_CREATE_DETACHED);
    pthread_attr_setstacksize(&attrs, 768);
    if (pthread_create(&t, &attrs, jamCancelThread, (void *)(intptr_t)h) != 0) {
        SysCtrlSystemReset();  /* never returns */
    }

    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    ts.tv_sec += 1;  /* generous margin for an abrupt (mode=0) abort */
    if (sem_timedwait(&jamCancelDoneSem, &ts) != 0) {
        SysCtrlSystemReset();  /* never returns */
    }
}

static void rfJamStop(void)
{
    if (jamCmdHandle != RF_ALLOC_ERROR) {
        RF_CmdHandle h = jamCmdHandle;
        jamCmdHandle = RF_ALLOC_ERROR;
        rfCancelCmdBounded(h);
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
    while (reflexRunning) {
        /* Arm a short promiscuous listen window on the current channel. */
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

        uint8_t capHeadBefore = capHead;

        RF_CmdHandle h = RF_postCmd(rfHandle, (RF_Op *)&RF_cmdIeeeRx,
                                     RF_PriorityNormal, rxCallback,
                                     RF_EventRxEntryDone);
        if (h == RF_ALLOC_ERROR) {
            break;
        }

        /* Poll for the listen window to end rather than a blocking
         * RF_pendCmd() - see rfPostAndPoll()'s comment for why. The window
         * is ~1ms (REFLEX_LISTEN_TICKS), so give it a generous margin. */
        for (uint32_t waitedUs = 0; waitedUs < 5000; waitedUs += 200) {
            if (RF_cmdIeeeRx.status >= 0x0400) {
                break;
            }
            usleep(200);
        }
        if (RF_cmdIeeeRx.status < 0x0400) {
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

/* Stops whichever jam mode is currently active. */
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
        /* reflexJamThread() posts CMD_IEEE_RX unconditionally - it isn't
         * band-aware like rfSniffStart()/rfTuneToChannel()/rfTransmitOnce()
         * are, so it would post a mismatched command if the RF core is
         * actually set up for the sub-1GHz PHY. Not yet implemented for
         * BAND_SUBG - constant-carrier jamming (CMD_TX_TEST, PHY-agnostic)
         * still works there via the rfJamStart() path below. */
        if (currentBand == BAND_SUBG) {
            return false;
        }
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
    return true;
}

/* ==================== Command dispatch ==================== */

static void handleCommand(uint8_t cmd, const uint8_t *payload, uint8_t len)
{
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
        bool validRange = (newPage == 0 && newChannel >= 11 && newChannel <= 26)
                        || (newPage == 31 && newChannel >= 1 && newChannel <= 10);
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
            ok = rfTransmitOnce(frame, frameLen);
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
