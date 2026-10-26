import os
import sys
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
)

from pylc.program import parse_program, load_program, ProgramError  # noqa: E402
from pylc.engine import Engine  # noqa: E402

CONVEYOR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "programs", "conveyor.json"
)


def make_engine():
    program = load_program(CONVEYOR)
    return Engine(program), program.table


class TestSealIn(unittest.TestCase):
    def test_motor_latches_and_seal_breaks(self):
        eng, tags = make_engine()
        eng.scan()
        self.assertEqual(tags.get("MotorRun"), 0)

        # Momentary start press -> motor runs...
        tags.set("StartPB", 1)
        eng.scan()
        self.assertEqual(tags.get("MotorRun"), 1)
        # ...and stays running after the button is released (seal-in).
        tags.set("StartPB", 0)
        eng.scan()
        self.assertEqual(tags.get("MotorRun"), 1)

        # Stop breaks the seal.
        tags.set("StopPB", 1)
        eng.scan()
        self.assertEqual(tags.get("MotorRun"), 0)
        tags.set("StopPB", 0)
        eng.scan()
        self.assertEqual(tags.get("MotorRun"), 0)

    def test_gate_interlock(self):
        eng, tags = make_engine()
        tags.set("StartPB", 1)
        eng.scan()
        tags.set("StartPB", 0)
        eng.scan()
        self.assertEqual(tags.get("MotorRun"), 1)

        # Opening the gate drops the motor immediately...
        tags.set("GateClosed", 0)
        eng.scan()
        self.assertEqual(tags.get("MotorRun"), 0)
        # ...and closing it again does NOT restart the motor:
        # the seal is broken until Start is pressed again.
        tags.set("GateClosed", 1)
        eng.scan()
        self.assertEqual(tags.get("MotorRun"), 0)
        tags.set("StartPB", 1)
        eng.scan()
        self.assertEqual(tags.get("MotorRun"), 1)

    def test_start_does_nothing_with_gate_open(self):
        eng, tags = make_engine()
        tags.set("GateClosed", 0)
        tags.set("StartPB", 1)
        eng.scan()
        self.assertEqual(tags.get("MotorRun"), 0)


class TestCounter(unittest.TestCase):
    def test_ctu_counts_rising_edges_only(self):
        eng, tags = make_engine()
        # Sensor held ON across many scans = exactly one part.
        tags.set("PartSensor", 1)
        for _ in range(5):
            eng.scan()
        self.assertEqual(tags.get("PartsCounter"), 1)
        # Off, then on again = second part.
        tags.set("PartSensor", 0)
        eng.scan()
        tags.set("PartSensor", 1)
        eng.scan()
        self.assertEqual(tags.get("PartsCounter"), 2)

    def test_res_resets_counter(self):
        program = parse_program(
            {
                "name": "t",
                "tags": [
                    {"name": "Eye", "type": "BOOL"},
                    {"name": "Rst", "type": "BOOL"},
                    {"name": "Count", "type": "INT"},
                ],
                "rungs": [
                    {"branches": [[{"op": "XIC", "tag": "Eye"}]],
                     "output": {"op": "CTU", "tag": "Count"}},
                    {"branches": [[{"op": "XIC", "tag": "Rst"}]],
                     "output": {"op": "RES", "tag": "Count"}},
                ],
            }
        )
        eng, tags = Engine(program), program.table
        tags.set("Eye", 1)
        eng.scan()
        self.assertEqual(tags.get("Count"), 1)
        tags.set("Rst", 1)
        eng.scan()
        self.assertEqual(tags.get("Count"), 0)


class TestLogicOps(unittest.TestCase):
    def test_xio_and_parallel_branches(self):
        program = parse_program(
            {
                "name": "t",
                "tags": [
                    {"name": "A", "type": "BOOL"},
                    {"name": "B", "type": "BOOL"},
                    {"name": "Out", "type": "BOOL"},
                ],
                "rungs": [
                    {
                        # Out = (XIO A) OR (XIC B)
                        "branches": [
                            [{"op": "XIO", "tag": "A"}],
                            [{"op": "XIC", "tag": "B"}],
                        ],
                        "output": {"op": "OTE", "tag": "Out"},
                    }
                ],
            }
        )
        eng, tags = Engine(program), program.table
        eng.scan()  # A=0 -> XIO conducts
        self.assertEqual(tags.get("Out"), 1)
        tags.set("A", 1)
        eng.scan()  # A=1 blocks branch 1, B=0 blocks branch 2
        self.assertEqual(tags.get("Out"), 0)
        tags.set("B", 1)
        eng.scan()
        self.assertEqual(tags.get("Out"), 1)


class TestTelemetry(unittest.TestCase):
    def test_watchdog_and_scan_stats(self):
        eng, tags = make_engine()
        for _ in range(3):
            eng.scan()
        self.assertEqual(tags.get("Watchdog"), 3)
        self.assertGreater(tags.get("MaxScanUs"), 0)
        self.assertGreaterEqual(tags.get("MaxScanUs"), tags.get("LastScanUs"))

    def test_watchdog_wraps_after_100k(self):
        eng, tags = make_engine()
        tags.set("Watchdog", 99_999)
        eng.scan()
        self.assertEqual(tags.get("Watchdog"), 100_000)
        eng.scan()
        self.assertEqual(tags.get("Watchdog"), 0)
        eng.scan()
        self.assertEqual(tags.get("Watchdog"), 1)


class TestValidation(unittest.TestCase):
    def test_unknown_contact_tag_rejected(self):
        with self.assertRaises(ProgramError):
            parse_program(
                {
                    "name": "t",
                    "tags": [{"name": "A", "type": "BOOL"}],
                    "rungs": [
                        {"branches": [[{"op": "XIC", "tag": "Nope"}]],
                         "output": {"op": "OTE", "tag": "A"}}
                    ],
                }
            )

    def test_unknown_output_op_rejected(self):
        with self.assertRaises(ProgramError):
            parse_program(
                {
                    "name": "t",
                    "tags": [{"name": "A", "type": "BOOL"}],
                    "rungs": [
                        {"branches": [[{"op": "XIC", "tag": "A"}]],
                         "output": {"op": "TON", "tag": "A"}}
                    ],
                }
            )

    def test_ctu_on_bool_tag_rejected(self):
        with self.assertRaises(ProgramError):
            parse_program(
                {
                    "name": "t",
                    "tags": [{"name": "A", "type": "BOOL"}],
                    "rungs": [
                        {"branches": [[{"op": "XIC", "tag": "A"}]],
                         "output": {"op": "CTU", "tag": "A"}}
                    ],
                }
            )


if __name__ == "__main__":
    unittest.main()
