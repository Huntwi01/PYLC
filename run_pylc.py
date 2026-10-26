#!/usr/bin/env python3
"""Run a PYLC ladder program with the multi-process runtime.

Usage:
    python3 run_pylc.py [program.json] [--period-ms 10]

Processes (same shape as the original prototype, generalized):
  1. LogicEngine  — the scan engine evaluating the JSON program.
  2. Simulator    — a scripted conveyor demo driving the input tags,
                    doubling as the watchdog monitor (emergency halt if
                    the engine's Watchdog tag stalls >100 ms).
  3. Logger       — prints the tag state twice a second.

The scripted demo tells the seal-in story end to end:
  press Start -> motor latches -> parts count -> gate opens -> motor
  drops and STAYS off after the gate closes (seal broken) -> press
  Start again -> motor restarts -> press Stop -> motor stops.
"""

import argparse
import ctypes
import multiprocessing
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from pylc.program import load_program          # noqa: E402
from pylc.engine import run_engine_process     # noqa: E402
from pylc.tags import TagTable                 # noqa: E402


# ==========================================
# Process 2: Scripted simulator + watchdog
# ==========================================
def simulator_process(table, shared, running_flag):
    def w(name, value):
        with shared.get_lock():
            table.set(name, value)

    def r(name):
        with shared.get_lock():
            return table.get(name)

    t0 = time.perf_counter()
    last_watchdog = -1
    stalled_for = 0.0
    tick = 0.05

    while running_flag.value:
        t = time.perf_counter() - t0

        # --- watchdog monitor (as in the prototype) ---
        wd = r("Watchdog")
        if last_watchdog != -1:
            if wd == last_watchdog:
                stalled_for += tick
                if stalled_for >= 0.1:
                    print("\n[EMERGENCY HALT] Watchdog stalled >100ms — logic engine locked!")
                    running_flag.value = False
                    break
            else:
                stalled_for = 0.0
        last_watchdog = wd

        # --- scripted inputs ---
        w("StartPB", 1 if (1.0 <= t < 1.4 or 11.0 <= t < 11.4) else 0)
        w("StopPB", 1 if 15.0 <= t < 15.4 else 0)
        w("GateClosed", 0 if 8.0 <= t < 10.0 else 1)
        motor = r("MotorRun")
        # Photo-eye pulses while the motor runs: 0.25s on / 0.45s off.
        w("PartSensor", 1 if (motor and (t % 0.7) < 0.25) else 0)

        if t >= 17.0:
            print("\n[DEMO] Script complete — shutting down.")
            running_flag.value = False
            break
        time.sleep(tick)


# ==========================================
# Process 3: Logger
# ==========================================
def logger_process(table, shared, running_flag):
    while running_flag.value:
        with shared.get_lock():
            s = table.snapshot()
        gate = "CLOSED" if s["GateClosed"] else "OPEN"
        motor = "RUNNING" if s["MotorRun"] else "STOPPED"
        print(
            f"[GATE: {gate:6}] [START: {'PRESSED' if s['StartPB'] else '--':7}] "
            f"[STOP: {'PRESSED' if s['StopPB'] else '--':7}] "
            f"[EYE: {'ON' if s['PartSensor'] else 'off':3}] "
            f"-> [MOTOR: {motor:7}] | Parts: {s['PartsCounter']:3} | "
            f"Watchdog: {s['Watchdog']:6} | Scan: last {s['LastScanUs']:4}us max {s['MaxScanUs']:4}us",
            flush=True,
        )
        time.sleep(0.5)


# ==========================================
# Main
# ==========================================
def main():
    ap = argparse.ArgumentParser(description="Run a PYLC ladder program.")
    ap.add_argument("program", nargs="?", default="programs/conveyor.json")
    ap.add_argument("--period-ms", type=float, default=10.0)
    args = ap.parse_args()

    program = load_program(args.program)
    print(f"Loaded program: {program!r}")
    for tag in ("GateClosed", "StartPB", "StopPB", "PartSensor"):
        if tag not in program.table:
            print(f"[ERROR] The scripted simulator expects the conveyor tags; {tag!r} missing.")
            return 1

    initials = [program.table.definition(n).initial for n in program.table.names()]
    shared = multiprocessing.Array("i", initials)
    program.table.storage = shared
    table = program.table
    running_flag = multiprocessing.Value(ctypes.c_bool, True)

    procs = [
        multiprocessing.Process(
            target=run_engine_process,
            args=(program, shared, running_flag, args.period_ms / 1000.0),
            name="LogicEngine",
        ),
        multiprocessing.Process(
            target=simulator_process, args=(table, shared, running_flag), name="Simulator"
        ),
        multiprocessing.Process(
            target=logger_process, args=(table, shared, running_flag), name="Logger"
        ),
    ]
    for p in procs:
        p.start()
    try:
        while running_flag.value:
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\n[Ctrl+C] Shutting down...")
        running_flag.value = False
    for p in procs:
        p.join(timeout=3)

    with shared.get_lock():
        final = table.snapshot()
    print(
        f"\nFinal state: Parts={final['PartsCounter']}, Watchdog={final['Watchdog']} scans, "
        f"MaxScan={final['MaxScanUs']}us"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
