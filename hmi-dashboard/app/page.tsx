'use client';

import { useCallback, useEffect, useState } from 'react';
import programJson from '../data/conveyor.json';

/* ------------------------------------------------------------------ */
/* API                                                                 */
/* ------------------------------------------------------------------ */

type Tags = Record<string, number>;
type Mode = 'DEMO' | 'MANUAL';

function apiBase(): string {
  if (process.env.NEXT_PUBLIC_PYLC_API) return process.env.NEXT_PUBLIC_PYLC_API;
  if (typeof window !== 'undefined') return `http://${window.location.hostname}:8085`;
  return 'http://localhost:8085';
}

async function post(path: string, obj: unknown): Promise<void> {
  try {
    await fetch(`${apiBase()}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(obj),
    });
  } catch {
    /* the poll loop will surface the dropped connection */
  }
}

/* ------------------------------------------------------------------ */
/* Rung rendering (programs/conveyor.json -> readable text)            */
/* ------------------------------------------------------------------ */

interface Contact {
  op: 'XIC' | 'XIO';
  tag: string;
}
interface RungOutput {
  op: string;
  tag: string;
  preset_ms?: number;
}
interface Rung {
  branches: Contact[][];
  comment: string;
  output: RungOutput;
}
interface Program {
  name: string;
  rungs: Rung[];
}

const program = programJson as unknown as Program;

function contactText(c: Contact): string {
  return c.op === 'XIO' ? `NOT ${c.tag}` : c.tag;
}

function rungLogic(rung: Rung): string {
  const branches = rung.branches;
  let input: string;
  if (
    rung.output.op === 'CTU' &&
    branches.length === 1 &&
    branches[0].length === 1 &&
    branches[0][0].op === 'XIC'
  ) {
    input = `${branches[0][0].tag} rising edge`;
  } else if (branches.length === 1) {
    input = branches[0].map(contactText).join(' AND ');
  } else {
    // Factor contacts shared by every branch out of the OR, the way a
    // tech would read it: (StartPB OR MotorRun) AND GateClosed AND NOT StopPB
    const key = (c: Contact) => `${c.op}:${c.tag}`;
    const common = branches[0].filter((c) =>
      branches.every((b) => b.some((x) => key(x) === key(c))),
    );
    const commonKeys = new Set(common.map(key));
    const variable = branches.map((b) => b.filter((c) => !commonKeys.has(key(c))));
    if (variable.some((v) => v.length === 0)) {
      input = branches
        .map((b) => `(${b.map(contactText).join(' AND ')})`)
        .join(' OR ');
    } else {
      const orPart = variable.map((v) => v.map(contactText).join(' AND ')).join(' OR ');
      input = [`(${orPart})`, ...common.map(contactText)].join(' AND ');
    }
  }
  const out = rung.output;
  if (out.op === 'CTU') return `${input} → ${out.tag} + 1 (CTU)`;
  if (out.op === 'TON' || out.op === 'TOF')
    return `${input} → ${out.tag} (${out.op}${out.preset_ms ? ` ${out.preset_ms} ms` : ''})`;
  return `${input} → ${out.tag}`;
}

/* ------------------------------------------------------------------ */
/* Static Modbus map for the conveyor program                          */
/* ------------------------------------------------------------------ */

const MODBUS_COILS: Array<[number, string, boolean]> = [
  [0, 'GateClosed', true],
  [1, 'StartPB', true],
  [2, 'StopPB', true],
  [3, 'PartSensor', false],
  [4, 'MotorRun', false],
];
const MODBUS_REGS: Array<[number, string, boolean]> = [
  [0, 'PartsCounter', true],
  [1, 'Watchdog', false],
  [2, 'LastScanUs', false],
  [3, 'MaxScanUs', false],
];

/* ------------------------------------------------------------------ */
/* Tag grid definitions                                                */
/* ------------------------------------------------------------------ */

const BOOL_TAGS: Array<{
  name: string;
  label: string;
  ledClass?: string;
  onText: string;
  offText: string;
}> = [
  { name: 'GateClosed', label: 'GATE', onText: 'CLOSED', offText: 'OPEN' },
  { name: 'StartPB', label: 'START PB', onText: 'PRESSED', offText: '—' },
  { name: 'StopPB', label: 'STOP PB', ledClass: 'red', onText: 'PRESSED', offText: '—' },
  { name: 'PartSensor', label: 'PHOTO-EYE', ledClass: 'amber', onText: 'ON', offText: 'off' },
  { name: 'MotorRun', label: 'MOTOR', onText: 'RUNNING', offText: 'STOPPED' },
];

/* ------------------------------------------------------------------ */
/* Page                                                                */
/* ------------------------------------------------------------------ */

export default function Page() {
  const [tags, setTags] = useState<Tags | null>(null);
  const [mode, setMode] = useState<Mode | null>(null);
  const [connected, setConnected] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const r = await fetch(`${apiBase()}/api/tags`);
      const d = await r.json();
      setTags(d.tags as Tags);
      setMode(d.mode as Mode);
      setConnected(true);
    } catch {
      setConnected(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const id = setInterval(() => void refresh(), 500);
    return () => clearInterval(id);
  }, [refresh]);

  const manual = mode === 'MANUAL';

  const setModeAndRefresh = (m: Mode) => {
    void post('/api/mode', { mode: m }).then(() => void refresh());
  };

  const momentary = (tag: string) => ({
    onPointerDown: (e: React.PointerEvent) => {
      e.preventDefault();
      void post('/api/set', { tag, value: 1 });
    },
    onPointerUp: () => void post('/api/set', { tag, value: 0 }),
    onPointerLeave: () => void post('/api/set', { tag, value: 0 }),
    onPointerCancel: () => void post('/api/set', { tag, value: 0 }),
  });

  const gateClosed = (tags?.GateClosed ?? 1) !== 0;

  return (
    <div className="wrap">
      <h1>
        PYLC — CONVEYOR HMI{' '}
        {connected ? (
          <span className="live">● LIVE</span>
        ) : (
          <span className="offline">● OFFLINE</span>
        )}
        {mode && (
          <span className={manual ? 'badge' : 'badge demo'}>MODE: {mode}</span>
        )}
      </h1>
      <div className="sub">
        The scan engine is executing <b>programs/conveyor.json</b> right now, on a
        10&nbsp;ms scan. This Next.js panel talks to the same API as the built-in
        HMI (<code>{apiBase()}</code>). In DEMO the simulator drives. Switch to
        MANUAL and the panel is yours.
      </div>

      <div className="panel">
        <div className="nm">OPERATOR PANEL</div>
        <div className="modes">
          <button
            className={!manual ? 'sel' : ''}
            onClick={() => setModeAndRefresh('DEMO')}
          >
            DEMO — SIMULATOR DRIVES
          </button>
          <button
            className={manual ? 'sel' : ''}
            onClick={() => setModeAndRefresh('MANUAL')}
          >
            MANUAL — YOU DRIVE
          </button>
        </div>
        <div className="ctrls">
          <button className="btn-start" disabled={!manual} {...momentary('StartPB')}>
            START
            <br />
            <span className="btn-note">(tap — it latches)</span>
          </button>
          <button className="btn-stop" disabled={!manual} {...momentary('StopPB')}>
            STOP
          </button>
          <button
            className={gateClosed ? 'btn-gate' : 'btn-gate gateopen'}
            disabled={!manual}
            onClick={() => void post('/api/toggle', { tag: 'GateClosed' })}
          >
            GATE: {tags ? (gateClosed ? 'CLOSED' : 'OPEN') : '—'}
            <br />
            <span className="btn-note">(maintained switch)</span>
          </button>
          <button
            disabled={!manual}
            onClick={() => void post('/api/set', { tag: 'PartsCounter', value: 0 })}
          >
            RESET PARTS
          </button>
        </div>
        <div className="hint">
          {manual
            ? 'MANUAL: tap START to latch the motor on. Open the GATE to prove the interlock. STOP breaks the seal.'
            : 'The 20-second story: start → seal-in → parts → gate opens (~8s) → motor stays off after the gate closes → restart (~11s) → stop (~15s).'}
        </div>
      </div>

      <div className="grid">
        {BOOL_TAGS.map((t) => {
          const on = (tags?.[t.name] ?? 0) !== 0;
          return (
            <div className="cell" key={t.name}>
              <span className="nm">{t.label}</span>
              <div className="st">
                <span className={on ? `led on ${t.ledClass ?? ''}` : 'led'} />
                {tags ? (on ? t.onText : t.offText) : '—'}
              </div>
            </div>
          );
        })}
      </div>
      <div className="grid">
        <div className="cell">
          <span className="nm">PARTS COUNTED</span>
          <div className="big">{tags?.PartsCounter ?? 0}</div>
        </div>
        <div className="cell">
          <span className="nm">SCANS (WATCHDOG)</span>
          <div className="big">{(tags?.Watchdog ?? 0).toLocaleString()}</div>
        </div>
        <div className="cell">
          <span className="nm">SCAN EVAL — LAST / MAX</span>
          <div className="big-sm">
            {tags?.LastScanUs ?? 0}µs / {tags?.MaxScanUs ?? 0}µs
          </div>
        </div>
      </div>

      <div className="rungs">
        <div className="cm" style={{ marginBottom: 8 }}>
          PROGRAM — {program.name.toUpperCase()} (programs/conveyor.json)
        </div>
        {program.rungs.map((rung, i) => (
          <div className="rung" key={i}>
            <div>
              <span className="cm">RUNG {i + 1} — </span>
              {rung.comment}
            </div>
            <div className="logic">{rungLogic(rung)}</div>
          </div>
        ))}
      </div>

      <div className="panel">
        <div className="nm">
          MODBUS/TCP GATEWAY — PORT 1502 &nbsp;·&nbsp; OFFSETS ARE 0-BASED
        </div>
        <table className="map">
          <thead>
            <tr>
              <th>ADDRESS</th>
              <th>TAG</th>
              <th>ACCESS</th>
            </tr>
          </thead>
          <tbody>
            {MODBUS_COILS.map(([addr, tag, rw]) => (
              <tr key={`c${addr}`}>
                <td>
                  COIL {addr} <span className="ro">({String(addr + 1).padStart(5, '0')})</span>
                </td>
                <td>{tag}</td>
                <td className={rw ? 'rw' : 'ro'}>{rw ? 'read/write' : 'read-only'}</td>
              </tr>
            ))}
            {MODBUS_REGS.map(([addr, tag, rw]) => (
              <tr key={`r${addr}`}>
                <td>
                  REGISTER {addr} <span className="ro">({40001 + addr})</span>
                </td>
                <td>{tag}</td>
                <td className={rw ? 'rw' : 'ro'}>{rw ? 'read/write' : 'read-only'}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="hint">
          BOOL tags also mirror onto discrete inputs; INT tags also mirror onto
          input registers. Any Modbus master — SCADA, another PLC, a phone app —
          can poll these addresses. Writes to read-only addresses are refused
          with exception 02. In DEMO mode the simulator drives the input tags,
          so master writes to them race the field exactly like a real input
          image; switch to MANUAL to drive over Modbus.
        </div>
      </div>

      <div className="foot">
        Try the interlock in MANUAL: start the motor, then open the gate — the
        motor drops and will not restart until you close the gate AND press
        Start again. The seal is a logic behavior, not a script.
      </div>
    </div>
  );
}
