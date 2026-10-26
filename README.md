# PyPLC: The Open-Source Industrial Gateway & Soft-PLC Runtime

PyPLC is a high-performance, low-cost "Soft-PLC" runtime framework engineered to run on recycled legacy x86 hardware (such as Intel Core 2 Duo systems). By combining a real-time optimized Linux kernel with an asynchronous Python control engine, PyPLC eliminates expensive vendor lock-in, proprietary software licensing fees, and high-cost hardware replacement cycles for small businesses.

## 🚀 Key Technical Features

- **Hard Real-Time Execution:** Engineered for Linux environments patched with `PREEMPT_RT` to ensure deterministic execution loops.
- **High-Performance PCIe/USB Bridge:** Native support for isolated Industrial USB-to-RS485 converters and ultra-low latency PCIe x1 multi-port serial cards utilizing Direct Memory Access (DMA).
- **Asynchronous Shared Memory Map:** A concurrent data architecture leveraging `multiprocessing.Array` to separate high-priority machine safety logic from low-priority monitoring tasks without CPU bottlenecks.
- **Dual-Engine Hybrid Runtime:**
  - *Legacy Side:* A JSON-based Ladder Logic parser allowing traditional factory maintenance technicians to troubleshoot visually.
  - *Power Side:* A native Python integration leveraging `Numba` (Just-In-Time compilation) and `NumPy` for high-speed calculation and predictive maintenance.
- **Edge-to-Cloud Scada Panel:** Built-in hooks for a local Next.js HMI Web Dashboard and real-time database logging via PocketBase.

## 📍 Current Status (Oct 2026)

**Working today:**
- `soft_plc_prototype.py` — the original all-in-one proof of concept: shared-memory map, Numba logic core, watchdog with emergency halt, hardware simulator, and a retro Flask HMI for a conveyor cell.
- **JSON Ladder Runtime (v0.1)** — the concept below is now real code: named tags, ladder programs written in plain JSON, and a scan engine that executes them. See the next section.
- **Modbus/TCP gateway** — a Modbus slave implemented from the spec in pure stdlib maps any loaded program's tags to coils and registers; any SCADA/HMI master can poll or drive the runtime. See the gateway section.
- **Modbus RTU (RS485)** — the same PDU core over serial framing (unit address + CRC-16) in `src/pylc/rtu.py`; verified end-to-end over a virtual serial (PTY) loopback. A USB-RS485 adapter is all it needs for a physical bus.
- **Next.js HMI** (`hmi-dashboard/`) — a TypeScript/React operator panel (static export) talking to the demo API: live tag grid, DEMO/MANUAL controls, rung viewer, Modbus map. Complement to the built-in HMI, not a replacement.
- **Analytics** (`src/pylc/analytics.py` + `sample_tags.py` / `analyze_log.py`) — scan-health and production analytics over recorded tag samples: mean/p95/max scan evaluation, jitter, implied scan rate, throughput halves, and trend flags (rising scan time, throughput drop). Descriptive statistics, labeled as such — no ML claims.
- **Interactive web HMI demo** (`demo_web.py`) — the conveyor running live against a simulated cell, with DEMO/MANUAL modes: drive the start/stop/gate yourself, or watch the simulator's 20-second story.

**Still concept / planned:** PocketBase logging, PREEMPT_RT tuning guide. The long-term learning track is the Rust port of the scan engine.

## 🪜 JSON Ladder Runtime (v0.1)

A ladder program is a single JSON file — tags plus rungs — that a maintenance tech can open in any text editor:

```bash
python3 run_pylc.py                  # runs programs/conveyor.json (scripted demo)
python3 -m unittest discover -s tests
```

- **Tags** are named and typed (`BOOL` / `INT`) and live in one shared-memory map (`multiprocessing.Array`), so the engine, simulator, and any future HMI/driver all read the same tag space.
- **Rungs** are series/parallel logic: contacts in a branch are ANDed, branches are ORed. Contacts: `XIC` (examine if closed), `XIO` (examine if open). Outputs: `OTE` (coil), `CTU` (count-up on rising edge), `RES` (reset while energized). **Timers:** `TON` (on-delay) and `TOF` (off-delay) outputs drive a `TIMER` tag with a `preset_ms`; the tag's slot holds the accumulated milliseconds, and its status bits are read as dotted contacts — `PurgeDelay.DN` (done), `.TT` (timing), `.EN` (powered).
- **Engine telemetry is just tags**: if a program defines `Watchdog`, `LastScanUs`, or `MaxScanUs`, the engine writes its heartbeat and scan-time stats there — no special API for an HMI to learn.
- The runtime core is **pure Python standard library** — no third-party packages needed to execute a program.
- Programs are validated at load (unknown tag/op, wrong target type) and fail with the rung number, before a single scan runs.

First measured run (2-rung conveyor program, 10 ms target scan, dev VM): evaluation averages ~10 µs per scan, worst observed 135 µs, 1,689 scans in the 17 s scripted demo. Soft real-time, honestly reported — the stats tags exist so the runtime proves its numbers instead of claiming them. Legacy-hardware benchmarks are still to come.

`programs/conveyor.json` is the prototype's conveyor rewritten as a real program: a seal-in motor circuit — `(StartPB OR MotorRun) AND GateClosed AND NOT StopPB` — with a photo-eye parts counter. Opening the gate drops the motor and breaks the seal, exactly like the real circuit.

