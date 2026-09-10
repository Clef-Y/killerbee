'''
Support for the TI CC1352P7 (SimpleLink CC13x2x7) running the KillerBee
firmware in firmware/src/kb-cc1352p7.

Sibling of dev_cc1354p10.py - see that module for the project's primary,
hardware-validated device. This one targets the CC1352P7 LaunchPad's own
ONBOARD XDS110 debug probe instead of a standalone LP-XDS110 probe: the
same USB connection carries both JTAG and the probe's UART backchannel,
enumerating as two /dev/ttyACM* ports (check both - one is debug/CMSIS-DAP,
the other is the actual UART). The LaunchPad has RXD<< and TXD>> jumpers
near the XDS110 that must be CLOSED for the backchannel UART to reach the
target chip at all - they are open by default (see TI's Board.html for this
board, "Jumper Settings").

Wire protocol is identical to the CC1354P10's (921600 8N1):

  Host -> Device:  [0xA5][CMD][LEN][LEN bytes payload]
  Device -> Host:  [0xA5][CMD|0x80][LEN][LEN bytes payload]     (command reply)
                   [0xA5][0x90]    [LEN][LEN bytes payload]     (async RX frame)

  CMD_PING         0x01  -> reply payload = ASCII firmware ID string
  CMD_GET_CHANNEL  0x02  -> reply payload = [channel][page]
  CMD_SET_CHANNEL  0x03  payload=[channel] or [channel][page]  -> reply [status]
                         page 0 = 2.4GHz (channel 11-26, default if page
                         omitted); page 31 = 915MHz US ISM Wi-SUN mode #1b
                         (channel 0-128, 902.2 + 0.2*channel MHz - same
                         channel plan as the CC1354P10, since CMD_FS tunes
                         frequency independent of modulation/PHY preset;
                         NOTE this chip's sub-1GHz PHY is FSK-based
                         Wi-SUN, not the CC1354P10's O-QPSK - see
                         firmware/src/kb-cc1352p7/README.md).
  CMD_SNIFFER_ON   0x04  -> reply [status]
  CMD_SNIFFER_OFF  0x05  -> reply [status]
  CMD_INJECT       0x06  payload=[count][delay_ms lo][delay_ms hi][frame...]
  CMD_JAMMER_ON    0x07  payload=[mode] (0=constant carrier, 1=reflexive)
  CMD_JAMMER_OFF   0x08
  CMD_SET_SELFACK  0x09  payload=[enable]
  CMD_RESET        0x0A
  CMD_GET_RSSI     0x0B  -> reply payload = [rssi int8]
                         Ambient channel energy via TI's RF_getRssi(), a
                         single direct/immediate RF-core call. REQUIRES an
                         active RX operation (i.e. SNIFFER_ON already
                         sent) or it returns TI's own documented error
                         sentinel, RF_GET_RSSI_ERROR_VAL = -128.

  Async packet (CMD 0x90) payload:
      [rssi int8][crc_ok uint8][timestamp uint32 LE][framelen uint8][frame...]

  status byte: 0 = OK, 1 = ERROR

See firmware/src/kb-cc1352p7/README.md for the full protocol writeup and
the RF-core command semantics behind each operation.
'''

from typing import Optional, Dict, Union, Any, List

import struct
import time
from datetime import datetime

import serial  # type: ignore

from .kbutils import KBCapabilities, makeFCS

KB_SOF: int = 0xA5

CMD_PING: int = 0x01
CMD_GET_CHANNEL: int = 0x02
CMD_SET_CHANNEL: int = 0x03
CMD_SNIFFER_ON: int = 0x04
CMD_SNIFFER_OFF: int = 0x05
CMD_INJECT: int = 0x06
CMD_JAMMER_ON: int = 0x07
CMD_JAMMER_OFF: int = 0x08
CMD_SET_SELFACK: int = 0x09
CMD_RESET: int = 0x0A
CMD_GET_RSSI: int = 0x0B

RF_GET_RSSI_ERROR_VAL: int = -128

CMD_REPLY_BIT: int = 0x80
CMD_ASYNC_PACKET: int = 0x90

JAM_MODE_CONSTANT: int = 0x00
JAM_MODE_REFLEXIVE: int = 0x01

STATUS_OK: int = 0x00

