import os
import socket
import struct
import sys
import threading
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
)

from pylc.program import load_program          # noqa: E402
from pylc.modbus import (                      # noqa: E402
    TagBridge, build_map, handle_pdu, _client_loop,
)

CONVEYOR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "programs", "conveyor.json"
)
WRITABLE = {"StartPB", "StopPB", "GateClosed", "PartsCounter"}


def make_bridge(writable=WRITABLE):
    program = load_program(CONVEYOR)
    table = program.table
    mbmap = build_map(table, writable=writable)
    return TagBridge(mbmap, table.get, table.set), table, mbmap


def pdu(hexstr):
    return bytes.fromhex(hexstr)


class TestMap(unittest.TestCase):
    def test_coils_and_registers_follow_definition_order(self):
        _, _, mbmap = make_bridge()
        self.assertEqual(
            [e.tag for e in mbmap.coils],
            ["GateClosed", "StartPB", "StopPB", "PartSensor", "MotorRun"],
        )
        self.assertEqual(
            [e.tag for e in mbmap.registers],
            ["PartsCounter", "Watchdog", "LastScanUs", "MaxScanUs"],
        )
        self.assertEqual([e.address for e in mbmap.coils], [0, 1, 2, 3, 4])

    def test_writable_policy(self):
        _, _, mbmap = make_bridge()
        coils = {e.tag: e.writable for e in mbmap.coils}
        regs = {e.tag: e.writable for e in mbmap.registers}
        self.assertTrue(coils["StartPB"])
        self.assertFalse(coils["MotorRun"])     # output: read-only to masters
        self.assertFalse(coils["PartSensor"])   # sensor: read-only
        self.assertTrue(regs["PartsCounter"])   # resettable totalizer
        self.assertFalse(regs["Watchdog"])      # telemetry: read-only


class TestPdu(unittest.TestCase):
    def setUp(self):
        self.bridge, self.table, _ = make_bridge()

    def test_read_holding_registers(self):
        self.table.set("PartsCounter", 7)
        self.table.set("Watchdog", 0x1234)
        resp = handle_pdu(pdu("03 0000 0002"), self.bridge)
        self.assertEqual(resp, pdu("03 04 0007 1234"))

    def test_read_input_registers_mirror(self):
        self.table.set("PartsCounter", 9)
        resp = handle_pdu(pdu("04 0000 0001"), self.bridge)
        self.assertEqual(resp, pdu("04 02 0009"))

    def test_read_coils_packs_lsb_first(self):
        # GateClosed initial 1, others 0 -> first byte 0b00000001
        resp = handle_pdu(pdu("01 0000 0005"), self.bridge)
        self.assertEqual(resp, pdu("01 01 01"))
        self.table.set("StartPB", 1)
        self.table.set("MotorRun", 1)
        resp = handle_pdu(pdu("01 0000 0005"), self.bridge)
        self.assertEqual(resp, pdu("01 01 13"))  # bits 0, 1 and 4

    def test_discrete_inputs_mirror_coils(self):
        resp = handle_pdu(pdu("02 0000 0001"), self.bridge)
        self.assertEqual(resp, pdu("02 01 01"))

    def test_write_single_coil(self):
        resp = handle_pdu(pdu("05 0001 FF00"), self.bridge)
        self.assertEqual(resp, pdu("05 0001 FF00"))
        self.assertEqual(self.table.get("StartPB"), 1)
        resp = handle_pdu(pdu("05 0001 0000"), self.bridge)
        self.assertEqual(self.table.get("StartPB"), 0)

    def test_write_readonly_coil_refused(self):
        resp = handle_pdu(pdu("05 0004 FF00"), self.bridge)  # MotorRun
        self.assertEqual(resp, pdu("85 02"))
        self.assertEqual(self.table.get("MotorRun"), 0)

    def test_write_bad_coil_value_refused(self):
        resp = handle_pdu(pdu("05 0001 1234"), self.bridge)
        self.assertEqual(resp, pdu("85 03"))

    def test_write_single_register(self):
        resp = handle_pdu(pdu("06 0000 01F4"), self.bridge)  # PartsCounter = 500
        self.assertEqual(resp, pdu("06 0000 01F4"))
        self.assertEqual(self.table.get("PartsCounter"), 500)

    def test_write_readonly_register_refused(self):
        resp = handle_pdu(pdu("06 0001 0009"), self.bridge)  # Watchdog
        self.assertEqual(resp, pdu("86 02"))

    def test_write_multiple_registers_all_or_nothing(self):
        # PartsCounter (ok) + Watchdog (read-only) -> whole write refused
        resp = handle_pdu(pdu("10 0000 0002 04 000A 000B"), self.bridge)
        self.assertEqual(resp, pdu("90 02"))
        self.assertEqual(self.table.get("PartsCounter"), 0)

    def test_write_multiple_coils(self):
        resp = handle_pdu(pdu("0F 0000 0002 01 02"), self.bridge)  # Gate=0, Start=1
        self.assertEqual(resp, pdu("0F 0000 0002"))
        self.assertEqual(self.table.get("GateClosed"), 0)
        self.assertEqual(self.table.get("StartPB"), 1)

    def test_out_of_range_read_refused(self):
        resp = handle_pdu(pdu("03 0063 0001"), self.bridge)
        self.assertEqual(resp, pdu("83 02"))
        resp = handle_pdu(pdu("01 0009 0001"), self.bridge)
        self.assertEqual(resp, pdu("81 02"))

    def test_bad_quantity_refused(self):
        resp = handle_pdu(pdu("03 0000 0000"), self.bridge)
        self.assertEqual(resp, pdu("83 03"))
        resp = handle_pdu(pdu("01 0000 07D1"), self.bridge)  # 2001 bits
        self.assertEqual(resp, pdu("81 03"))

    def test_unknown_function_refused(self):
        resp = handle_pdu(pdu("07 0000"), self.bridge)
        self.assertEqual(resp, pdu("87 01"))

    def test_register_value_is_low_16_bits(self):
        self.table.set("Watchdog", 0x0001_0005)  # int32 wraps past 16 bits
        resp = handle_pdu(pdu("03 0001 0001"), self.bridge)
        self.assertEqual(resp, pdu("03 02 0005"))


class TestFraming(unittest.TestCase):
    def test_mbap_round_trip_over_socketpair(self):
        bridge, table, _ = make_bridge()
        table.set("PartsCounter", 42)
        client, server_side = socket.socketpair()
        t = threading.Thread(target=_client_loop, args=(server_side, bridge), daemon=True)
        t.start()
        try:
            # tid=1, protocol=0, length=6, unit=1, PDU = FC3 addr 0 qty 1
            client.sendall(struct.pack(">HHHB", 1, 0, 6, 1) + pdu("03 0000 0001"))
            head = client.recv(7)
            tid, pid, length, uid = struct.unpack(">HHHB", head)
            self.assertEqual((tid, pid, uid), (1, 0, 1))
            body = client.recv(length - 1)
            self.assertEqual(body, pdu("03 02 002A"))  # 42
        finally:
            client.close()
            t.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
