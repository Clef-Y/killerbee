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
 *   rejection, JAMMER constant and reflexive start/stop). SET_SELFACK not
 *   yet re-validated against real hardware.
 *
 * All RF commands are posted with RF_postCmd() and waited on by polling
 * the command's own .status field (rfPostAndPoll()) rather than a blocking
 * RF_pendCmd(..., RF_EventLastCmdDone): on this hardware/setup that
 * blocking wait was observed to hang indefinitely even after the command
 * had already completed successfully, wedging the whole UART command loop.
 */

#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <unistd.h>
#include <pthread.h>
#include <semaphore.h>

#include <ti/drivers/UART2.h>
#include <ti/drivers/GPIO.h>
#include <ti/drivers/rf/RF.h>

#include <ti/devices/DeviceFamily.h>
#include DeviceFamily_constructPath(driverlib/rf_mailbox.h)
#include DeviceFamily_constructPath(driverlib/rf_common_cmd.h)
#include DeviceFamily_constructPath(driverlib/rf_ieee_cmd.h)
#include DeviceFamily_constructPath(driverlib/rf_ieee_mailbox.h)

#include "ti_drivers_config.h"
#include "ti_radio_config.h"
#include "RFQueue.h"

/* ti_radio_config.h exposes the *_ieee154 suffixed globals; alias here so
 * the rest of this file can use the command names from the README's
 * protocol table. */
#define RF_cmdIeeeRx RF_cmdIeeeRx_ieee154
#define RF_cmdIeeeTx RF_cmdIeeeTx_ieee154
#define RF_cmdFs     RF_cmdFs_ieee154

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
            uint8_t corrCrc = p[frameLen + 1];
            slot->crcOk = (corrCrc & 0x80) == 0; /* bit7 = bCrcErr */
            memcpy(&slot->timestamp, &p[frameLen + 2], 4);
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

static bool rfSniffStart(uint8_t ch)
{
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

/* Neither CMD_IEEE_TX nor CMD_TX_TEST carries its own channel field - both
 * transmit on whatever frequency the synth is currently tuned to, so an
 * explicit CMD_FS is required before either, regardless of prior state. */
static bool rfTuneToChannel(uint8_t ch)
{
    RF_cmdFs.frequency = (uint16_t)(2405 + 5 * (ch - 11));
    RF_cmdFs.fractFreq = 0;

    return rfPostAndPoll((RF_Op *)&RF_cmdFs, DONE_OK);
}

static bool rfTransmitOnce(const uint8_t *frame, uint8_t len)
{
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

/* ==================== Command dispatch ==================== */

static void handleCommand(uint8_t cmd, const uint8_t *payload, uint8_t len)
{
    switch (cmd) {
    case KB_CMD_PING:
        sendReply(cmd, (const uint8_t *)FW_ID, (uint8_t)(sizeof(FW_ID) - 1));
        break;

    case KB_CMD_GET_CHANNEL:
        sendReply(cmd, &channel, 1);
        break;

    case KB_CMD_SET_CHANNEL:
        if (len == 1 && payload[0] >= 11 && payload[0] <= 26) {
            channel = payload[0];
            bool ok = true;
            if (snifferOn) {
                rfSniffStop();
                snifferOn = rfSniffStart(channel);
                ok = snifferOn;
            } else if (jammerOn && jamMode == JAM_MODE_CONSTANT) {
                rfJamStop();
                jammerOn = rfJamStart(channel);
                ok = jammerOn;
            }
            /* Reflexive jam reads `channel` fresh every loop iteration, so
             * no explicit restart is needed for that mode. */
            sendStatus(cmd, ok ? STATUS_OK : STATUS_ERROR);
        } else {
            sendStatus(cmd, STATUS_ERROR);
        }
        break;

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
        rfSniffStop();
        stopJammer();
        snifferOn = false;
        selfAckEnabled = false;
        channel = 11;
        sendStatus(cmd, STATUS_OK);
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
