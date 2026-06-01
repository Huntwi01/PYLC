import multiprocessing
import time
import random
import ctypes
import sys
import threading
import logging
import numpy as np
from numba import njit

# Import Flask for the Web HMI
try:
    from flask import Flask, jsonify, render_template_string
    from werkzeug.serving import make_server
except ImportError:
    print("\n[ERROR] Flask is not installed.")
    print("Please run: pip install Flask werkzeug")
    sys.exit(1)


# ==========================================
# 1. Shared Memory Map (The "Whiteboard")
# ==========================================
IDX_GATE_SENSOR = 0   
IDX_START_BUTTON = 1  
IDX_MOTOR_STATUS = 2  
IDX_PARTS_COUNTER = 3 
IDX_WATCHDOG = 4      

# ==========================================
# 2. Process 1: High-Speed Logic Engine
# ==========================================
@njit
def core_logic(shared_mem):
    if shared_mem[IDX_GATE_SENSOR] == 0:
        shared_mem[IDX_MOTOR_STATUS] = 0
        return
    
    if shared_mem[IDX_START_BUTTON] == 1:
        shared_mem[IDX_MOTOR_STATUS] = 1
    else:
        shared_mem[IDX_MOTOR_STATUS] = 0

def logic_engine(shared_mem, running_flag):
    mem_array = np.frombuffer(shared_mem.get_obj(), dtype=np.int32)
    core_logic(mem_array)
    
    while running_flag.value:
        start_time = time.perf_counter()
        
        core_logic(mem_array)
        mem_array[IDX_WATCHDOG] += 1
        
        if mem_array[IDX_MOTOR_STATUS] == 1:
            mem_array[IDX_PARTS_COUNTER] += 1
            
        elapsed = time.perf_counter() - start_time
        sleep_time = max(0.0, 0.01 - elapsed)
        time.sleep(sleep_time)

# ==========================================
# 3. Process 2: Factory Hardware Simulator
# ==========================================
def hardware_simulator(shared_mem, running_flag):
    # Give the logic engine 1.5 seconds to finish Numba JIT compilation
    time.sleep(1.5)
    
    last_watchdog = -1
    watchdog_stalled_time = 0.0
    
    check_interval = 0.05
    print_interval = 0.5
    time_since_print = print_interval
    
    while running_flag.value:
        with shared_mem.get_lock():
            watchdog = shared_mem[IDX_WATCHDOG]
            
        if last_watchdog != -1:
            if watchdog == last_watchdog:
                watchdog_stalled_time += check_interval
                if watchdog_stalled_time >= 0.1:
                    print("\n[EMERGENCY HALT] Watchdog failed to increment for >100ms. Logic engine locked!")
                    running_flag.value = False
                    break
            else:
                watchdog_stalled_time = 0.0
                
        last_watchdog = watchdog
        
        if time_since_print >= print_interval:
            new_gate_val = random.choices([0, 1], weights=[0.2, 0.8])[0]
            new_btn_val = random.choices([0, 1], weights=[0.5, 0.5])[0]
            
            with shared_mem.get_lock():
                shared_mem[IDX_GATE_SENSOR] = new_gate_val
                shared_mem[IDX_START_BUTTON] = new_btn_val
                motor = shared_mem[IDX_MOTOR_STATUS]
            
            gate_str = "CLOSED" if new_gate_val else "OPEN"
            btn_str = "PRESSED" if new_btn_val else "RELEASED"
            motor_str = "RUNNING" if motor else "STOPPED"
            
            print(f"[GATE: {gate_str:6}] [BUTTON: {btn_str:8}] -> [MOTOR: {motor_str:7}] | Watchdog: {watchdog}")
            time_since_print = 0.0
        
        time.sleep(check_interval)
        time_since_print += check_interval

# ==========================================
# 4. Process 3: Data Logger & SCADA Layer
# ==========================================
def scada_logger(shared_mem, running_flag):
    motor_history = []
    
    while running_flag.value:
        with shared_mem.get_lock():
            motor = shared_mem[IDX_MOTOR_STATUS]
            parts = shared_mem[IDX_PARTS_COUNTER]
            
        motor_history.append(motor)
        if len(motor_history) > 10:
            motor_history.pop(0)
            
        history_arr = np.array(motor_history)
        uptime_ratio = np.mean(history_arr) * 100 if len(history_arr) > 0 else 0.0
        
        # print(f"--- SCADA LOG: Total Parts: {parts}, Recent Uptime: {uptime_ratio:.1f}% ---")
        time.sleep(1.0)


