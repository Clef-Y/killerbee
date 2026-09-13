'''
Support for the TI CC1354P10 (SimpleLink CC13x4) running the KillerBee
firmware in firmware/src/kb-cc1354p10.

The CC1354P10 has no on-board debug/USB probe of its own - the standard
hardware setup is a CC1354P10 target board connected via its 20-pin debug
connector directly to a standalone LP-XDS110 debug probe (no LaunchPad, no
separate USB-serial adapter). That connector carries both JTAG and the
probe's UART backchannel; this driver talks over the same serial port the
XDS110 exposes as that backchannel (e.g. /dev/ttyACM0 - check both
/dev/ttyACM* ports it enumerates, since one is the debug/CMSIS-DAP
interface and the other is the actual UART).

Wire protocol (921600 8N1):

  Host -> Device:  [0xA5][CMD][LEN][LEN bytes payload]
  Device -> Host:  [0xA5][CMD|0x80][LEN][LEN bytes payload]     (command reply)
                   [0xA5][0x90]    [LEN][LEN bytes payload]     (async RX frame)

  CMD_PING         0x01  -> reply payload = ASCII firmware ID string
  CMD_GET_CHANNEL  0x02  -> reply payload = [channel][page]
  CMD_SET_CHANNEL  0x03  payload=[channel] or [channel][page]  -> reply [status]
                         page 0 = 2.4GHz (channel 11-26, default if page
                         omitted); page 31 = 915MHz US ISM SUN O-QPSK
                         Rate Mode 0 (channel 0-128, 902.2 + 0.2*channel
                         MHz - the real standard channel plan, verified
                         against TI's own ti154stack); page 28 = 863-876MHz
                         EU/UK SUN O-QPSK Rate Mode 0 (channel 0-65, 863.0 +
                         0.2*channel MHz - same 0.2MHz channel spacing as
                         page 31, for finer scan resolution; a project-local
                         channel plan otherwise, see main.c's "stage
                         5"/"Page 28 support" comments for why - switches
                         the RF core's radio setup at runtime, see main.c's
                         "stage 4" comment.
  CMD_SNIFFER_ON   0x04  -> reply [status]
  CMD_SNIFFER_OFF  0x05  -> reply [status]
  CMD_INJECT       0x06  payload=[count][delay_ms lo][delay_ms hi][frame...]
  CMD_JAMMER_ON    0x07  payload=[mode] (0=constant carrier, 1=reflexive)
  CMD_JAMMER_OFF   0x08
  CMD_SET_SELFACK  0x09  payload=[enable]
  CMD_RESET        0x0A
  CMD_GET_RSSI     0x0B  -> reply payload = [rssi int8]
                         Ambient channel energy via TI's RF_getRssi(), a
                         single direct/immediate RF-core call - no new
                         RF_postCmd, no state change. REQUIRES an active
                         RX operation (i.e. SNIFFER_ON already sent) or it
                         returns TI's own documented error sentinel,
                         RF_GET_RSSI_ERROR_VAL = -128 (see RFCC26X2.h).
  CMD_JAM_HOP_ON   0x0C  payload=[dwell_ms lo][dwell_ms hi][rangeCount]
                         [page0][chStart0][chEnd0]...
                         [page(n-1)][chStart(n-1)][chEnd(n-1)]
                         -> reply [status]. Sub-1GHz only - each range
                         must be (31, 0<=start<=end<=128) or
                         (28, 0<=start<=end<=65). Starts a constant-carrier
                         jam that hops across the given *cross-page*
                         channel *range* list entirely on-chip - no host
                         round-trip per hop, unlike driving the same
                         rotation via repeated CMD_SET_CHANNEL calls. Each
                         entry is an inclusive [page][chStart][chEnd]
                         range, not a bare channel: page 31/28 use
                         different frequency formulas at the same raw
                         channel number (so a bare channel span would be
                         ambiguous), and ranges (not one entry per
                         channel) are what let a large contiguous request
                         (e.g. 120 channels) fit this command's payload at
                         all - see jam_hop_on()'s _collapse_to_ranges().
                         Stopped with the existing CMD_JAMMER_OFF. See
                         main.c's "stage 6" comment.

  Async packet (CMD 0x90) payload:
      [rssi int8][crc_ok uint8][timestamp uint32 LE][framelen uint8][frame...]

  status byte: 0 = OK, 1 = ERROR

See firmware/src/kb-cc1354p10/README.md for the full protocol writeup and
the RF-core command semantics behind each operation.
'''

