"""Undo/redo stacks with merging of quick changes, a step limit and a save point."""
from __future__ import annotations

import itertools
import time
from dataclasses import dataclass
from typing import Callable, Hashable

from b6lab.core.model import Outcome

MERGE_WINDOW = 0.7
_seq = itertools.count(1)


@dataclass
class Step:
    label: str
    undo: Callable[[], Outcome]
    redo: Callable[[], Outcome]
    merge_key: Hashable | None = None
    seq: int = 0
    at: float = 0.0


class Stack:
    def __init__(self, limit: int = 100, clock: Callable[[], float] = time.monotonic):
        self.limit = limit
        self.clock = clock
        self._items: list[Step] = []
        self._pos = 0  # steps before _pos are applied, the rest are redoable
        self._saved: int | None = 0  # position of the saved state, None when unreachable

    def push(self, step: Step) -> None:
        now = self.clock()
        if self._saved is not None and self._saved > self._pos:
            self._saved = None
        del self._items[self._pos:]
        top = self._items[-1] if self._items else None
        if (top is not None and step.merge_key is not None and top.merge_key == step.merge_key
                and now - top.at <= MERGE_WINDOW and self._saved != self._pos):
            top.redo = step.redo
            top.label = step.label
            top.at = now
            top.seq = next(_seq)
            return
        step.seq = next(_seq)
        step.at = now
        self._items.append(step)
        self._pos += 1
        if len(self._items) > self.limit:
            drop = len(self._items) - self.limit
            del self._items[:drop]
            self._pos -= drop
            if self._saved is not None:
                self._saved -= drop
                if self._saved < 0:
                    self._saved = None

    def undo(self) -> Outcome | None:
        if not self.can_undo:
            return None
        outcome = self._items[self._pos - 1].undo()
        if outcome.ok:
            self._pos -= 1
        return outcome

    def redo(self) -> Outcome | None:
        if not self.can_redo:
            return None
        outcome = self._items[self._pos].redo()
        if outcome.ok:
            self._pos += 1
        return outcome

    @property
    def top_seq(self) -> int | None:
        return self._items[self._pos - 1].seq if self._pos else None

    @property
    def can_undo(self) -> bool:
        return self._pos > 0

    @property
    def can_redo(self) -> bool:
        return self._pos < len(self._items)

    @property
    def undo_label(self) -> str | None:
        return self._items[self._pos - 1].label if self._pos else None

    @property
    def redo_label(self) -> str | None:
        return self._items[self._pos].label if self.can_redo else None

    def __len__(self) -> int:
        return self._pos

    def mark_saved(self) -> None:
        self._saved = self._pos

    @property
    def changes_since_saved(self) -> int:
        if self._saved is None:
            return self._pos
        return abs(self._pos - self._saved)


def newest(*stacks: Stack) -> Stack | None:
    best, best_seq = None, -1
    for s in stacks:
        seq = s.top_seq
        if seq is not None and seq > best_seq:
            best, best_seq = s, seq
    return best
