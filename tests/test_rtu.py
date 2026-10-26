import os
import select
import sys
import threading
import tty
import types
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
)

from pylc.program import load_program          # noqa: E402
from pylc.modbus import TagBridge, build_map   # noqa: E402
from pylc.rtu import (                         # noqa: E402
    RtuSlave, crc16_modbus, decode_frame, encode_frame, extract_frames,
    _serve_fd,
)

CONVEYOR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "programs", "conveyor.json"
)
WRITABLE = {"StartPB", "StopPB", "GateClosed", "PartsCounter"}
UNIT = 1


def make_slave(unit_id=UNIT):
    program = load_program(CONVEYOR)
    table = program.table
    mbmap = build_map(table, writable=WRITABLE)
    bridge = TagBridge(mbmap, table.get, table.set)
    return RtuSlave(bridge, unit_id), table


def pdu(hexstr):
    return bytes.fromhex(hexstr)


class TestCrc(unittest.TestCase):
    def test_check_value(self):
        # The standard CRC-16/MODBUS check value for the ASCII digits.
        self.assertEqual(crc16_modbus(b"123456789"), 0x4B37)

    def test_empty_input_is_init(self):
        self.assertEqual(crc16_modbus(b""), 0xFFFF)


class TestFraming(unittest.TestCase):
    def test_encode_decode_round_trip(self):
        frame = encode_frame(UNIT, pdu("03 0000 0004"))
        self.assertEqual(frame[0], UNIT)
        self.assertEqual(frame[1:-2], pdu("03 0000 0004"))
        # CRC is carried low byte first.
        self.assertEqual(
            int.from_bytes(frame[-2:], "little"), crc16_modbus(frame[:-2])
        )
        unit_id, body = decode_frame(frame)
        self.assertEqual(unit_id, UNIT)
        self.assertEqual(body, pdu("03 0000 0004"))

    def test_decode_rejects_corrupt_crc(self):
        frame = bytearray(encode_frame(UNIT, pdu("03 0000 0004")))
        frame[3] ^= 0xFF  # flip a data byte; CRC no longer matches
        with self.assertRaises(ValueError):
            decode_frame(bytes(frame))

    def test_decode_rejects_short_frame(self):
        with self.assertRaises(ValueError):
            decode_frame(b"\x01\x03\x00")


class TestExtractFrames(unittest.TestCase):
    def setUp(self):
        self.f1 = encode_frame(UNIT, pdu("03 0000 0004"))       # FC3, 8 bytes
        self.f2 = encode_frame(UNIT, pdu("05 0001 FF00"))       # FC5, 8 bytes
        self.f3 = encode_frame(UNIT, pdu("10 0000 0001 02 000A"))  # FC16, 11

    def test_two_frames_one_buffer(self):
        frames, rest = extract_frames(self.f1 + self.f2)
        self.assertEqual(frames, [self.f1, self.f2])
        self.assertEqual(rest, b"")

    def test_variable_length_frame(self):
        frames, rest = extract_frames(self.f1 + self.f3)
        self.assertEqual(frames, [self.f1, self.f3])
        self.assertEqual(rest, b"")

    def test_frame_fed_byte_by_byte(self):
        buf = b""
        got = []
        for i, byte in enumerate(self.f3):
            buf += bytes([byte])
            frames, buf = extract_frames(buf)
            if i < len(self.f3) - 1:
                self.assertEqual(frames, [])  # nothing until the last byte
            got += frames
        self.assertEqual(got, [self.f3])
        self.assertEqual(buf, b"")

    def test_incomplete_tail_is_kept(self):
        frames, rest = extract_frames(self.f1[:5])
        self.assertEqual(frames, [])
        self.assertEqual(rest, self.f1[:5])

    def test_garbage_byte_resync(self):
        # Unit 0x21 on purpose: with a noise byte in front, offset 1 holds
        # 0x21 — not a known function code — so the parser drops the
        # noise byte and locks onto the real frame behind it.
        frame = encode_frame(0x21, pdu("03 0000 0001"))
        frames, rest = extract_frames(b"\x07" + frame)
        self.assertEqual(frames, [frame])
        self.assertEqual(rest, b"")
        # Same recovery for noise between two frames.
        frames, rest = extract_frames(frame + b"\x07" + frame)
        self.assertEqual(frames, [frame, frame])
        self.assertEqual(rest, b"")


