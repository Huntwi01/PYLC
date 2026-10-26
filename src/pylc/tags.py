"""Named tag table backed by a flat int32 storage.

A real PLC addresses data by name (``MotorRun``), not by raw array
offset. The TagTable keeps that model while staying on the same kind of
shared-memory storage the original prototype used: every tag occupies
one int32 slot in a sequence (a plain list in tests, a
``multiprocessing.Array`` in the live runtime), so several processes can
share one tag space.

BOOL tags are stored as 0/1. INT tags hold a signed 32-bit value.
TIMER tags hold the timer's accumulated time (ACC) in milliseconds;
their EN/TT/DN bits live in the scan engine and are read through
dotted contacts such as ``PurgeDelay.DN`` (see pylc.program).
"""

from dataclasses import dataclass

VALID_TYPES = ("BOOL", "INT", "TIMER")


@dataclass(frozen=True)
class TagDef:
    name: str
    type: str = "BOOL"
    initial: int = 0
    comment: str = ""


class TagError(Exception):
    """Raised for duplicate/unknown tags and bad tag definitions."""


class TagTable:
    """Maps tag names to slots in a shared int32 storage sequence."""

    def __init__(self, defs, storage=None):
        self._defs = []
        self._index = {}
        for d in defs:
            if not isinstance(d, TagDef):
                d = TagDef(**d)
            if d.type not in VALID_TYPES:
                raise TagError(
                    f"tag {d.name!r}: type must be one of {VALID_TYPES}, got {d.type!r}"
                )
            if d.name in self._index:
                raise TagError(f"duplicate tag name: {d.name!r}")
            self._index[d.name] = len(self._defs)
            self._defs.append(d)
        if storage is None:
            storage = [int(d.initial) for d in self._defs]
        if len(storage) != len(self._defs):
            raise TagError(
                f"storage has {len(storage)} slots but {len(self._defs)} tags are defined"
            )
        self.storage = storage

    # -- metadata -----------------------------------------------------
    def __len__(self):
        return len(self._defs)

    def __contains__(self, name):
        return name in self._index

    def names(self):
        return [d.name for d in self._defs]

    def definition(self, name):
        return self._defs[self.index(name)]

    def index(self, name):
        """Slot number for a tag name (used by the compiled engine)."""
        try:
            return self._index[name]
        except KeyError:
            raise TagError(f"unknown tag: {name!r}") from None

    # -- value access -------------------------------------------------
    def get(self, name):
        return self.storage[self.index(name)]

    def set(self, name, value):
        self.storage[self.index(name)] = int(value)

    def snapshot(self):
        """Current {name: value} for every tag (logging/HMI/debugging)."""
        return {d.name: self.storage[i] for i, d in enumerate(self._defs)}
