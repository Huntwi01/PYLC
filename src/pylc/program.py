"""JSON ladder program loading, validation, and compilation.

A PYLC program is a single JSON file a maintenance tech can open in any
text editor:

.. code-block:: json

    {
      "name": "Conveyor Control",
      "tags": [
        {"name": "GateClosed", "type": "BOOL", "initial": 1},
        {"name": "MotorRun",   "type": "BOOL", "initial": 0}
      ],
      "rungs": [
        {
          "comment": "Motor seal-in with gate interlock",
          "branches": [
            [{"op": "XIC", "tag": "StartPB"}, {"op": "XIC", "tag": "GateClosed"}],
            [{"op": "XIC", "tag": "MotorRun"}, {"op": "XIC", "tag": "GateClosed"}]
          ],
          "output": {"op": "OTE", "tag": "MotorRun"}
        }
      ]
    }

Semantics (standard ladder logic):

- Contacts in one branch are in **series** (AND). Branches are in
  **parallel** (OR): the rung has power if any branch conducts.
- ``XIC`` (examine if closed) conducts when its tag is non-zero;
  ``XIO`` (examine if open) conducts when its tag is zero.
- Outputs: ``OTE`` — coil follows rung power every scan.
  ``CTU`` — count up: the target INT tag increments on the *rising
  edge* of rung power only. ``RES`` — while its rung has power, the
  target tag is held at 0 (counter/timer reset).
- Timers: a ``TIMER`` tag is driven by exactly one ``TON`` (on-delay)
  or ``TOF`` (off-delay) output with a ``preset_ms``. The tag's slot
  holds the accumulated milliseconds; its status bits are read with
  dotted contacts — ``XIC "PurgeDelay.DN"`` (done), ``.TT`` (timing),
  ``.EN`` (rung powered). A dot in a contact name always means a
  timer-bit reference.

Loading compiles tag names to slot indexes once, so the scan engine
never does dictionary lookups in the hot loop. Any structural problem
(unknown tag, unknown op, malformed rung) raises :class:`ProgramError`
naming the rung, before a single scan runs.
"""

import json

from .tags import TagDef, TagTable

CONTACT_OPS = ("XIC", "XIO")
OUTPUT_OPS = ("OTE", "CTU", "RES", "TON", "TOF")
TIMER_OPS = ("TON", "TOF")
TIMER_BITS = ("EN", "TT", "DN")


class ProgramError(Exception):
    """Raised when a program file is structurally invalid."""


class Program:
    """A validated, compiled ladder program ready for the scan engine."""

    def __init__(self, name, table, rungs, raw):
        self.name = name
        self.table = table          # TagTable (defs + shared storage)
        self.rungs = rungs          # compiled rung dicts (see load_program)
        self.raw = raw              # original parsed JSON (for tooling)

    def __repr__(self):
        return f"<Program {self.name!r}: {len(self.table)} tags, {len(self.rungs)} rungs>"


def load_program(path):
    """Load a program JSON file from disk."""
    with open(path, "r", encoding="utf-8") as f:
        return parse_program(json.load(f))


def parse_program(data):
    """Validate + compile a parsed program dict into a Program."""
    if not isinstance(data, dict):
        raise ProgramError("program must be a JSON object")
    name = data.get("name", "unnamed")

    tag_specs = data.get("tags")
    if not tag_specs:
        raise ProgramError(f"program {name!r}: no 'tags' list")
    defs = []
    for i, spec in enumerate(tag_specs):
        try:
            defs.append(TagDef(**spec))
        except TypeError as e:
            raise ProgramError(f"program {name!r}: tag #{i} is malformed: {e}") from None
    table = TagTable(defs)

    rung_specs = data.get("rungs")
    if rung_specs is None:
        raise ProgramError(f"program {name!r}: no 'rungs' list")
    rungs = []
    timer_drivers = {}  # TIMER tag name -> rung number driving it
    for i, spec in enumerate(rung_specs):
        rung = _compile_rung(table, spec, i + 1)
        if rung["output_op"] in TIMER_OPS:
            tname = table.names()[rung["output_idx"]]
            if tname in timer_drivers:
                raise ProgramError(
                    f"rung {i + 1}: timer {tname!r} is already driven by "
                    f"rung {timer_drivers[tname]} — one timer, one rung"
                )
            timer_drivers[tname] = i + 1
        rungs.append(rung)
    # A timer bit contact is only meaningful if some rung drives the timer.
    for i, rung in enumerate(rungs):
        for branch in rung["branches"]:
            for _is_xic, idx, bit in branch:
                if bit is not None:
                    tname = table.names()[idx]
                    if tname not in timer_drivers:
                        raise ProgramError(
                            f"rung {i + 1}: TIMER tag {tname!r} is read "
                            "(.EN/.TT/.DN) but no TON/TOF rung drives it"
                        )
    return Program(name, table, rungs, data)