class TestSlave(unittest.TestCase):
    def setUp(self):
        self.slave, self.table = make_slave()

    def test_answers_own_unit(self):
        self.table.set("PartsCounter", 7)
        resp = self.slave.handle_frame(encode_frame(UNIT, pdu("03 0000 0001")))
        unit_id, body = decode_frame(resp)
        self.assertEqual(unit_id, UNIT)
        self.assertEqual(body, pdu("03 02 0007"))

    def test_silent_for_other_unit(self):
        resp = self.slave.handle_frame(encode_frame(2, pdu("03 0000 0001")))
        self.assertIsNone(resp)

    def test_silent_for_bad_crc(self):
        frame = bytearray(encode_frame(UNIT, pdu("05 0001 FF00")))
        frame[-1] ^= 0xFF
        self.assertIsNone(self.slave.handle_frame(bytes(frame)))
        self.assertEqual(self.table.get("StartPB"), 0)  # write not applied

    def test_broadcast_write_applies_silently(self):
        resp = self.slave.handle_frame(encode_frame(0, pdu("05 0001 FF00")))
        self.assertIsNone(resp)                        # never answered
        self.assertEqual(self.table.get("StartPB"), 1)  # but applied

    def test_broadcast_read_is_silent(self):
        resp = self.slave.handle_frame(encode_frame(0, pdu("03 0000 0001")))
        self.assertIsNone(resp)


def read_exact(fd, n, timeout=2.0):
    """Read exactly n bytes from the master fd (a response's length is
    known from the request that prompted it — the request-shaped
    stream parser belongs on the slave side, not here)."""
    import time
    buf = b""
    deadline = time.monotonic() + timeout
    while len(buf) < n and time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if not ready:
            continue
        chunk = os.read(fd, n - len(buf))
        if not chunk:
            break
        buf += chunk
    if len(buf) < n:
        raise AssertionError(f"timed out waiting for {n} bytes, got {buf!r}")
    return buf


class TestPtyEndToEnd(unittest.TestCase):
    """The whole stack over a pseudo-terminal pair — the loopback
    stands in for the RS485 wire (no serial hardware exists here)."""

    def test_read_and_write_over_pty(self):
        slave, table = make_slave()
        table.set("PartsCounter", 42)
        table.set("Watchdog", 0x1234)
        master_fd, slave_fd = os.openpty()
        tty.setraw(master_fd)
        tty.setraw(slave_fd)
        flag = types.SimpleNamespace(value=True)
        pump = threading.Thread(
            target=_serve_fd, args=(slave, slave_fd, flag), daemon=True
        )
        pump.start()
        try:
            # FC3: read registers 0..3. Response frame: unit + fc +
            # byte count + 8 data bytes + CRC = 13 bytes.
            os.write(master_fd, encode_frame(UNIT, pdu("03 0000 0004")))
            unit_id, body = decode_frame(read_exact(master_fd, 13))
            self.assertEqual(unit_id, UNIT)
            self.assertEqual(body, pdu("03 08 002A 1234 0000 0000"))

            # FC5: press StartPB (coil 1) — 8-byte echo response.
            os.write(master_fd, encode_frame(UNIT, pdu("05 0001 FF00")))
            unit_id, body = decode_frame(read_exact(master_fd, 8))
            self.assertEqual(body, pdu("05 0001 FF00"))
            self.assertEqual(table.get("StartPB"), 1)

            # FC1: coils now read GateClosed (initial 1) + StartPB set.
            # Response frame: unit + fc + byte count + 1 data + CRC = 6.
            os.write(master_fd, encode_frame(UNIT, pdu("01 0000 0005")))
            unit_id, body = decode_frame(read_exact(master_fd, 6))
            self.assertEqual(body, pdu("01 01 03"))
        finally:
            flag.value = False
            pump.join(timeout=2)
            os.close(master_fd)
            os.close(slave_fd)


if __name__ == "__main__":
    unittest.main()
