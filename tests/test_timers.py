import os
import sys
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
)

from pylc.program import parse_program, load_program, ProgramError  # noqa: E402
from pylc.engine import Engine                                      # noqa: E402
from pylc.modbus import TagBridge, build_map                        # noqa: E402

FAN = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "programs", "fan_control.json"
)
STEP_S = 0.010  # 10 ms per scan, like the live runtime


class FakeClock:
    """Deterministic perf_counter stand-in: time only moves on advance()."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def make_engine(program):
    """Engine on a fake clock, primed with one scan (first-scan dt is 0,
    so after priming every step() advances timers by exactly 10 ms)."""
    clock = FakeClock()
    eng = Engine(program, time_fn=clock)
    eng.scan()
    return eng, program.table, clock


def step(eng, clock, scans):
    for _ in range(scans):
        clock.advance(STEP_S)
        eng.scan()


def B(name):
    return {"name": name, "type": "BOOL"}


def I(name):
    return {"name": name, "type": "INT"}


def T(name):
    return {"name": name, "type": "TIMER"}


def bits_program(preset_ms=1000):
    """In -> TON Delay; DN/TT/EN copied to plain BOOLs for assertion."""
    return parse_program({
        "name": "bits",
        "tags": [B("In"), T("Delay"), B("Done"), B("Timing"), B("Enabled")],
        "rungs": [
            {"branches": [[{"op": "XIC", "tag": "In"}]],
             "output": {"op": "TON", "tag": "Delay", "preset_ms": preset_ms}},
            {"branches": [[{"op": "XIC", "tag": "Delay.DN"}]],
             "output": {"op": "OTE", "tag": "Done"}},
            {"branches": [[{"op": "XIC", "tag": "Delay.TT"}]],
             "output": {"op": "OTE", "tag": "Timing"}},
            {"branches": [[{"op": "XIC", "tag": "Delay.EN"}]],
             "output": {"op": "OTE", "tag": "Enabled"}},
        ],
    })


class TestTon(unittest.TestCase):
    def test_bits_and_acc_over_time(self):
        eng, tags, clock = make_engine(bits_program())
        self.assertEqual(tags.get("Enabled"), 0)
        tags.set("In", 1)
        step(eng, clock, 1)
        self.assertEqual(tags.get("Delay"), 10)       # ACC in the tag slot
        self.assertEqual(tags.get("Enabled"), 1)      # EN follows rung power
        self.assertEqual(tags.get("Timing"), 1)       # TT while timing
        self.assertEqual(tags.get("Done"), 0)
        step(eng, clock, 98)
        self.assertEqual(tags.get("Delay"), 990)
        self.assertEqual(tags.get("Done"), 0)
        step(eng, clock, 1)                            # ACC reaches the preset
        self.assertEqual(tags.get("Delay"), 1000)      # ACC caps at preset
        self.assertEqual(tags.get("Done"), 1)
        self.assertEqual(tags.get("Timing"), 0)
        self.assertEqual(tags.get("Enabled"), 1)
        step(eng, clock, 50)                           # stays done, ACC pinned
        self.assertEqual(tags.get("Delay"), 1000)
        self.assertEqual(tags.get("Done"), 1)

    def test_power_loss_resets(self):
        eng, tags, clock = make_engine(bits_program())
        tags.set("In", 1)
        step(eng, clock, 50)
        self.assertEqual(tags.get("Delay"), 500)
        self.assertEqual(tags.get("Timing"), 1)
        tags.set("In", 0)
        step(eng, clock, 1)
        self.assertEqual(tags.get("Delay"), 0)
        self.assertEqual(tags.get("Done"), 0)
        self.assertEqual(tags.get("Timing"), 0)
        self.assertEqual(tags.get("Enabled"), 0)

    def test_zero_preset_done_immediately(self):
        eng, tags, clock = make_engine(bits_program(preset_ms=0))
        tags.set("In", 1)
        step(eng, clock, 1)
        self.assertEqual(tags.get("Done"), 1)
        self.assertEqual(tags.get("Timing"), 0)


class TestFanProgram(unittest.TestCase):
    """End-to-end through programs/fan_control.json (3000 ms presets)."""

    def setUp(self):
        self.program = load_program(FAN)
        self.eng, self.tags, self.clock = make_engine(self.program)

    def _start_motor(self):
        self.tags.set("StartPB", 1)
        step(self.eng, self.clock, 1)
        self.tags.set("StartPB", 0)
        self.assertEqual(self.tags.get("MotorRun"), 1)

    def _stop_motor(self):
        self.tags.set("StopPB", 1)
        step(self.eng, self.clock, 1)
        self.tags.set("StopPB", 0)
        self.assertEqual(self.tags.get("MotorRun"), 0)

    def test_ton_purge_delay(self):
        self._start_motor()                       # ACC = 10 after this scan
        step(self.eng, self.clock, 298)
        self.assertEqual(self.tags.get("PurgeDelay"), 2990)
        self.assertEqual(self.tags.get("PurgeReady"), 0)
        step(self.eng, self.clock, 1)
        self.assertEqual(self.tags.get("PurgeDelay"), 3000)
        self.assertEqual(self.tags.get("PurgeReady"), 1)

    def test_tof_fan_run_on(self):
        self._start_motor()
        self.assertEqual(self.tags.get("FanMotor"), 1)   # fan with the motor
        self._stop_motor()                               # run-on starts, ACC=10
        self.assertEqual(self.tags.get("FanMotor"), 1)
        step(self.eng, self.clock, 298)
        self.assertEqual(self.tags.get("FanRunOn"), 2990)
        self.assertEqual(self.tags.get("FanMotor"), 1)
        step(self.eng, self.clock, 1)                    # run-on elapses
        self.assertEqual(self.tags.get("FanRunOn"), 3000)
        self.assertEqual(self.tags.get("FanMotor"), 0)
        self.assertEqual(self.tags.get("PurgeReady"), 0)  # purge reset with motor

    def test_tof_retrigger_keeps_fan_running(self):
        self._start_motor()
        self._stop_motor()
        step(self.eng, self.clock, 100)                  # 1 s into the run-on
        self.assertEqual(self.tags.get("FanRunOn"), 1010)
        self.assertEqual(self.tags.get("FanMotor"), 1)
        self._start_motor()                              # motor back: ACC resets
        self.assertEqual(self.tags.get("FanRunOn"), 0)
        self.assertEqual(self.tags.get("FanMotor"), 1)

    def test_fan_never_runs_before_first_start(self):
        step(self.eng, self.clock, 400)                  # 4 s of idle scans
        self.assertEqual(self.tags.get("FanMotor"), 0)
        self.assertEqual(self.tags.get("FanRunOn"), 0)


class TestResOnTimer(unittest.TestCase):
    def test_res_zeroes_acc_and_done(self):
        program = parse_program({
            "name": "res",
            "tags": [B("In"), T("Delay"), B("ResetPB"), B("Done")],
            "rungs": [
                {"branches": [[{"op": "XIC", "tag": "In"}]],
                 "output": {"op": "TON", "tag": "Delay", "preset_ms": 500}},
                {"branches": [[{"op": "XIC", "tag": "Delay.DN"}]],
                 "output": {"op": "OTE", "tag": "Done"}},
                {"branches": [[{"op": "XIC", "tag": "ResetPB"}]],
                 "output": {"op": "RES", "tag": "Delay"}},
            ],
        })
        eng, tags, clock = make_engine(program)
        tags.set("In", 1)
        step(eng, clock, 50)
        self.assertEqual(tags.get("Delay"), 500)
        self.assertEqual(tags.get("Done"), 1)
        tags.set("ResetPB", 1)
        step(eng, clock, 1)          # RES rung writes 0 into the ACC slot
        tags.set("ResetPB", 0)
        step(eng, clock, 1)          # timer adopts 0, then advances one step
        self.assertEqual(tags.get("Delay"), 10)
        self.assertEqual(tags.get("Done"), 0)


class TestValidation(unittest.TestCase):
    def assert_bad(self, tags, rungs):
        with self.assertRaises(ProgramError):
            parse_program({"name": "bad", "tags": tags, "rungs": rungs})

    def test_ton_requires_timer_tag(self):
        self.assert_bad([B("In"), I("N")], [
            {"branches": [[{"op": "XIC", "tag": "In"}]],
             "output": {"op": "TON", "tag": "N", "preset_ms": 100}}])
        self.assert_bad([B("In"), B("Y")], [
            {"branches": [[{"op": "XIC", "tag": "In"}]],
             "output": {"op": "TON", "tag": "Y", "preset_ms": 100}}])

    def test_preset_must_be_nonnegative_int(self):
        for out in ({"op": "TOF", "tag": "Delay"},
                    {"op": "TOF", "tag": "Delay", "preset_ms": -5},
                    {"op": "TOF", "tag": "Delay", "preset_ms": "3000"}):
            self.assert_bad([B("In"), T("Delay")], [
                {"branches": [[{"op": "XIC", "tag": "In"}]], "output": out}])

    def test_bit_contacts_need_a_timer(self):
        self.assert_bad([I("N"), B("Y")], [
            {"branches": [[{"op": "XIC", "tag": "N.DN"}]],
             "output": {"op": "OTE", "tag": "Y"}}])
        self.assert_bad([B("In"), T("Delay"), B("Y")], [
            {"branches": [[{"op": "XIC", "tag": "In"}]],
             "output": {"op": "TON", "tag": "Delay", "preset_ms": 100}},
            {"branches": [[{"op": "XIC", "tag": "Delay.XX"}]],
             "output": {"op": "OTE", "tag": "Y"}}])

    def test_timer_has_no_plain_contact(self):
        self.assert_bad([B("In"), T("Delay"), B("Y")], [
            {"branches": [[{"op": "XIC", "tag": "In"}]],
             "output": {"op": "TON", "tag": "Delay", "preset_ms": 100}},
            {"branches": [[{"op": "XIC", "tag": "Delay"}]],
             "output": {"op": "OTE", "tag": "Y"}}])

    def test_one_driver_per_timer(self):
        rung = {"branches": [[{"op": "XIC", "tag": "In"}]],
                "output": {"op": "TON", "tag": "Delay", "preset_ms": 100}}
        self.assert_bad([B("In"), T("Delay")], [rung, dict(rung)])

    def test_bit_contact_needs_a_driver(self):
        self.assert_bad([T("Delay"), B("Y")], [
            {"branches": [[{"op": "XIC", "tag": "Delay.DN"}]],
             "output": {"op": "OTE", "tag": "Y"}}])

    def test_ote_and_res_type_rules(self):
        self.assert_bad([B("In"), T("Delay")], [
            {"branches": [[{"op": "XIC", "tag": "In"}]],
             "output": {"op": "OTE", "tag": "Delay"}}])
        self.assert_bad([B("In"), B("Y")], [
            {"branches": [[{"op": "XIC", "tag": "In"}]],
             "output": {"op": "RES", "tag": "Y"}}])


class TestTimerModbusMap(unittest.TestCase):
    def test_timers_map_to_registers_holding_acc(self):
        program = load_program(FAN)
        mbmap = build_map(program.table, writable=set())
        self.assertEqual(
            [e.tag for e in mbmap.coils],
            ["StartPB", "StopPB", "MotorRun", "PurgeReady", "FanMotor"],
        )
        self.assertEqual(
            [e.tag for e in mbmap.registers],
            ["PurgeDelay", "FanRunOn", "Watchdog", "LastScanUs", "MaxScanUs"],
        )
        eng, tags, clock = make_engine(program)
        bridge = TagBridge(mbmap, tags.get, tags.set)
        tags.set("StartPB", 1)
        step(eng, clock, 1)
        tags.set("StartPB", 0)
        step(eng, clock, 299)
        self.assertEqual(bridge.read_registers(0, 1), [3000])  # PurgeDelay ACC


if __name__ == "__main__":
    unittest.main()
