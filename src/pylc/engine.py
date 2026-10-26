"""The scan engine: PYLC's equivalent of a PLC's cyclic task.

One *scan* = evaluate every rung once, top to bottom, against the tag
storage, exactly like a real controller. Outputs written by a rung are
visible to the rungs below it in the same scan.

Telemetry is written back into ordinary tags when the program defines
them, so any HMI/logger can read engine health with no special API:

- ``Watchdog``   (INT) incremented once per completed scan;
  wraps back to 0 after 100,000.
- ``LastScanUs`` (INT) rung-evaluation time of the last scan, in µs.
- ``MaxScanUs``  (INT) worst rung-evaluation time since startup, in µs.

Honesty note: this loop targets *soft* real-time. The period is kept by
sleeping for (period − evaluation time), overruns are possible under
OS scheduling pressure, and the stats tags exist precisely so the
runtime can prove what it actually achieved instead of claiming a
number. Python does not do hard real-time; the eventual Rust scan
engine exists for that.

Timers: ``TON``/``TOF`` rungs keep their state in the engine (one
record per TIMER tag), accumulate *wall-clock* time between scans
(measured with an injectable clock so tests can step time
deterministically), cap ACC at the preset like a real timer, and
publish ACC back into the tag slot every scan. If the ACC slot is
written from outside (a RES rung, an HMI), the timer adopts the
written value — the slot is the shared truth.
"""

import time


class Engine:
    """Evaluates a compiled Program against shared tag storage."""

    def __init__(self, program, storage=None, time_fn=None):
        self.program = program
        self.storage = storage if storage is not None else program.table.storage
        self._prev_power = [False] * len(program.rungs)
        self._time_fn = time.perf_counter if time_fn is None else time_fn
        self._last_scan_t = None
        self._timers = {}
        for rung in program.rungs:
            if rung["output_op"] in ("TON", "TOF"):
                idx = rung["output_idx"]
                self._timers[idx] = {
                    "preset": float(rung["preset_ms"]),
                    "acc": float(self.storage[idx]),
                    "written": self.storage[idx],
                    "bits": {"EN": False, "TT": False, "DN": False},
                }
        table = program.table
        self._watchdog_idx = table.index("Watchdog") if "Watchdog" in table else None
        self._last_us_idx = table.index("LastScanUs") if "LastScanUs" in table else None
        self._max_us_idx = table.index("MaxScanUs") if "MaxScanUs" in table else None
        self.scans_completed = 0

    # -- one scan ------------------------------------------------------
    def scan(self):
        """Evaluate all rungs once. Returns evaluation time in seconds."""
        start = time.perf_counter()
        now = self._time_fn()
        dt_ms = 0.0 if self._last_scan_t is None else (now - self._last_scan_t) * 1000.0
        self._last_scan_t = now
        storage = self.storage
        for i, rung in enumerate(self.program.rungs):
            power = False
            for branch in rung["branches"]:
                conducts = True
                for is_xic, idx, bit in branch:
                    if bit is None:
                        value = storage[idx] != 0
                    else:
                        value = self._timers[idx]["bits"][bit]
                    if value != is_xic:
                        conducts = False
                        break
                if conducts:
                    power = True
                    break

            op = rung["output_op"]
            out_idx = rung["output_idx"]
            if op == "OTE":
                storage[out_idx] = 1 if power else 0
            elif op == "CTU":
                if power and not self._prev_power[i]:
                    storage[out_idx] += 1
            elif op == "RES":
                if power:
                    storage[out_idx] = 0
            elif op in ("TON", "TOF"):
                self._run_timer(op, out_idx, power, dt_ms)
            self._prev_power[i] = power

        elapsed = time.perf_counter() - start
        self.scans_completed += 1

        if self._watchdog_idx is not None:
            wd = self.storage[self._watchdog_idx] + 1
            self.storage[self._watchdog_idx] = 0 if wd > 100_000 else wd
        if self._last_us_idx is not None:
            us = int(elapsed * 1_000_000)
            self.storage[self._last_us_idx] = us
            if self._max_us_idx is not None and us > self.storage[self._max_us_idx]:
                self.storage[self._max_us_idx] = us
        return elapsed

    def _run_timer(self, op, idx, power, dt_ms):
        """Advance one TON/TOF timer by dt_ms and publish its ACC."""
        st = self._timers[idx]
        storage = self.storage
        # An outside write to the ACC slot (RES rung, HMI, Modbus)
        # restarts the timer from the written value.
        if storage[idx] != st["written"]:
            st["acc"] = float(storage[idx])
        bits = st["bits"]
        preset = st["preset"]
        # ACC is kept as a float internally, but a timer's accumulated
        # value is an integer number of milliseconds — round before any
        # comparison or publication so float dust (2999.9999 ms) can
        # never leave a finished timer reading 2999 or DN unset.
        if op == "TON":
            if power:
                bits["EN"] = True
                if round(st["acc"]) < preset:
                    st["acc"] = min(preset, st["acc"] + dt_ms)
                bits["DN"] = round(st["acc"]) >= preset
                bits["TT"] = not bits["DN"]
            else:
                st["acc"] = 0.0
                bits["EN"] = bits["TT"] = bits["DN"] = False
        else:  # TOF
            if power:
                st["acc"] = 0.0
                bits["EN"] = True
                bits["TT"] = False
                bits["DN"] = True
            else:
                bits["EN"] = False
                if bits["DN"]:
                    if round(st["acc"]) < preset:
                        st["acc"] = min(preset, st["acc"] + dt_ms)
                    bits["DN"] = round(st["acc"]) < preset
                    bits["TT"] = bits["DN"]
                else:
                    bits["TT"] = False
        storage[idx] = int(round(st["acc"]))
        st["written"] = storage[idx]

    # -- cyclic task ---------------------------------------------------
    def run(self, running_flag, period_s=0.010):
        """Scan every ``period_s`` seconds until ``running_flag`` clears.

        Sleeps only for the time left in the period after evaluation, so
        the scan rate tracks the target instead of drifting by the
        evaluation time each cycle.
        """
        while running_flag.value:
            cycle_start = time.perf_counter()
            self.scan()
            remaining = period_s - (time.perf_counter() - cycle_start)
            if remaining > 0:
                time.sleep(remaining)


def run_engine_process(program, shared_storage, running_flag, period_s=0.010):
    """Multiprocessing entry point: run the scan loop in its own process.

    The tag table's storage is swapped for the shared array so this
    process and the simulator/logger processes all see one tag space.
    """
    program.table.storage = shared_storage
    engine = Engine(program, shared_storage)
    engine.run(running_flag, period_s)