`programs/fan_control.json` is the timer companion over the same seal-in motor: a `TON` gives a 3-second purge delay after start (`PurgeDelay.DN` lights the purge-ready lamp), and a `TOF` gives the exhaust fan a 3-second run-on after the motor stops — the two timer patterns every real panel uses.

## 🌐 Modbus/TCP Gateway

`src/pylc/modbus.py` is a Modbus/TCP slave written from the protocol spec using only `socket` + `struct` — no `pymodbus`, keeping the runtime's zero-dependency promise. Loading a program builds its map automatically, in tag-definition order:

- **BOOL tags → coils** (FC1 read / FC5, FC15 write), mirrored read-only as **discrete inputs** (FC2).
- **INT tags → holding registers** (FC3 read / FC6, FC16 write), mirrored read-only as **input registers** (FC4). A register carries the low 16 bits of the tag value.
- Addresses are 0-based offsets (add 1 for classic 00001/40001 notation). A host-configured write whitelist decides which tags a master may write; outputs and telemetry refuse writes with exception 02, as do out-of-map addresses. Multi-writes are all-or-nothing.

The conveyor's map:

| Offset | Tag | Access |
|---|---|---|
| Coil 0 | GateClosed | read/write |
| Coil 1 | StartPB | read/write |
| Coil 2 | StopPB | read/write |
| Coil 3 | PartSensor | read-only |
| Coil 4 | MotorRun | read-only |
| Register 0 | PartsCounter | read/write (reset) |
| Register 1 | Watchdog | read-only |
| Register 2 | LastScanUs | read-only |
| Register 3 | MaxScanUs | read-only |

The gateway runs as part of the live demo (`python3 demo_web.py`, Modbus on TCP **1502**, web HMI on 8085; the HMI page prints the live map). Verified end-to-end with a raw-socket client: a master latched the motor via the StartPB coil, read the parts counter as it climbed, dropped the motor with a gate write, and was refused on read-only addresses. One honest PLC behavior: input tags are the image table — in DEMO mode the simulator (the "field") overwrites them every cycle, so drive inputs in MANUAL mode.

**RTU:** `src/pylc/rtu.py` carries the same PDUs over an RS485 serial line — unit address, PDU, CRC-16 — with broadcast writes applied silently and a stream parser that resyncs on garbage. It reuses `handle_pdu` unchanged (the TCP work was deliberately transport-independent) and is verified over a PTY loopback; `run_rtu_process(...)` spawns it alongside the other processes once a serial device exists.

## 📂 Repository File Structure

```text
pyplc/
├── run_pylc.py               # Runtime entry point (engine + simulator + logger)
├── demo_web.py               # Live web HMI + interactive demo (spawns the Modbus gateway)
├── sample_tags.py            # Record tag samples from the live API to CSV
├── analyze_log.py            # Print an analytics report for a sample CSV
├── programs/
│   ├── conveyor.json         # Example ladder program (seal-in conveyor)
│   └── fan_control.json      # Timer demo (TON purge delay + TOF fan run-on)
├── src/
│   └── pylc/
│       ├── tags.py           # Named tag table over shared memory
│       ├── program.py        # JSON program loader / validator / compiler
│       ├── engine.py         # The cyclic scan engine + telemetry tags
│       ├── modbus.py         # Modbus/TCP gateway (stdlib slave, tag map)
│       ├── rtu.py              # Modbus RTU slave (serial framing + CRC-16)
│       └── analytics.py        # Scan-health + production analytics
├── tests/
│   ├── test_ladder.py        # Seal-in, interlock, counter, validation tests
│   ├── test_modbus.py        # Map, PDU, and framing tests
│   ├── test_timers.py        # TON/TOF behavior, bits, RES, validation
│   ├── test_rtu.py           # RTU CRC, framing, parser, PTY end-to-end
│   └── test_analytics.py     # Analytics stats, trends, flags (72 tests total)
├── hmi-dashboard/            # Next.js HMI (TypeScript/React, static export)
├── soft_plc_prototype.py     # Original v0 all-in-one prototype (Flask HMI)
├── requirements.txt          # Prototype deps (flask, numpy, numba)
└── README.md

Planned, not yet built: config/hardware_config.toml, drivers/modbus_rtu_driver.py
(RS485), analytics/predictive.py, hmi-dashboard/ (Next.js), CI workflows.

[ Physical Sensors ] ──(RS485 Bus)──> [ PCIe Card / DMA ]
                                              │
                      ┌───────────────────────┴───────────────────────┐
                      ▼                                               ▼
          [ Core 1: Hardware Sync ]                        [ Core 3: Next.js HMI ]
                      │                                               │
                      ▼                                               ▼
          💾 [ SHARED MEMORY MAP ARRAY ] <───────────────────────> [ WebSockets ]
                      ▲
                      │ (Sub-10ms Scan Loop)
                      ▼
          [ Core 2: Numba Logic Engine ] ───> 📊 [ NumPy Predictive Data ] ───> PocketBase

Prototype + JSON ladder runtime are real and running (see status above).
Modbus (TCP + RTU), both HMIs, timers, and analytics are real too.
What remains concept: PocketBase logging and the PREEMPT_RT tuning guide.