from typing import Optional, Dict, Union, Any, List, Tuple

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
CMD_JAM_HOP_ON: int = 0x0C

RF_GET_RSSI_ERROR_VAL: int = -128

CMD_REPLY_BIT: int = 0x80
CMD_ASYNC_PACKET: int = 0x90

JAM_MODE_CONSTANT: int = 0x00
JAM_MODE_REFLEXIVE: int = 0x01
MAX_HOP_RANGES: int = 84  # matches firmware's MAX_HOP_RANGES - the actual wire-protocol
                          # ceiling (255-byte payload LEN, 3-byte header + 3 bytes/range)

STATUS_OK: int = 0x00

FW_ID_PREFIX: bytes = b"KB-CC1354P10"


class CC1354P10:
    def __init__(self, dev: str) -> None:
        '''
        Instantiates the KillerBee class for the TI CC1354P10 KillerBee
        firmware, reached over the XDS110 backchannel serial port.
        @param dev: /dev/ttyACM* (or COM*) serial device path
        '''
        self._channel: Optional[int] = 11
        self._page: int = 0
        self.dev: str = dev
        self.handle: Optional[serial.Serial] = None
        self.__stream_open: bool = False
        self.__pending_pkts: List[bytes] = []

        # Fixed for the handle's whole lifetime - see __read_exact() for why
        # this must never be reassigned after construction.
        self.handle = serial.Serial(port=self.dev, baudrate=921600,
                                     timeout=0.1, bytesize=8, parity='N',
                                     stopbits=1, xonxoff=0)
        # Let the target's USB CDC-ACM enumeration and firmware settle.
        time.sleep(0.2)
        self.handle.reset_input_buffer()
        # reset_input_buffer() alone isn't enough: macOS's CDC-ACM driver can
        # still be delivering a previous session's leftover response bytes
        # (already in flight over USB when the flush ran) if this port was
        # recently opened/closed elsewhere (e.g. zbid's own probing). Drain
        # until genuinely empty so the first real command's reply can't get
        # misframed by stale bytes ahead of it.
        while self.handle.read(64):
            pass

        self.capabilities: KBCapabilities = KBCapabilities()
        self.__set_capabilities()

    def __set_capabilities(self) -> None:
        self.capabilities.setcapab(KBCapabilities.FREQ_2400, True)
        self.capabilities.setcapab(KBCapabilities.FREQ_900, False)
        self.capabilities.setcapab(KBCapabilities.FREQ_868, False)
        self.capabilities.setcapab(KBCapabilities.FREQ_870, False)
        # 915 MHz US ISM, channels 0-128 (902.2 + 0.2*channel MHz - the
        # real SUN O-QPSK Rate Mode 0 channel plan, verified against TI's
        # own ti154stack), via a second on-chip radio setup the firmware
        # switches to at runtime - see firmware/src/kb-cc1354p10/main.c's
        # "stage 4" header comment.
        self.capabilities.setcapab(KBCapabilities.FREQ_915, True)
        # 863-876 MHz EU/UK, channels 0-65 (863.0 + 0.2*channel MHz, the
        # same 0.2MHz spacing page 31 uses at 915MHz - a project-local
        # channel plan, NOT the same as KBCapabilities'
        # generic page-28 frequency() formula, which was written for older
        # Silabs hardware's narrower 863-868 MHz sub-band; kb.frequency()
        # will report the wrong frequency for this device on page 28, same
        # as the already-documented page 31 divergence) - CC1354P10 only,
        # see firmware/src/kb-cc1354p10/main.c's "stage 5"/README.md's
        # "Page 28 support" section.
        self.capabilities.setcapab(KBCapabilities.FREQ_863, True)
        # Also advertise the wider range explicitly: KBCapabilities.
        # is_valid_channel()'s page-28 case can't just raise its shared
        # upper bound to 65 for every FREQ_863 device (see
        # FREQ_863_WIDE's docstring in kbutils.py - some page-28 hardware
        # packs the channel into a 5-bit field and would silently corrupt
        # above channel 31), so this device needs its own, distinct flag
        # to unlock KillerBee.set_channel()'s full 0-65 range at the
        # generic capabilities-check layer (dev_cc1354p10.set_channel()'s
        # own range check was already correct - this fixes the *caller's*
        # pre-check in killerbee/__init__.py, which runs first and would
        # otherwise reject channels 27-65 before this driver ever sees them).
        self.capabilities.setcapab(KBCapabilities.FREQ_863_WIDE, True)

        self.capabilities.setcapab(KBCapabilities.SNIFF, True)
        self.capabilities.setcapab(KBCapabilities.SETCHAN, True)
        self.capabilities.setcapab(KBCapabilities.INJECT, True)
        self.capabilities.setcapab(KBCapabilities.SELFACK, True)
        self.capabilities.setcapab(KBCapabilities.PHYJAM, True)
        self.capabilities.setcapab(KBCapabilities.PHYJAM_REFLEX, True)
        # On-chip channel-hop jamming (CMD_JAM_HOP_ON) - sub-1GHz only on
        # this firmware (pages 31/28; ported from the CC1352P7 firmware's
        # 2.4GHz-only version) - see firmware/src/kb-cc1354p10/main.c's
        # "stage 6" comment. jam_hop_on() enforces the sub-1GHz-only
        # restriction; this flag just advertises on-chip hop support in
        # general, same as how PHYJAM's specifics are validated per-device.
        self.capabilities.setcapab(KBCapabilities.PHYJAM_HOP, True)
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
        # Deliberately never touch handle.timeout here: reassigning a
        # CDC-ACM serial.Serial's timeout attribute after construction was
        # confirmed (by direct testing on macOS) to silently drop bytes
        # already in flight, making reads time out even though the device
        # had already replied within milliseconds. Poll instead, using the
        # handle's fixed construction-time timeout for each underlying
        # read() and tracking the caller's requested deadline ourselves.
        deadline = time.time() + timeout if timeout is not None else None
        data = b""
        while len(data) < n:
            data += handle.read(n - len(data))
            if len(data) == n:
                break
            if deadline is not None and time.time() >= deadline:
                break
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
            fwid_str = "CC1354P10 (no response)"
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
                                 'band - the real SUN O-QPSK Rate Mode 0 channel plan, '
                                 '902.2 + 0.2*channel MHz, verified against TI\'s own '
                                 'ti154stack; see firmware/src/kb-cc1354p10/README.md)')
        elif page == 28:
            self.capabilities.require(KBCapabilities.FREQ_863)
            if channel < 0 or channel > 65:
                raise Exception('Invalid channel (must be 0-65 for the 863-876 MHz EU/UK '
                                 'band - a project-local channel plan specific to this '
                                 'firmware, 863.0 + 0.2*channel MHz, NOT the same as '
                                 'KBCapabilities\' generic page-28 frequency() formula; '
                                 'see firmware/src/kb-cc1354p10/README.md\'s "Page 28 '
                                 'support" section)')
        else:
            raise Exception('Unsupported page %d - only 0 (2.4 GHz), 28 (863-876 MHz EU/UK) '
                             'and 31 (915 MHz US ISM) exist on this device' % page)
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
            See firmware/src/kb-cc1354p10/README.md for the reflexive mode's
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

    @staticmethod
    def _collapse_to_ranges(hops: List[Tuple[int, int]]) -> List[Tuple[int, int, int]]:
        '''
        Collapses a flat (page, channel) hop list into (page, chStart,
        chEnd) inclusive ranges wherever consecutive entries share the
        same page and increment by exactly 1. A scattered, non-contiguous
        entry just becomes its own length-1 range (chStart == chEnd) -
        this never loses hop order or any entry, it only shrinks the wire
        payload. Exists because the firmware's wire payload is capped at
        255 bytes total (a single-byte LEN field) - an explicit
        [page][channel] pair per hop caps out around ~126 hops, but two
        real, useful requests (e.g. page 31 channels 9-128 + page 28
        channels 9-65 = 177 channels) are common and are each a single
        contiguous run, so encoding them as ranges (3 bytes each,
        regardless of span) sidesteps the ceiling entirely.
        '''
        if not hops:
            return []
        ranges: List[Tuple[int, int, int]] = []
        curPage, start = hops[0]
        end = start
        for page, ch in hops[1:]:
            if page == curPage and ch == end + 1:
                end = ch
            else:
                ranges.append((curPage, start, end))
                curPage, start, end = page, ch, ch
        ranges.append((curPage, start, end))
        return ranges

    def jam_hop_on(self, hops: List[Tuple[int, int]], dwell_ms: int) -> None:
        '''
        Starts a constant-carrier jam that hops across the given
        *cross-page* sub-1GHz channel list entirely on-chip - no host
        round-trip per hop, unlike driving the same rotation via repeated
        set_channel() calls (what tools/subg_jam.py's --page-channels does).
        See firmware/src/kb-cc1354p10/README.md's "On-chip channel-hop
        jamming" section for why this exists and how it compares. Stop
        with jammer_off() - the same command that stops every other jam
        mode.
        @param hops: List of (page, channel) tuples, in hop order. page
            must be 31 (channel 0-128) or 28 (channel 0-65) - 2.4GHz
            (page 0) is not supported by this on-chip hop command on this
            firmware. Each entry carries its own page (not a bare
            channel) since page 31 and page 28 use different frequency
            formulas at the same raw channel number. Internally collapsed
            into contiguous (page, start, end) ranges before sending -
            see _collapse_to_ranges() - so there is no small, fixed cap on
            len(hops) itself; a large contiguous request (e.g. 9-128)
            costs the same 3 wire bytes as a single channel.
        @param dwell_ms: milliseconds to jam each channel before hopping to
            the next (1-65535).
        '''
        self.capabilities.require(KBCapabilities.PHYJAM_HOP)
        if not hops:
            raise Exception('hops must be a non-empty list of (page, channel) tuples')
        for page, ch in hops:
            okPair = (page == 31 and 0 <= ch <= 128) or (page == 28 and 0 <= ch <= 65)
            if not okPair:
                raise Exception('Invalid (page=%d, channel=%d) - must be '
                                 '(31, 0-128) or (28, 0-65); 2.4GHz (page 0) is '
                                 'not supported by this on-chip hop command' % (page, ch))
        if dwell_ms < 1 or dwell_ms > 0xFFFF:
            raise Exception('dwell_ms must be 1-65535')

        ranges = self._collapse_to_ranges(hops)
        if len(ranges) > MAX_HOP_RANGES:
            raise Exception('hops collapses to %d contiguous ranges, more than the '
                             'firmware supports (%d) - it needs %d non-contiguous runs; '
                             'try fewer distinct/scattered spans' % (len(ranges), MAX_HOP_RANGES, len(ranges)))

        payload = struct.pack('<HB', dwell_ms, len(ranges))
        for page, chStart, chEnd in ranges:
            payload += bytes([page, chStart, chEnd])
        status = self.__command(CMD_JAM_HOP_ON, payload)
        if status[0] != STATUS_OK:
            raise Exception("Device rejected jam_hop_on()")

    def jam_hop_subg_on(self, hops: List[Tuple[int, int]], dwell_ms: int) -> None:
        '''
        Alias for jam_hop_on() - this driver's only on-chip hop mode is
        already sub-1GHz/cross-page, so both names do exactly the same
        thing here. Exists so generic tools (e.g. tools/subg_jam_hop.py)
        can call jam_hop_subg_on() uniformly across this driver and
        dev_cc1352p7.CC1352P7, which needs the distinct name since its own
        jam_hop_on() is a different, 2.4GHz-only hop mode.
        '''
        return self.jam_hop_on(hops, dwell_ms)

    def set_sync(self, sync: int = 0xA70F) -> Any:
        self.capabilities.require(KBCapabilities.SET_SYNC)
        raise Exception('Not supported: the CC1354P10 native IEEE 802.15.4 '
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
        RF_GET_RSSI_ERROR_VAL sentinel (see RFCC26X2.h) rather than
        returning the raw, misleading -128.
        '''
        payload = self.__command(CMD_GET_RSSI)
        if len(payload) < 1:
            raise Exception("Malformed GET_RSSI reply")
        rssi = struct.unpack('b', payload[0:1])[0]
        if rssi == RF_GET_RSSI_ERROR_VAL:
            return None
        return rssi
