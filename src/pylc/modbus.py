"""Modbus/TCP gateway: expose the tag table as a Modbus data table.

Implemented from the protocol spec in pure stdlib (socket + struct),
like the rest of the PYLC core — no third-party dependency to install
on the recycled hardware this project targets.

Mapping, built automatically from a program's tag definitions with
definition order preserved (see :func:`build_map`):

- BOOL tags -> **coils** (FC1 read, FC5/FC15 write), mirrored read-only
  as **discrete inputs** (FC2).
- INT tags -> **holding registers** (FC3 read, FC6/FC16 write),
  mirrored read-only as **input registers** (FC4). A register carries
  the low 16 bits of the tag's int32 value.

Addresses in the PDU are 0-based offsets — the convention most client
libraries use. Add 1 (coils/registers numbered from 00001 / 40001) for
the classic notation.

Write policy: ``build_map`` takes the set of tag names a master may
write (``None`` = all). Writing a mapped-but-read-only address, or any
address outside the map, is refused with exception 02 (illegal data
address); bad quantities get 03 (illegal data value); unknown function
codes get 01 (illegal function). Multi-writes are all-or-nothing.

One real-PLC behavior worth knowing: tags the field side drives (the
simulator here, physical input scans on real hardware) are overwritten
by that side every cycle. A master writing an input tag in DEMO mode
is racing the simulator exactly the way it would race a real input
image — switch the HMI to MANUAL to drive inputs over Modbus.

The PDU handler (:func:`handle_pdu`) is transport-independent and
unit-tested against a fake bridge; the TCP server is a thin shell.
"""

import socket
import struct
import threading
from dataclasses import dataclass, field

EX_ILLEGAL_FUNCTION = 0x01
EX_ILLEGAL_ADDRESS = 0x02
EX_ILLEGAL_VALUE = 0x03

MAX_READ_BITS = 2000
MAX_READ_REGS = 125
MAX_WRITE_BITS = 1968
MAX_WRITE_REGS = 123


class ModbusError(Exception):
    """A protocol refusal; ``code`` is the Modbus exception code."""

    def __init__(self, code):
        super().__init__(f"modbus exception {code:#04x}")
        self.code = code


# ==========================================
# Address map
# ==========================================
@dataclass(frozen=True)
class MapEntry:
    address: int
    tag: str
    table: str  # "coil" or "register"
    writable: bool


@dataclass
class ModbusMap:
    coils: list = field(default_factory=list)
    registers: list = field(default_factory=list)

    def coil(self, address):
        return self.coils[address] if 0 <= address < len(self.coils) else None

    def register(self, address):
        return self.registers[address] if 0 <= address < len(self.registers) else None


def build_map(table, writable=None):
    """Build a ModbusMap from a TagTable's definitions, in order.

    ``writable`` is the set of tag names a master may write;
    ``None`` means every mapped tag is writable.
    """
    coils, registers = [], []
    for name in table.names():
        d = table.definition(name)
        w = True if writable is None else name in writable
        if d.type == "BOOL":
            coils.append(MapEntry(len(coils), name, "coil", w))
        elif d.type in ("INT", "TIMER"):  # a TIMER register reads its ACC (ms)
            registers.append(MapEntry(len(registers), name, "register", w))
    return ModbusMap(coils, registers)


# ==========================================
# Bridge: Modbus addresses <-> tag values
# ==========================================
class TagBridge:
    """Translates address-based reads/writes into tag get/set calls.

    ``get_value(name)`` / ``set_value(name, value)`` are supplied by the
    host, which owns locking and shared-memory access.
    """

    def __init__(self, mbmap, get_value, set_value):
        self.map = mbmap
        self._get = get_value
        self._set = set_value

    def _entries(self, table_name, start, qty):
        lookup = self.map.coil if table_name == "coil" else self.map.register
        entries = [lookup(start + i) for i in range(qty)]
        if any(e is None for e in entries):
            raise ModbusError(EX_ILLEGAL_ADDRESS)
        return entries

    def read_bits(self, start, qty):
        return [1 if self._get(e.tag) else 0 for e in self._entries("coil", start, qty)]

    def read_registers(self, start, qty):
        return [self._get(e.tag) & 0xFFFF for e in self._entries("register", start, qty)]

    def write_bits(self, start, values):
        entries = self._entries("coil", start, len(values))
        if any(not e.writable for e in entries):
            raise ModbusError(EX_ILLEGAL_ADDRESS)
        for e, v in zip(entries, values):
            self._set(e.tag, 1 if v else 0)

    def write_registers(self, start, values):
        entries = self._entries("register", start, len(values))
        if any(not e.writable for e in entries):
            raise ModbusError(EX_ILLEGAL_ADDRESS)
        for e, v in zip(entries, values):
            self._set(e.tag, v & 0xFFFF)


