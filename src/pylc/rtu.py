"""Modbus RTU (RS485) gateway: the same tag table on a serial bus.

The protocol work is shared with the TCP gateway — :func:`handle_pdu`
in :mod:`pylc.modbus` turns a PDU into a response PDU regardless of
transport. RTU adds three things of its own, all implemented here in
pure stdlib like the rest of the core:

- **Framing**: a frame is ``unit id (1) + PDU + CRC16 (2, low byte
  first)``. There is no length field and no MBAP header.
- **Addressing**: each slave answers only its own unit id. Broadcast
  (unit 0) writes are applied by every slave but never answered —
  silence on the bus is how a master knows the write went everywhere.
- **Error checking**: a frame whose CRC fails is dropped silently.
  On a shared, noisy RS485 pair, answering a garbled frame would be
  worse than ignoring it.

Frame boundaries deserve an honest note. Real RTU delimits frames by
*silence* (3.5 character times of quiet line), with the CRC as the
final arbiter. The stream parser here (:func:`extract_frames`) uses
the request length implied by the function code as its primary cue —
the same heuristic most software slaves fall back on — and the CRC
still decides whether a candidate frame is real. Inter-frame timing
is not enforced; on the point-to-point links and USB-RS485 dongles
this targets, length + CRC is what every practical master waits for
anyway.

Serial I/O uses ``os`` + ``termios`` directly (raw 8N1) — no pyserial,
keeping the runtime's zero-dependency promise. The serve loop polls
with :func:`select.select`, so it honors the same ``running_flag``
shutdown contract as the engine and TCP processes.
"""

import os
import select
import termios

from .modbus import TagBridge, handle_pdu

BROADCAST = 0
MAX_FRAME = 256  # spec ceiling for an RTU frame, ADU included
_FIXED_LEN_FCS = (1, 2, 3, 4, 5, 6)   # request is unit+fc+4 data+CRC = 8
_VAR_LEN_FCS = (15, 16)                # + byte count + data + CRC

_BAUD_CONSTANTS = {
    1200: termios.B1200,
    2400: termios.B2400,
    4800: termios.B4800,
    9600: termios.B9600,
    19200: termios.B19200,
    38400: termios.B38400,
    57600: termios.B57600,
    115200: termios.B115200,
}


