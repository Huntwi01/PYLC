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

## 📂 Repository File Structure

```text
pyplc/
├── .github/                  # CI/CD Workflows
├── config/
│   └── hardware_config.toml  # Baud rates, Modbus addressing, port mapping
├── src/
│   ├── core/
│   │   ├── __init__.py
│   │   ├── memory_map.py     # multiprocessing.Array shared whiteboard
│   │   └── engine.py         # The main deterministic 10ms Scan Cycle
│   ├── drivers/
│   │   └── modbus_driver.py  # Talk to PCIe/USB hardware using pymodbus
│   ├── parsers/
│   │   └── ladder_parser.py  # Translates visual JSON rungs to Python functions
│   └── analytics/
│       └── predictive.py     # NumPy anomalies and maintenance watcher thread
├── tests/
│   └── test_scan_cycle.py    # Jitter and latency benchmarks
├── hmi-dashboard/            # Next.js web application directory
├── requirements.txt          # numba, numpy, pymodbus, setserial
└── README.md

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

ALL CONCEPT CURRENTLY.