# ==========================================
# PDU handling (transport-independent)
# ==========================================
def handle_pdu(pdu, bridge):
    """Handle one Modbus PDU (function code + data) and return the
    response PDU. Never raises for protocol-level problems — refusals
    come back as exception responses, as the spec requires."""
    if not pdu:
        return b""
    fc = pdu[0]
    try:
        if fc in (1, 2):  # read coils / discrete inputs (same map)
            addr, qty = struct.unpack(">HH", pdu[1:5])
            if not 1 <= qty <= MAX_READ_BITS:
                raise ModbusError(EX_ILLEGAL_VALUE)
            bits = bridge.read_bits(addr, qty)
            data = bytearray((qty + 7) // 8)
            for i, b in enumerate(bits):
                if b:
                    data[i // 8] |= 1 << (i % 8)
            return bytes([fc, len(data)]) + bytes(data)

        if fc in (3, 4):  # read holding / input registers (same map)
            addr, qty = struct.unpack(">HH", pdu[1:5])
            if not 1 <= qty <= MAX_READ_REGS:
                raise ModbusError(EX_ILLEGAL_VALUE)
            regs = bridge.read_registers(addr, qty)
            return bytes([fc, qty * 2]) + b"".join(struct.pack(">H", v) for v in regs)

        if fc == 5:  # write single coil
            addr, val = struct.unpack(">HH", pdu[1:5])
            if val not in (0x0000, 0xFF00):
                raise ModbusError(EX_ILLEGAL_VALUE)
            bridge.write_bits(addr, [val == 0xFF00])
            return pdu[:5]

        if fc == 6:  # write single register
            addr, val = struct.unpack(">HH", pdu[1:5])
            bridge.write_registers(addr, [val])
            return pdu[:5]

        if fc == 15:  # write multiple coils
            addr, qty = struct.unpack(">HH", pdu[1:5])
            if not 1 <= qty <= MAX_WRITE_BITS:
                raise ModbusError(EX_ILLEGAL_VALUE)
            nbytes = pdu[5]
            data = pdu[6:6 + nbytes]
            if len(data) < (qty + 7) // 8:
                raise ModbusError(EX_ILLEGAL_VALUE)
            values = [bool(data[i // 8] >> (i % 8) & 1) for i in range(qty)]
            bridge.write_bits(addr, values)
            return struct.pack(">BHH", fc, addr, qty)

        if fc == 16:  # write multiple registers
            addr, qty = struct.unpack(">HH", pdu[1:5])
            if not 1 <= qty <= MAX_WRITE_REGS:
                raise ModbusError(EX_ILLEGAL_VALUE)
            nbytes = pdu[5]
            data = pdu[6:6 + nbytes]
            if len(data) < qty * 2:
                raise ModbusError(EX_ILLEGAL_VALUE)
            values = [struct.unpack(">H", data[i * 2:i * 2 + 2])[0] for i in range(qty)]
            bridge.write_registers(addr, values)
            return struct.pack(">BHH", fc, addr, qty)

        raise ModbusError(EX_ILLEGAL_FUNCTION)
    except ModbusError as e:
        return bytes([fc | 0x80, e.code])
    except (struct.error, IndexError):
        return bytes([fc | 0x80, EX_ILLEGAL_VALUE])


# ==========================================
# TCP server (runs in its own process for the live runtime)
# ==========================================
def _recv_exact(conn, n):
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return buf
        buf += chunk
    return buf


def _client_loop(conn, bridge):
    try:
        while True:
            header = _recv_exact(conn, 7)  # MBAP: tid, protocol, length, unit
            if len(header) < 7:
                return
            tid, _pid, length, uid = struct.unpack(">HHHB", header)
            if length < 2:
                return
            pdu = _recv_exact(conn, length - 1)
            if len(pdu) < length - 1:
                return
            response = handle_pdu(pdu, bridge)
            if response:
                frame = struct.pack(">HHHB", tid, 0, len(response) + 1, uid) + response
                conn.sendall(frame)
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def serve_modbus(bridge, running_flag, port=1502):
    """Accept Modbus/TCP connections until ``running_flag`` clears."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port))
    srv.listen(8)
    srv.settimeout(0.5)
    print(f"PYLC Modbus/TCP slave listening on 0.0.0.0:{port}", flush=True)
    try:
        while running_flag.value:
            try:
                conn, _addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=_client_loop, args=(conn, bridge), daemon=True).start()
    finally:
        srv.close()


def run_modbus_process(table, shared_storage, running_flag, mbmap, port=1502):
    """Multiprocessing entry point: bridge the shared tag table and serve.

    Follows the same pattern as the engine/simulator processes: the tag
    table's storage is the shared array, and every access is locked.
    """
    table.storage = shared_storage

    def get_value(name):
        with shared_storage.get_lock():
            return table.get(name)

    def set_value(name, value):
        with shared_storage.get_lock():
            table.set(name, int(value))

    serve_modbus(TagBridge(mbmap, get_value, set_value), running_flag, port)