FW_ID_PREFIX: bytes = b"KB-CC1352P7"


class CC1352P7:
    def __init__(self, dev: str) -> None:
        '''
        Instantiates the KillerBee class for the TI CC1352P7 KillerBee
        firmware, reached over the LaunchPad's onboard XDS110 backchannel
        serial port.
        @param dev: /dev/ttyACM* (or COM*) serial device path
        '''
        self._channel: Optional[int] = 11
        self._page: int = 0
        self.dev: str = dev
        self.handle: Optional[serial.Serial] = None
        self.__stream_open: bool = False
        self.__pending_pkts: List[bytes] = []

        self.handle = serial.Serial(port=self.dev, baudrate=921600,
                                     timeout=0.5, bytesize=8, parity='N',
                                     stopbits=1, xonxoff=0)
        # Let the target's USB CDC-ACM enumeration and firmware settle.
        time.sleep(0.2)
        self.handle.reset_input_buffer()

        self.capabilities: KBCapabilities = KBCapabilities()
        self.__set_capabilities()

    def __set_capabilities(self) -> None:
        self.capabilities.setcapab(KBCapabilities.FREQ_2400, True)
        self.capabilities.setcapab(KBCapabilities.FREQ_900, False)
        self.capabilities.setcapab(KBCapabilities.FREQ_863, False)
        self.capabilities.setcapab(KBCapabilities.FREQ_868, False)
        self.capabilities.setcapab(KBCapabilities.FREQ_870, False)
        self.capabilities.setcapab(KBCapabilities.FREQ_915, True)

        self.capabilities.setcapab(KBCapabilities.SNIFF, True)
        self.capabilities.setcapab(KBCapabilities.SETCHAN, True)
        self.capabilities.setcapab(KBCapabilities.INJECT, True)
        self.capabilities.setcapab(KBCapabilities.SELFACK, True)
        self.capabilities.setcapab(KBCapabilities.PHYJAM, True)
        self.capabilities.setcapab(KBCapabilities.PHYJAM_REFLEX, True)
        # The native IEEE 802.15.4 RX/TX radio commands use a fixed,
        # standard-compliant O-QPSK preamble/SFD - there is no register to
        # reprogram the PHY sync word the way CC2420-style radios expose one.
        self.capabilities.setcapab(KBCapabilities.SET_SYNC, False)
        self.capabilities.setcapab(KBCapabilities.BOOT, False)

    # ==================== Wire protocol helpers ====================

    def __send(self, cmd: int, payload: bytes = b"") -> None:
        if self.handle is None:
            raise Exception("Handle does not exist")
        frame = bytes([KB_SOF, cmd, len(payload)]) + payload
        self.handle.write(frame)

    def __read_exact(self, n: int, timeout: Optional[float] = None) -> Optional[bytes]:
        # Snapshot self.handle once: close() (e.g. from a SIGINT handler
        # while this method is blocked in a read - see tools/zbdump) can set
        # self.handle to None between the initial check and the finally
        # block below if we keep re-reading the attribute.
        handle = self.handle
        if handle is None:
            raise Exception("Handle does not exist")
        if timeout is not None:
            old = handle.timeout
            handle.timeout = timeout
        try:
            data = handle.read(n)
        finally:
            if timeout is not None:
                handle.timeout = old
        if len(data) != n:
            return None
        return data

    def __read_frame(self, timeout: Optional[float] = None) -> Optional[Any]:
        '''
        Reads one [SOF][CMD][LEN][payload] frame from the device, skipping
        any bytes before a valid SOF. Returns (cmd, payload) or None on
        timeout/desync.
        '''
        sof = self.__read_exact(1, timeout)
        if sof is None or sof[0] != KB_SOF:
            return None
        hdr = self.__read_exact(2, timeout)
        if hdr is None:
            return None
        cmd, length = hdr[0], hdr[1]
        payload = b""
        if length > 0:
            payload = self.__read_exact(length, timeout)
            if payload is None:
                return None
        return (cmd, payload)

    def __command(self, cmd: int, payload: bytes = b"", timeout: float = 1.0) -> Any:
        '''
        Sends a command and waits for its reply, transparently forwarding
        any async RX packets seen in the meantime into self.__pending_pkts.
        '''
        self.__send(cmd, payload)
        deadline = time.time() + timeout
        while time.time() < deadline:
            frame = self.__read_frame(timeout=0.2)
            if frame is None:
                continue
            rcmd, rpayload = frame
            if rcmd == CMD_ASYNC_PACKET:
                self.__pending_pkts.append(rpayload)
                continue
            if rcmd == (cmd | CMD_REPLY_BIT):
                return rpayload
        raise Exception("Timed out waiting for reply to command 0x%02x" % cmd)

    # ==================== KillerBee driver interface ====================

    def close(self) -> None:
        if self.__stream_open:
            self.sniffer_off()
        if self.handle is not None:
            self.handle.close()
        self.handle = None

    def check_capability(self, capab: int) -> bool:
        return self.capabilities.check(capab)

    def get_capabilities(self) -> Dict[int, bool]:
        return self.capabilities.getlist()

    def get_dev_info(self) -> List[Any]:
        try:
            fwid = self.__command(CMD_PING, timeout=1.0)
            fwid_str = fwid.decode('ascii', errors='replace')
        except Exception:
            fwid_str = "CC1352P7 (no response)"
        return [self.dev, fwid_str, ""]

    def sniffer_on(self, channel: Optional[int] = None, page: int = 0) -> None:
        self.capabilities.require(KBCapabilities.SNIFF)
        if channel is not None or page:
            self.set_channel(channel, page)
        status = self.__command(CMD_SNIFFER_ON)
        if status[0] != STATUS_OK:
            raise Exception("Device rejected sniffer_on")
        self.__stream_open = True

    def sniffer_off(self) -> None:
        if self.handle is None:
            self.__stream_open = False
            return
        try:
            self.__command(CMD_SNIFFER_OFF)
        finally:
            self.__stream_open = False

    def set_channel(self, channel: int, page: int = 0) -> None:
        self.capabilities.require(KBCapabilities.SETCHAN)
        if page == 0:
            if channel < 11 or channel > 26:
                raise Exception('Invalid channel')
        elif page == 31:
            self.capabilities.require(KBCapabilities.FREQ_915)
            if channel < 0 or channel > 128:
                raise Exception('Invalid channel (must be 0-128 for the 915 MHz US ISM '
                                 'band - 902.2 + 0.2*channel MHz, same channel plan as '
                                 'the CC1354P10; this chip uses a Wi-SUN FSK PHY instead '
                                 'of O-QPSK for RX/TX, see firmware/src/kb-cc1352p7/'
                                 'README.md)')
        else:
            raise Exception('Unsupported page %d - only 0 (2.4 GHz) and 31 (915 MHz) exist on this device' % page)
        status = self.__command(CMD_SET_CHANNEL, bytes([channel, page]))
        if status[0] != STATUS_OK:
            raise Exception("Device rejected channel %d (page %d)" % (channel, page))
        self._channel = channel
        self._page = page

    def inject(self, packet: bytes, channel: Optional[int] = None, count: int = 1,
               delay: int = 0, page: int = 0) -> None:
        self.capabilities.require(KBCapabilities.INJECT)

        if len(packet) < 1:
            raise Exception('Empty packet')
        if len(packet) > 125:  # 127 - 2 to accommodate FCS the hardware appends
            raise Exception('Packet too long')

        if channel is not None or page:
            self.set_channel(channel, page)

        delay_ms = int(delay * 1000)
        if delay_ms > 0xFFFF:
            delay_ms = 0xFFFF

        payload = bytes([count & 0xFF]) + struct.pack('<H', delay_ms) + bytes(packet)
        status = self.__command(CMD_INJECT, payload, timeout=2.0 + count * (delay_ms / 1000.0))
        if status[0] != STATUS_OK:
            raise Exception("Device rejected inject()")

    def pnext(self, timeout: int = 100) -> Optional[Dict[Union[int, str], Any]]:
        self.capabilities.require(KBCapabilities.SNIFF)

        if not self.__stream_open:
            self.sniffer_on()

        if self.__pending_pkts:
            payload = self.__pending_pkts.pop(0)
        else:
            # timeout is documented in usec elsewhere in KillerBee, but by
            # convention (see dev_sl_beehive/dev_sl_nodetest) is treated here
            # as a plain serial read timeout in seconds.
            frame = self.__read_frame(timeout=timeout if timeout else 0.1)
            if frame is None:
                return None
            rcmd, payload = frame
            if rcmd != CMD_ASYNC_PACKET:
                return None

        if len(payload) < 7:
            return None

        rssi = struct.unpack('b', payload[0:1])[0]
        crc_ok = bool(payload[1])
        framelen = payload[6]
        frame_bytes = payload[7:7 + framelen]

        if len(frame_bytes) != framelen:
            return None

        result: Dict[Union[int, str], Any] = {
            0: frame_bytes,
            1: crc_ok,
            2: rssi,
            'bytes': frame_bytes,
            'validcrc': crc_ok,
            'rssi': rssi,
            'location': None,
            'datetime': datetime.utcnow(),
        }
        result['dbm'] = rssi
        return result

    def ping(self, da: Any, panid: Any, sa: Any, channel: Optional[int] = None,
             page: int = 0) -> None:
        raise Exception('Not yet implemented')

    def jammer_on(self, channel: Optional[int] = None, page: int = 0,
                  method: Optional[str] = None) -> None:
        '''
        @param method: None/"constant" for a continuous PHY-level carrier
            (KBCapabilities.PHYJAM), or "reflexive" for the best-effort
            listen-then-burst reflexive jammer (KBCapabilities.PHYJAM_REFLEX).
            See firmware/src/kb-cc1352p7/README.md for the reflexive mode's
            latency caveats - it is a software-loop reflex, not an
            RF-core-instant one.
        '''
        mode = JAM_MODE_REFLEXIVE if method == "reflexive" else JAM_MODE_CONSTANT
        self.capabilities.require(KBCapabilities.PHYJAM_REFLEX if mode
                                   else KBCapabilities.PHYJAM)

        if channel is not None or page:
            self.set_channel(channel, page)

        status = self.__command(CMD_JAMMER_ON, bytes([mode]))
        if status[0] != STATUS_OK:
            raise Exception("Device rejected jammer_on()")

    def jammer_off(self) -> None:
        self.__command(CMD_JAMMER_OFF)

    def set_sync(self, sync: int = 0xA70F) -> Any:
        self.capabilities.require(KBCapabilities.SET_SYNC)
        raise Exception('Not supported: the CC1352P7 native IEEE 802.15.4 '
                         'RX/TX commands use a fixed standard PHY sync word')

    # ==================== Extra, non-framework-standard controls ====================
    # KillerBee's KBCapabilities.SELFACK has no generic setter in the
    # KillerBee/dev_* interface anywhere in this codebase (dev_template.py
    # simply declares the capability False and stops there). This device
    # genuinely supports it, so it's exposed as an extra method reachable
    # via kb.driver.set_selfack(...).

    def set_selfack(self, enable: bool) -> None:
        '''
        Enables/disables the radio's hardware auto-ACK.
        NOTE: enabling self-ACK also enables IEEE 802.15.4 address-based
        frame filtering on the firmware side (the radio needs to know a
        frame is "for us" before it can ACK it), which sacrifices full
        promiscuous capture while it is on.
        '''
        self.capabilities.require(KBCapabilities.SELFACK)
        status = self.__command(CMD_SET_SELFACK, bytes([1 if enable else 0]))
        if status[0] != STATUS_OK:
            raise Exception("Device rejected set_selfack()")

    def reset(self) -> None:
        '''Resets the firmware's RF/channel/self-ack state to defaults.'''
        self.__command(CMD_RESET)

    def get_rssi(self) -> Optional[int]:
        '''
        Samples ambient RF energy on the current channel via TI's
        RF_getRssi(), independent of packet capture - i.e. this reports a
        real reading even on a channel with zero traffic.

        REQUIRES sniffer_on() to already be active (an RX operation must be
        running on the RF core for the radio to have a live RSSI value).
        Returns None if no RX operation is running, mirroring TI's own
        RF_GET_RSSI_ERROR_VAL sentinel rather than returning the raw,
        misleading -128.
        '''
        payload = self.__command(CMD_GET_RSSI)
        if len(payload) < 1:
            raise Exception("Malformed GET_RSSI reply")
        rssi = struct.unpack('b', payload[0:1])[0]
        if rssi == RF_GET_RSSI_ERROR_VAL:
            return None
        return rssi