# ==========================================
# CRC + framing
# ==========================================
def crc16_modbus(data):
    """CRC-16/MODBUS of ``data`` (poly 0xA001 reflected, init 0xFFFF).

    Check value: ``crc16_modbus(b"123456789") == 0x4B37``.
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def encode_frame(unit_id, pdu):
    """Wrap a PDU in an RTU frame: unit byte + PDU + CRC, low byte first."""
    body = bytes([unit_id]) + bytes(pdu)
    return body + crc16_modbus(body).to_bytes(2, "little")


def decode_frame(frame):
    """Unwrap an RTU frame to ``(unit_id, pdu)``, validating the CRC.

    Raises ValueError for frames shorter than unit+fc+CRC (4 bytes)
    and for CRC mismatches — callers treat both as "not a frame".
    """
    if len(frame) < 4:
        raise ValueError(f"RTU frame too short: {len(frame)} bytes")
    carried = int.from_bytes(frame[-2:], "little")
    if crc16_modbus(frame[:-2]) != carried:
        raise ValueError("RTU frame CRC mismatch")
    return frame[0], bytes(frame[1:-2])


def _expected_len(buffer):
    """Total frame length implied by the function code at offset 1,
    or None if more bytes are needed to tell, or -1 if the byte at
    offset 1 cannot begin a plausible request frame."""
    fc = buffer[1]
    if fc in _FIXED_LEN_FCS:
        return 8
    if fc in _VAR_LEN_FCS:
        if len(buffer) < 7:
            return None  # byte count is the frame's 7th byte; not here yet
        total = 9 + buffer[6]
        return total if total <= MAX_FRAME else -1
    return -1


def extract_frames(buffer):
    """Split a growing byte stream into complete RTU frames.

    Returns ``(frames, rest)``: every complete frame in order, plus the
    incomplete tail to prepend to the next read. Because RTU carries
    no length field, completeness is judged by the function code (see
    :func:`_expected_len`).

    Resync: if the byte at offset 1 is not a function code this slave
    understands, the first byte cannot start a plausible frame — it is
    dropped and parsing retries from the next byte. This recovers the
    stream after line noise or a sender that started mid-frame, at the
    cost of a byte per retry. Frames are NOT CRC-checked here;
    :func:`decode_frame` / :class:`RtuSlave` remain the arbiter, so a
    well-shaped but corrupt frame is extracted and then refused.
    """
    buf = bytes(buffer)
    frames = []
    while len(buf) >= 2:
        total = _expected_len(buf)
        if total is None or len(buf) < total:
            break  # incomplete tail — wait for more bytes
        if total == -1:
            buf = buf[1:]  # implausible start: drop one byte, resync
            continue
        frames.append(buf[:total])
        buf = buf[total:]
    return frames, buf


# ==========================================
# Slave
# ==========================================
class RtuSlave:
    """Answers RTU frames for one unit id through a TagBridge."""

    def __init__(self, bridge, unit_id):
        self.bridge = bridge
        self.unit_id = unit_id

    def handle_frame(self, frame):
        """Return the response frame, or None for silence.

        Silence is the answer for: a bad CRC, a frame addressed to
        another unit, and every broadcast. Broadcast *writes* are
        still applied (that is their point) — :func:`handle_pdu` runs
        and its response is simply discarded.
        """
        try:
            unit_id, pdu = decode_frame(frame)
        except ValueError:
            return None
        if unit_id == BROADCAST:
            handle_pdu(pdu, self.bridge)
            return None
        if unit_id != self.unit_id:
            return None
        response = handle_pdu(pdu, self.bridge)
        if not response:
            return None
        return encode_frame(self.unit_id, response)


# ==========================================
# Serial serving (own process for the live runtime)
# ==========================================
def _open_serial(device_path, baud):
    """Open a serial device raw, 8N1, at ``baud`` — stdlib termios only."""
    if baud not in _BAUD_CONSTANTS:
        raise ValueError(
            f"unsupported baud {baud!r}; choose one of {sorted(_BAUD_CONSTANTS)}"
        )
    speed = _BAUD_CONSTANTS[baud]
    fd = os.open(device_path, os.O_RDWR | os.O_NOCTTY)
    attrs = termios.tcgetattr(fd)
    attrs[0] &= ~(termios.BRKINT | termios.ICRNL | termios.INPCK
                  | termios.ISTRIP | termios.IXON)          # iflag: raw in
    attrs[1] &= ~termios.OPOST                             # oflag: raw out
    attrs[2] &= ~(termios.PARENB | termios.CSTOPB | termios.CSIZE)
    attrs[2] |= termios.CS8 | termios.CREAD | termios.CLOCAL  # cflag: 8N1
    attrs[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON
                  | termios.ISIG | termios.IEXTEN)          # lflag: raw
    attrs[4] = speed                                        # ispeed
    attrs[5] = speed                                        # ospeed
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 1                             # 0.1 s read tick
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    return fd


def _serve_fd(slave, fd, running_flag):
    """Pump one open serial fd until ``running_flag`` clears."""
    buffer = b""
    while running_flag.value:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if not ready:
            continue
        try:
            chunk = os.read(fd, 256)
        except OSError:
            break
        if not chunk:
            continue
        buffer += chunk
        frames, buffer = extract_frames(buffer)
        for frame in frames:
            response = slave.handle_frame(frame)
            if response is not None:
                try:
                    os.write(fd, response)
                except OSError:
                    return


def serve_rtu(bridge, unit_id, device_path, baud, running_flag):
    """Serve Modbus RTU on a serial device until ``running_flag`` clears."""
    fd = _open_serial(device_path, baud)
    print(
        f"PYLC Modbus RTU slave unit {unit_id} on {device_path} @ {baud} 8N1",
        flush=True,
    )
    try:
        _serve_fd(RtuSlave(bridge, unit_id), fd, running_flag)
    finally:
        os.close(fd)


def run_rtu_process(table, shared_storage, running_flag, mbmap,
                    device_path, baud=9600, unit_id=1):
    """Multiprocessing entry point: bridge the shared tag table and serve RTU.

    Mirrors :func:`pylc.modbus.run_modbus_process`: the tag table's
    storage is the shared array, and every access is locked.
    """
    table.storage = shared_storage

    def get_value(name):
        with shared_storage.get_lock():
            return table.get(name)

    def set_value(name, value):
        with shared_storage.get_lock():
            table.set(name, int(value))

    serve_rtu(
        TagBridge(mbmap, get_value, set_value), unit_id, device_path, baud,
        running_flag,
    )
