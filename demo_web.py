#!/usr/bin/env python3
"""PYLC live demo + interactive HMI.

Serves a web page (stdlib only) over the running scan engine and the
conveyor program (programs/conveyor.json). Two modes:

- DEMO   — a simulator loops a 20-second story: start, seal-in, parts,
           gate fault, restart, stop.
- MANUAL — the operator drives: momentary START / STOP pushbuttons and
           a maintained GATE switch write the program's input tags
           directly. The photo-eye stays automatic (it is a sensor,
           not an operator control): it pulses while the motor runs.
- A Modbus/TCP slave (pylc.modbus, pure stdlib) exposes the same tag
  table as coils/registers on --modbus-port (default 1502), so any
  SCADA/HMI master can poll or drive the program.

Usage:  python3 demo_web.py [--port 8085] [--period-ms 10]

NOTE: the write API is unauthenticated because this drives a
simulation on a home LAN. Gate it before it ever touches real I/O.
"""

import argparse
import ctypes
import json
import multiprocessing
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from pylc.program import load_program        # noqa: E402
from pylc.engine import run_engine_process   # noqa: E402
from pylc.modbus import build_map, run_modbus_process  # noqa: E402

CYCLE_S = 20.0
MODE_DEMO = 1
MODE_MANUAL = 0
WRITABLE_TAGS = ("StartPB", "StopPB", "GateClosed", "PartsCounter")


# ==========================================
# Simulator (script in DEMO, photo-eye in MANUAL) + watchdog
# ==========================================
def demo_simulator(table, shared, running_flag, mode):
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
        wd = r("Watchdog")
        if last_watchdog != -1:
            if wd == last_watchdog:
                stalled_for += tick
                if stalled_for >= 0.1:
                    print("[EMERGENCY HALT] Watchdog stalled >100ms — engine locked!", flush=True)
                    running_flag.value = False
                    break
            else:
                stalled_for = 0.0
        last_watchdog = wd

        if mode.value == MODE_DEMO:
            t = (time.perf_counter() - t0) % CYCLE_S
            w("StartPB", 1 if (1.0 <= t < 1.4 or 11.0 <= t < 11.4) else 0)
            w("StopPB", 1 if 15.0 <= t < 15.4 else 0)
            w("GateClosed", 0 if 8.0 <= t < 10.0 else 1)
        # In BOTH modes the photo-eye is automatic: it pulses while the
        # motor runs (0.25s on / 0.45s off), as a real sensor would.
        motor = r("MotorRun")
        t = time.perf_counter() - t0
        w("PartSensor", 1 if (motor and (t % 0.7) < 0.25) else 0)
        time.sleep(tick)