def _compile_contact(table, tag, where, b_no):
    """Compile one contact's tag reference to (slot index, timer bit).

    Plain tags give (index, None). A dotted name reads a TIMER tag's
    status bit: (timer index, "EN"|"TT"|"DN").
    """
    if not isinstance(tag, str):
        raise ProgramError(f"{where}, branch {b_no}: contact tag must be a string")
    if "." in tag:
        base, _, bit = tag.rpartition(".")
        if base not in table:
            raise ProgramError(f"{where}, branch {b_no}: unknown tag {base!r}")
        if table.definition(base).type != "TIMER":
            raise ProgramError(
                f"{where}, branch {b_no}: {base!r} is not a TIMER tag — "
                "a dotted contact reads a timer bit"
            )
        if bit not in TIMER_BITS:
            raise ProgramError(
                f"{where}, branch {b_no}: timer bit must be one of "
                f"{TIMER_BITS}, got {bit!r}"
            )
        return table.index(base), bit
    if tag not in table:
        raise ProgramError(f"{where}, branch {b_no}: unknown tag {tag!r}")
    if table.definition(tag).type == "TIMER":
        raise ProgramError(
            f"{where}, branch {b_no}: TIMER tag {tag!r} has no plain contact "
            f"value — read a bit instead: {tag}.DN, {tag}.TT or {tag}.EN"
        )
    return table.index(tag), None


def _compile_rung(table, spec, rung_no):
    where = f"rung {rung_no}"
    if not isinstance(spec, dict):
        raise ProgramError(f"{where}: rung must be an object")
    branches_spec = spec.get("branches")
    if not branches_spec:
        raise ProgramError(f"{where}: no 'branches' (a rung needs at least one)")

    branches = []
    for b_no, branch in enumerate(branches_spec, start=1):
        contacts = []
        for c in branch:
            op = c.get("op")
            if op not in CONTACT_OPS:
                raise ProgramError(
                    f"{where}, branch {b_no}: contact op must be one of "
                    f"{CONTACT_OPS}, got {op!r}"
                )
            tag = c.get("tag")
            idx, bit = _compile_contact(table, tag, where, b_no)
            contacts.append((op == "XIC", idx, bit))
        branches.append(contacts)

    out = spec.get("output")
    if not isinstance(out, dict):
        raise ProgramError(f"{where}: missing 'output' object")
    op = out.get("op")
    if op not in OUTPUT_OPS:
        raise ProgramError(
            f"{where}: output op must be one of {OUTPUT_OPS}, got {op!r}"
        )
    tag = out.get("tag")
    if tag not in table:
        raise ProgramError(f"{where}: unknown output tag {tag!r}")
    tag_type = table.definition(tag).type
    if op == "CTU" and tag_type != "INT":
        raise ProgramError(
            f"{where}: CTU target {tag!r} must be an INT tag, got {tag_type}"
        )
    if op == "RES" and tag_type not in ("INT", "TIMER"):
        raise ProgramError(
            f"{where}: RES target {tag!r} must be an INT or TIMER tag, "
            f"got {tag_type}"
        )
    if op == "OTE" and tag_type == "TIMER":
        raise ProgramError(
            f"{where}: OTE cannot drive TIMER tag {tag!r} — its slot holds "
            "the timer's accumulated ms; drive it with TON or TOF"
        )
    preset_ms = None
    if op in TIMER_OPS:
        if tag_type != "TIMER":
            raise ProgramError(
                f"{where}: {op} target {tag!r} must be a TIMER tag, "
                f"got {tag_type}"
            )
        preset_ms = out.get("preset_ms")
        if isinstance(preset_ms, bool) or not isinstance(preset_ms, int) or preset_ms < 0:
            raise ProgramError(
                f"{where}: {op} needs a non-negative integer 'preset_ms'"
            )

    return {
        "branches": branches,
        "output_op": op,
        "output_idx": table.index(tag),
        "preset_ms": preset_ms,
        "comment": spec.get("comment", ""),
    }