# ==========================================
# 5. Process 4: Web Dashboard (HMI)
# ==========================================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>HMI - Main Control Panel</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body { background-color: #a3a3a3; font-family: 'Arial', sans-serif; }
        .hmi-panel {
            background-color: #d4d4d8;
            border-top: 2px solid #f4f4f5;
            border-left: 2px solid #f4f4f5;
            border-bottom: 3px solid #71717a;
            border-right: 3px solid #71717a;
        }
        .hmi-inset {
            background-color: #e4e4e7;
            border-top: 2px solid #71717a;
            border-left: 2px solid #71717a;
            border-bottom: 2px solid #f4f4f5;
            border-right: 2px solid #f4f4f5;
        }
        .led-off { background: radial-gradient(circle, #064e3b 10%, #022c22 80%); box-shadow: inset 0 2px 4px rgba(0,0,0,0.5); }
        .led-on-green { background: radial-gradient(circle, #4ade80 20%, #166534 90%); box-shadow: 0 0 10px #4ade80, inset 0 1px 2px rgba(255,255,255,0.8); }
        .led-on-red { background: radial-gradient(circle, #f87171 20%, #991b1b 90%); box-shadow: 0 0 10px #f87171, inset 0 1px 2px rgba(255,255,255,0.8); }
        .led-off-red { background: radial-gradient(circle, #450a0a 10%, #200000 80%); box-shadow: inset 0 2px 4px rgba(0,0,0,0.5); }
        
        .seven-segment { font-family: 'Courier New', Courier, monospace; background-color: #000; color: #00ff00; }
    </style>
</head>
<body class="min-h-screen p-4 flex flex-col items-center justify-center">

    <div class="hmi-panel max-w-5xl w-full p-4 flex flex-col">
        <!-- HEADER -->
        <div class="flex justify-between items-center bg-blue-900 text-white px-4 py-2 mb-4 border-2 border-slate-900 shadow-inner">
            <h1 class="text-xl font-bold tracking-widest uppercase">CONVEYOR CONTROL SYSTEM</h1>
            <div class="flex items-center gap-4 text-sm font-mono">
                <span id="clock">00:00:00</span>
                <span>|</span>
                <span class="text-green-400 font-bold">ONLINE</span>
            </div>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-4">
            <!-- I/O DIAGNOSTICS TABLE -->
            <div class="hmi-panel p-2 col-span-1">
                <div class="bg-gray-800 text-white font-bold px-2 py-1 mb-2 text-sm">I/O MAP DIAGNOSTICS</div>
                <div class="hmi-inset p-2 bg-white font-mono text-xs overflow-hidden">
                    <table class="w-full text-left border-collapse">
                        <thead>
                            <tr class="bg-gray-200 border-b border-gray-400">
                                <th class="p-1">ADDR</th>
                                <th class="p-1">TAG</th>
                                <th class="p-1">STATE</th>
                            </tr>
                        </thead>
                        <tbody id="io-table">
                            <tr class="border-b border-gray-200">
                                <td class="p-1 text-gray-500">I0.0</td>
                                <td class="p-1">GATE_SENS</td>
                                <td class="p-1 font-bold text-red-600" id="raw-gate">0</td>
                            </tr>
                            <tr class="border-b border-gray-200">
                                <td class="p-1 text-gray-500">I0.1</td>
                                <td class="p-1">START_PB</td>
                                <td class="p-1 font-bold text-blue-600" id="raw-btn">0</td>
                            </tr>
                            <tr class="border-b border-gray-200 bg-gray-50">
                                <td class="p-1 text-gray-500">Q0.0</td>
                                <td class="p-1">MOTOR_CMD</td>
                                <td class="p-1 font-bold text-green-600" id="raw-motor">0</td>
                            </tr>
                            <tr class="border-b border-gray-200 bg-gray-50">
                                <td class="p-1 text-gray-500">DB1.DBD0</td>
                                <td class="p-1">PARTS_CNT</td>
                                <td class="p-1 font-bold" id="raw-parts">0</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- PROCESS GRAPHIC -->
            <div class="hmi-panel p-2 col-span-2 relative">
                <div class="bg-gray-800 text-white font-bold px-2 py-1 mb-2 text-sm">PROCESS OVERVIEW</div>
                <div class="hmi-inset p-4 h-[250px] relative bg-slate-200 flex items-center justify-center flex-col">
                    
                    <!-- Motor Graphic -->
                    <div class="flex items-end mb-4 relative w-full justify-center">
                        <!-- Conveyor Belt -->
                        <div class="absolute bottom-0 w-3/4 h-8 bg-gray-600 border-4 border-gray-800 rounded flex items-center justify-evenly overflow-hidden" id="conveyor-belt">
                            <div class="w-4 h-full bg-gray-800"></div>
                            <div class="w-4 h-full bg-gray-800"></div>
                            <div class="w-4 h-full bg-gray-800"></div>
                            <div class="w-4 h-full bg-gray-800"></div>
                            <div class="w-4 h-full bg-gray-800"></div>
                        </div>
                        
                        <!-- Motor Assembly -->
                        <div class="z-10 flex flex-col items-center mr-48">
                            <div class="w-20 h-24 bg-blue-700 border-2 border-gray-900 rounded-t-lg shadow-lg relative flex items-center justify-center">
                                <div id="motor-fan" class="w-12 h-12 border-4 border-gray-900 rounded-full border-dashed transition-transform duration-100"></div>
                            </div>
                            <div class="bg-gray-800 text-white text-xs font-bold px-2 py-1 w-full text-center">M100</div>
                        </div>
                        
                        <!-- Safety Gate Graphic -->
                        <div class="z-10 absolute right-16 bottom-0 w-2 h-32 bg-yellow-400 border border-gray-800 transition-all duration-300 origin-bottom" id="gate-graphic" style="transform: rotate(0deg);"></div>
                    </div>
                    
                </div>
                
                <!-- Status Overlays -->
                <div class="absolute top-10 right-4 flex gap-4">
                    <div class="flex flex-col items-center">
                        <div id="led-motor" class="w-6 h-6 rounded-full led-off border-2 border-gray-400 mb-1"></div>
                        <span class="text-[10px] font-bold">MOTOR_RUN</span>
                    </div>
                    <div class="flex flex-col items-center">
                        <div id="led-gate" class="w-6 h-6 rounded-full led-on-green border-2 border-gray-400 mb-1"></div>
                        <span class="text-[10px] font-bold">GATE_CLOSED</span>
                    </div>
                    <div class="flex flex-col items-center">
                        <div id="led-fault" class="w-6 h-6 rounded-full led-off-red border-2 border-gray-400 mb-1"></div>
                        <span class="text-[10px] font-bold">INTERLOCK</span>
                    </div>
                </div>
            </div>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
            <!-- PHYSICAL PUSHBUTTON SIMULATION -->
            <div class="hmi-panel p-2 flex flex-col justify-center items-center h-40">
                <div class="text-sm font-bold mb-4">OPERATOR PANEL</div>
                <div class="flex gap-8">
                    <div class="flex flex-col items-center">
                        <div id="btn-start" class="w-16 h-16 rounded-full bg-green-600 border-4 border-gray-400 shadow-[0_5px_0_#14532d] flex items-center justify-center mb-2 transition-transform">
                             <div class="w-12 h-12 rounded-full border-2 border-green-500 opacity-50"></div>
                        </div>
                        <span class="font-bold text-xs uppercase">Start CMD</span>
                    </div>
                    
                    <div class="flex flex-col items-center">
                        <div id="gate-switch" class="w-12 h-16 bg-black border-4 border-gray-300 rounded shadow-md relative mb-2">
                             <div id="gate-toggle" class="absolute w-full h-1/2 bg-yellow-500 top-0 transition-all border-b border-black"></div>
                        </div>
                        <span class="font-bold text-xs uppercase">Safety Gate</span>
                    </div>
                </div>
            </div>

            <!-- PRODUCTION TOTALIZER -->
            <div class="hmi-panel p-2 flex flex-col justify-center items-center h-40">
                <div class="text-sm font-bold mb-4">PRODUCTION TOTALIZER</div>
                <div class="seven-segment text-5xl p-4 border-4 border-gray-600 rounded shadow-inner tracking-widest w-64 text-right">
                    <span id="counter-val">000000</span>
                </div>
            </div>
        </div>
    </div>

    <script>
        // Clock
        setInterval(() => {
            document.getElementById('clock').innerText = new Date().toLocaleTimeString('en-US', { hour12: false });
        }, 1000);

        let rotation = 0;

        function updateHMI() {
            fetch('/api/status')
                .then(r => r.json())
                .then(data => {
                    // Update I/O Table
                    document.getElementById('raw-gate').innerText = data.gate_closed ? "1" : "0";
                    document.getElementById('raw-btn').innerText = data.button_pressed ? "1" : "0";
                    document.getElementById('raw-motor').innerText = data.motor_running ? "1" : "0";
                    document.getElementById('raw-parts').innerText = data.parts_count;

                    // Update Operator Panel Graphics
                    const btnStart = document.getElementById('btn-start');
                    if(data.button_pressed) {
                        btnStart.style.transform = 'translateY(5px)';
                        btnStart.style.boxShadow = '0 0px 0 #14532d';
                    } else {
                        btnStart.style.transform = 'translateY(0px)';
                        btnStart.style.boxShadow = '0 5px 0 #14532d';
                    }

                    const gateToggle = document.getElementById('gate-toggle');
                    const gateGraphic = document.getElementById('gate-graphic');
                    if(data.gate_closed) {
                        gateToggle.style.top = '50%';
                        gateGraphic.style.transform = 'rotate(0deg)';
                    } else {
                        gateToggle.style.top = '0%';
                        gateGraphic.style.transform = 'rotate(-60deg)';
                    }

                    // Update LED Status Indicators
                    const ledMotor = document.getElementById('led-motor');
                    const ledGate = document.getElementById('led-gate');
                    const ledFault = document.getElementById('led-fault');
                    
                    if(data.motor_running) {
                        ledMotor.className = 'w-6 h-6 rounded-full led-on-green border-2 border-gray-400 mb-1';
                        // Animate Fan
                        rotation += 45;
                        document.getElementById('motor-fan').style.transform = `rotate(${rotation}deg)`;
                    } else {
                        ledMotor.className = 'w-6 h-6 rounded-full led-off border-2 border-gray-400 mb-1';
                    }

                    if(data.gate_closed) {
                        ledGate.className = 'w-6 h-6 rounded-full led-on-green border-2 border-gray-400 mb-1';
                        ledFault.className = 'w-6 h-6 rounded-full led-off-red border-2 border-gray-400 mb-1';
                    } else {
                        ledGate.className = 'w-6 h-6 rounded-full led-off border-2 border-gray-400 mb-1';
                        ledFault.className = 'w-6 h-6 rounded-full led-on-red border-2 border-gray-400 mb-1 animate-pulse';
                    }

                    // Update Totalizer
                    document.getElementById('counter-val').innerText = String(data.parts_count).padStart(6, '0');
                })
                .catch(e => console.error(e));
        }

        setInterval(updateHMI, 150);
        updateHMI();
    </script>
</body>
</html>
"""

def hmi_web_server(shared_mem, running_flag):
    app = Flask(__name__)
    
    # Suppress Flask default logging so it doesn't interrupt our terminal factory map output
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    
    @app.route('/')
    def index():
        return render_template_string(HTML_TEMPLATE)
        
    @app.route('/api/status')
    def status():
        # [ LOCK-FREE READ ]
        gate = shared_mem[IDX_GATE_SENSOR]
        btn = shared_mem[IDX_START_BUTTON]
        motor = shared_mem[IDX_MOTOR_STATUS]
        parts = shared_mem[IDX_PARTS_COUNTER]
        
        return jsonify({
            "gate_closed": bool(gate),
            "button_pressed": bool(btn),
            "motor_running": bool(motor),
            "parts_count": parts
        })
        
    # make_server allows us to cleanly start/stop the Werkzeug WSGI server
    server = make_server('0.0.0.0', 5000, app)
    
    # Run the server in a daemon thread so this process loop isn't blocked
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    
    print("\n=======================================================")
    print(" HMI Web Server Running at: http://127.0.0.1:5000")
    print("=======================================================\n")
    
    # Loop and wait for the global shutdown signal
    while running_flag.value:
        time.sleep(0.5)
        
    # Clean, graceful shutdown of the web server
    server.shutdown()
    server_thread.join(timeout=2)


# ==========================================
# Main Execution / Setup
# ==========================================
if __name__ == '__main__':
    print("Initializing Soft-PLC Prototype with Web HMI...")
    
    shared_memory = multiprocessing.Array('i', 5)
    
    shared_memory[IDX_GATE_SENSOR] = 1 
    shared_memory[IDX_START_BUTTON] = 0
    shared_memory[IDX_MOTOR_STATUS] = 0
    shared_memory[IDX_PARTS_COUNTER] = 0
    shared_memory[IDX_WATCHDOG] = 0
    
    running_flag = multiprocessing.Value(ctypes.c_bool, True)
    
    # Create the 4 processes
    p1 = multiprocessing.Process(target=logic_engine, args=(shared_memory, running_flag), name="LogicEngine")
    p2 = multiprocessing.Process(target=hardware_simulator, args=(shared_memory, running_flag), name="HardwareSim")
    p3 = multiprocessing.Process(target=scada_logger, args=(shared_memory, running_flag), name="SCADALogger")
    p4 = multiprocessing.Process(target=hmi_web_server, args=(shared_memory, running_flag), name="WebHMI")
    
    # Start processes
    p1.start()
    p2.start()
    p3.start()
    p4.start()
    
    try:
        while running_flag.value:
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\n[Ctrl+C Detected] Initiating graceful shutdown of Soft-PLC & HMI...")
        running_flag.value = False
        
    # Wait for processes to finish
    p1.join()
    p2.join()
    p3.join()
    p4.join()
    
    print("System safely shut down.")