# ==========================================
# Web HMI
# ==========================================
PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>PYLC — Live Conveyor HMI</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body{margin:0;background:#0d1512;color:#d7e0da;font-family:ui-monospace,Menlo,Consolas,monospace}
  .wrap{max-width:860px;margin:0 auto;padding:28px 20px 40px}
  h1{font-size:20px;letter-spacing:.14em;margin:0 0 4px}
  .sub{color:#7d9184;font-size:13px;margin-bottom:20px;line-height:1.6}
  .live{color:#39d98a}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:20px}
  .cell{background:#14201a;border:1px solid #24382d;border-radius:10px;padding:14px}
  .cell .nm{font-size:12px;letter-spacing:.1em;color:#8fa398}
  .led{display:inline-block;width:14px;height:14px;border-radius:50%;background:#233028;margin-right:8px;vertical-align:-2px}
  .on{background:#39d98a;box-shadow:0 0 10px #39d98a}
  .on.red{background:#ff5f56;box-shadow:0 0 10px #ff5f56}
  .on.amber{background:#ffb340;box-shadow:0 0 10px #ffb340}
  .cell .st{font-size:16px;margin-top:8px}
  .big{font-size:44px;text-align:right}
  .rungs{background:#101a15;border:1px solid #24382d;border-radius:10px;padding:16px;font-size:13.5px;line-height:1.9;overflow-x:auto;white-space:nowrap}
  .rungs .cm{color:#63796a}
  .foot{color:#63796a;font-size:12px;margin-top:14px;line-height:1.7}
  .panel{background:#14201a;border:1px solid #24382d;border-radius:10px;padding:16px;margin-bottom:20px}
  .panel .nm{font-size:12px;letter-spacing:.1em;color:#8fa398;margin-bottom:12px}
  .modes{display:flex;gap:10px;margin-bottom:16px}
  .modes button{flex:1}
  button{background:#182420;color:#cfd8d2;border:1px solid #2b3d33;border-radius:10px;padding:14px 10px;
    font:inherit;font-size:15px;cursor:pointer;user-select:none;-webkit-user-select:none;-webkit-touch-callout:none;touch-action:none}
  button.sel{background:#1f6f43;border-color:#2e8f5b;color:#fff}
  button:disabled{opacity:.35;cursor:default}
  button:active:not(:disabled){transform:translateY(1px)}
  .ctrls{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
  #btnStart{background:#14532d;border-color:#22c55e;color:#eafff2}
  #btnStop{background:#5f1d1d;border-color:#ef4444;color:#ffecec}
  #btnGate.gateopen{background:#5f451d;border-color:#ffb340;color:#fff6e5}
  .hint{color:#63796a;font-size:12px;margin-top:12px;line-height:1.6}
</style></head><body><div class="wrap">
<h1>PYLC — CONVEYOR HMI <span class="live">● LIVE</span></h1>
<div class="sub">The scan engine is executing <b>programs/conveyor.json</b> right now, on a 10&nbsp;ms scan.
In DEMO the simulator drives. Switch to MANUAL and the panel is yours.</div>

<div class="panel">
  <div class="nm">OPERATOR PANEL</div>
  <div class="modes">
    <button id="modeDemo" class="sel">DEMO — SIMULATOR DRIVES</button>
    <button id="modeManual">MANUAL — YOU DRIVE</button>
  </div>
  <div class="ctrls">
    <button id="btnStart" disabled>START<br><span style="font-size:11px">(tap — it latches)</span></button>
    <button id="btnStop" disabled>STOP</button>
    <button id="btnGate" disabled>GATE: <span id="gateState">—</span><br><span style="font-size:11px">(maintained switch)</span></button>
    <button id="btnReset" disabled>RESET PARTS</button>
  </div>
  <div class="hint" id="modeHint">The 20-second story: start → seal-in → parts → gate opens (~8s) → motor stays off after the gate closes → restart (~11s) → stop (~15s).</div>
</div>

<div class="grid">
  <div class="cell"><span class="nm">GATE</span><div class="st"><span class="led" id="ledGate"></span><span id="vGate">—</span></div></div>
  <div class="cell"><span class="nm">START PB</span><div class="st"><span class="led" id="ledStart"></span><span id="vStart">—</span></div></div>
  <div class="cell"><span class="nm">STOP PB</span><div class="st"><span class="led" id="ledStop"></span><span id="vStop">—</span></div></div>
  <div class="cell"><span class="nm">PHOTO-EYE</span><div class="st"><span class="led" id="ledEye"></span><span id="vEye">—</span></div></div>
  <div class="cell"><span class="nm">MOTOR</span><div class="st"><span class="led" id="ledMotor"></span><span id="vMotor">—</span></div></div>
</div>
<div class="grid">
  <div class="cell"><span class="nm">PARTS COUNTED</span><div class="big" id="vParts">0</div></div>
  <div class="cell"><span class="nm">SCANS (WATCHDOG)</span><div class="big" id="vWd">0</div></div>
  <div class="cell"><span class="nm">SCAN EVAL — LAST / MAX</span><div class="big" style="font-size:30px;padding-top:10px"><span id="vLast">0</span>µs / <span id="vMax">0</span>µs</div></div>
</div>
<div class="rungs">
<div><span class="cm">RUNG 1 — </span>(StartPB <b>OR</b> MotorRun) <b>AND</b> GateClosed <b>AND NOT</b> StopPB → <b>MotorRun</b></div>
<div><span class="cm">RUNG 2 — </span>PartSensor rising edge → <b>PartsCounter + 1</b> (CTU)</div>
</div>
<div class="panel" style="margin-top:20px">
  <div class="nm">MODBUS/TCP GATEWAY — PORT __MODBUS_PORT__ &nbsp;·&nbsp; OFFSETS ARE 0-BASED</div>
  <div class="rungs" style="border:none;background:none;padding:0">__MODBUS_ROWS__</div>
  <div class="hint">Any Modbus master — SCADA, another PLC, a phone app — can poll these addresses. In DEMO mode the simulator drives the input tags, so master writes to them race the field exactly like a real input image; switch to MANUAL to drive over Modbus.</div>
</div>
<div class="foot">Try the interlock in MANUAL: start the motor, then open the gate — the motor drops and will not restart until you close the gate AND press Start again. The seal is a logic behavior, not a script.</div>
</div>
<script>
var manual = false;
function post(path, obj){
  return fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(obj)});
}
function setMode(m){
  post('/api/mode', {mode: m}).then(()=>refresh());
}
document.getElementById('modeDemo').onclick = ()=>setMode('DEMO');
document.getElementById('modeManual').onclick = ()=>setMode('MANUAL');

function momentary(id, tag){
  var el = document.getElementById(id);
  el.addEventListener('pointerdown', e=>{ e.preventDefault(); post('/api/set', {tag: tag, value: 1}); });
  ['pointerup','pointerleave','pointercancel'].forEach(ev=>
    el.addEventListener(ev, ()=>post('/api/set', {tag: tag, value: 0})));
}
momentary('btnStart','StartPB');
momentary('btnStop','StopPB');
document.getElementById('btnGate').onclick = ()=>{
  post('/api/toggle', {tag: 'GateClosed'});
};
document.getElementById('btnReset').onclick = ()=>post('/api/set', {tag:'PartsCounter', value: 0});

function led(id, on, cls){ var e=document.getElementById(id); e.className='led'+(on?(' on '+(cls||'')):''); }
function refresh(){
  fetch('/api/tags').then(r=>r.json()).then(d=>{
    var s = d.tags; manual = (d.mode === 'MANUAL');
    led('ledGate', s.GateClosed); document.getElementById('vGate').textContent = s.GateClosed?'CLOSED':'OPEN';
    led('ledStart', s.StartPB); document.getElementById('vStart').textContent = s.StartPB?'PRESSED':'—';
    led('ledStop', s.StopPB, 'red'); document.getElementById('vStop').textContent = s.StopPB?'PRESSED':'—';
    led('ledEye', s.PartSensor, 'amber'); document.getElementById('vEye').textContent = s.PartSensor?'ON':'off';
    led('ledMotor', s.MotorRun); document.getElementById('vMotor').textContent = s.MotorRun?'RUNNING':'STOPPED';
    document.getElementById('vParts').textContent = s.PartsCounter;
    document.getElementById('vWd').textContent = s.Watchdog.toLocaleString();
    document.getElementById('vLast').textContent = s.LastScanUs;
    document.getElementById('vMax').textContent = s.MaxScanUs;
    document.getElementById('gateState').textContent = s.GateClosed?'CLOSED':'OPEN';
    document.getElementById('btnGate').className = s.GateClosed?'':'gateopen';
    document.getElementById('modeDemo').className = manual?'':'sel';
    document.getElementById('modeManual').className = manual?'sel':'';
    ['btnStart','btnStop','btnGate','btnReset'].forEach(id=>{ document.getElementById(id).disabled = !manual; });
    document.getElementById('modeHint').textContent = manual
      ? 'MANUAL: tap START to latch the motor on. Open the GATE to prove the interlock. STOP breaks the seal.'
      : 'The 20-second story: start → seal-in → parts → gate opens (~8s) → motor stays off after the gate closes → restart (~11s) → stop (~15s).';
  }).catch(()=>{});
}
setInterval(refresh, 200); refresh();
</script></body></html>
"""


def _modbus_rows(mbmap, port):
    """Render the gateway's address map for the HMI page."""
    if port <= 0 or mbmap is None:
        return "<div>Gateway disabled (--modbus-port 0).</div>"
    rows = []
    for e in mbmap.coils:
        access = "read/write" if e.writable else "read-only · also on discrete inputs"
        rows.append(
            f"<div>COIL {e.address} <span class='cm'>({e.address + 1:05d})</span>"
            f" — <b>{e.tag}</b> <span class='cm'>{access}</span></div>"
        )
    for e in mbmap.registers:
        access = "read/write" if e.writable else "read-only · also on input registers"
        rows.append(
            f"<div>REGISTER {e.address} <span class='cm'>({40001 + e.address})</span>"
            f" — <b>{e.tag}</b> <span class='cm'>{access}</span></div>"
        )
    return "\n".join(rows)


def make_handler(table, shared, mode, page_html):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            # The Next.js HMI is served from another port (another
            # origin); let it call this API. LAN demo — gate before
            # this ever fronts real I/O, same as the write API itself.
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def _read_body(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return {}

        def do_GET(self):
            if self.path == "/api/tags":
                with shared.get_lock():
                    snap = table.snapshot()
                self._json({"tags": snap, "mode": "DEMO" if mode.value == MODE_DEMO else "MANUAL"})
            elif self.path == "/":
                body = page_html.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            data = self._read_body()
            if self.path == "/api/mode":
                m = data.get("mode")
                if m == "MANUAL":
                    # Clean handover: safe defaults before the operator takes over.
                    with shared.get_lock():
                        table.set("GateClosed", 1)
                        table.set("StartPB", 0)
                        table.set("StopPB", 0)
                    mode.value = MODE_MANUAL
                elif m == "DEMO":
                    mode.value = MODE_DEMO
                else:
                    return self._json({"error": "mode must be DEMO or MANUAL"}, 400)
                return self._json({"mode": m})
            if mode.value != MODE_MANUAL:
                return self._json({"error": "writes are only accepted in MANUAL mode"}, 403)
            if self.path in ("/api/set", "/api/toggle"):
                tag = data.get("tag")
                if tag not in WRITABLE_TAGS:
                    return self._json({"error": f"tag {tag!r} is not writable"}, 400)
                with shared.get_lock():
                    if self.path == "/api/set":
                        table.set(tag, int(data.get("value", 0)))
                    else:
                        table.set(tag, 0 if table.get(tag) else 1)
                    value = table.get(tag)
                return self._json({tag: value})
            self.send_response(404)
            self.end_headers()

    return Handler


def main():
    ap = argparse.ArgumentParser(description="PYLC live conveyor demo + HMI (web).")
    ap.add_argument("--port", type=int, default=8085)
    ap.add_argument("--period-ms", type=float, default=10.0)
    ap.add_argument("--modbus-port", type=int, default=1502,
                    help="Modbus/TCP slave port (0 disables the gateway)")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    program = load_program(os.path.join(here, "programs", "conveyor.json"))
    initials = [program.table.definition(n).initial for n in program.table.names()]
    shared = multiprocessing.Array("i", initials)
    program.table.storage = shared
    table = program.table
    running_flag = multiprocessing.Value(ctypes.c_bool, True)
    mode = multiprocessing.Value("i", MODE_DEMO)

    procs = [
        multiprocessing.Process(
            target=run_engine_process,
            args=(program, shared, running_flag, args.period_ms / 1000.0),
            name="LogicEngine",
        ),
        multiprocessing.Process(
            target=demo_simulator, args=(table, shared, running_flag, mode), name="Simulator"
        ),
    ]
    mbmap = build_map(table, writable=set(WRITABLE_TAGS))
    if args.modbus_port > 0:
        procs.append(
            multiprocessing.Process(
                target=run_modbus_process,
                args=(table, shared, running_flag, mbmap, args.modbus_port),
                name="ModbusGateway",
            )
        )
    for p in procs:
        p.start()

    page_html = PAGE.replace("__MODBUS_PORT__", str(args.modbus_port)).replace(
        "__MODBUS_ROWS__", _modbus_rows(mbmap, args.modbus_port)
    )
    server = ThreadingHTTPServer(
        ("0.0.0.0", args.port), make_handler(table, shared, mode, page_html)
    )
    print(f"PYLC HMI live at http://0.0.0.0:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        running_flag.value = False
        for p in procs:
            p.join(timeout=3)


if __name__ == "__main__":
    main()
