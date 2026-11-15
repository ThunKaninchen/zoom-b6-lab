"""PatchEditor: edits one open patch of the pedal, live, with state, undo, save and replay."""
from __future__ import annotations

from b6lab import ptcf
from b6lab.core.edits import (DeleteEdit, InsertEdit, KnobEdit, LevelEdit, MoveEdit, NameEdit,
                              OnEdit, TypeEdit, default_namer)
from b6lab.core.history import Stack, Step
from b6lab.core.model import EditState, Gates, Outcome
from b6lab.core.writes import GuardedWriter
from b6lab.device import used_bytes
from b6lab.live import LiveError
from b6lab.patchspec import is_empty_patch
from b6lab.search import slot_label

_STRUCTURAL = (TypeEdit, InsertEdit, DeleteEdit, MoveEdit)
# RuntimeError: the pedal answered with an error code (b6lab.midi)
_SEND_ERRORS = (LiveError, TimeoutError, OSError, ValueError, IndexError, RuntimeError)


def _flat(edits) -> list:
    return list(edits) if isinstance(edits, (tuple, list)) else [edits]


class PatchEditor:
    def __init__(self, dev, slot: int, stored: bytes, writer: GuardedWriter, gates: Gates,
                 namer=default_namer):
        self.dev = dev
        self.slot = slot
        self.stored = bytes(stored)
        self.data = bytes(stored)
        self.writer = writer
        self.gates = gates
        self.namer = namer
        self.state = EditState.STORED
        self.stack = Stack()
        self.failed: str | None = None
        # a library action wrote this slot: saving over it would lose what is there now
        self.stale = False
        # called with (slot, data) after a verified save (the session updates its library)
        self.on_saved = None
        # applied edits, aligned with the stack, to replay them onto the stored patch
        self._log: list = []      # per applied-or-undone step: tuple of Edits
        self._pos = 0
        self._saved_pos: int = 0
        self._pre: list = []      # edits that lead from stored to the state at _saved_pos
        self._tops: list = []     # the Step object of each log item (carries its first old value)
        p0 = ptcf.parse(self.stored)
        self._name, self._level = p0.name, p0.level  # held locally: the pedal never sees them

    # state --------------------------------------------------------------------------------

    @property
    def changes(self) -> int:
        n = self.stack.changes_since_saved
        if n == 0 and used_bytes(self.data) != used_bytes(self.stored):
            return 1
        return n

    def _overlay(self, data: bytes) -> bytes:
        """The locally-held name and level on top of what the pedal reported."""
        p = ptcf.parse(bytes(data))
        if p.name == self._name and p.level == self._level:
            return bytes(data)
        if p.name != self._name:
            ptcf.set_name(p, self._name)
        if p.level != self._level:
            ptcf.set_level(p, self._level)
        return ptcf.encode(p)

    def _patch(self):
        return ptcf.parse(self.data)

    def _effect(self, entry: int):
        """The effect at chain entry `entry`; IndexError when there is none (stale index)."""
        if not 0 <= entry < len(self._patch().effects):
            raise IndexError(entry)
        return self._patch().effects[entry]

    def _make(self, build) -> Outcome:
        """Build the edit and apply it; an index that no longer fits the chain is an Outcome."""
        try:
            edit = build()
        except (IndexError, ValueError):
            return Outcome(False, "That effect or knob is no longer in the patch")
        return self._edit(edit)

    def _fail(self, label: str) -> Outcome:
        message = f"{label} not applied: pedal did not answer"
        self.failed = message
        try:
            self.data = self._overlay(bytes(self.dev.read_edit()))
        except Exception:
            pass
        self.state = EditState.FAILED
        return Outcome(False, message)

    def _run(self, edits, data: bytes, label: str):
        """Send the edits in order; returns (data, None) or (data, failure Outcome)."""
        self.state = EditState.SENDING
        try:
            for e in _flat(edits):
                data = e.send(self.dev, data)
                if isinstance(e, NameEdit):
                    self._name = e.new
                elif isinstance(e, LevelEdit):
                    self._level = e.new
                data = self._overlay(data)
        except _SEND_ERRORS:
            return data, self._fail(label)
        self.failed = None
        return data, None

    # edits --------------------------------------------------------------------------------

    def _edit(self, edit) -> Outcome:
        label = edit.label(self.namer)
        data, failure = self._run(edit, self.data, label)
        if failure is not None:
            return failure
        self.data = bytes(data)
        self.state = EditState.EDITED
        self._push(edit, label)
        return Outcome(True, label)

    def _push(self, edit, label: str) -> None:
        key = edit.merge_key
        if self._saved_pos > self._pos:
            self._pre += [i for step in reversed(self._log[self._pos:self._saved_pos])
                          for i in self._inverses(step)]
            self._saved_pos = self._pos
        # steps above _pos were undone and are dropped by the stack's push
        del self._log[self._pos:]
        del self._tops[self._pos:]
        top = self._tops[-1] if self._tops else None
        inherit = key is not None and top is not None and top.merge_key == key
        # guess a merge for the label; fixed below when the stack did not merge
        shown_label = label
        if inherit:
            shown = KnobEdit(edit.entry, edit.index, top.first_old, edit.new, edit.fx_id)
            shown_label = shown.label(self.namer)
        step = Step(shown_label, lambda e=edit: self._send_step(e.inverse(), label, "undo"),
                    lambda e=edit: self._send_step(e, label, "redo"), key)
        step.first_old = getattr(edit, "old", None)
        self.stack.push(step)
        if step.seq == 0:  # the stack merged it into the top step (its own decision)
            return  # the top step keeps its own first_old
        step.label = label
        self._log.append((edit,))
        self._tops.append(step)
        self._pos += 1

    @staticmethod
    def _inverses(step) -> list:
        out = []
        for e in reversed(step):
            out += _flat(e.inverse())
        return out

    def _send_step(self, edits, label: str, verb: str) -> Outcome:
        data, failure = self._run(edits, self.data, label)
        if failure is not None:
            return failure
        self.data = bytes(data)
        self.state = EditState.EDITED
        return Outcome(True, f"{'Undid' if verb == 'undo' else 'Redid'} {label}")

    def set_knob(self, entry: int, index: int, value: int) -> Outcome:
        def build():
            eff = self._effect(entry)
            if not 0 <= index < len(eff.params):
                raise IndexError(index)
            return KnobEdit(entry, index, eff.params[index], int(value), eff.fx_id)
        return self._make(build)

    def set_on(self, entry: int, on: bool) -> Outcome:
        def build():
            eff = self._effect(entry)
            return OnEdit(entry, bool(eff.on), bool(on), eff.fx_id)
        return self._make(build)

    def change_type(self, entry: int, fx_id: int) -> Outcome:
        return self._make(lambda: TypeEdit(entry, self._effect(entry).fx_id, fx_id))

    def insert(self, position: int) -> Outcome:
        return self._edit(InsertEdit(position))

    def delete(self, position: int) -> Outcome:
        def build():
            self._effect(position)
            return DeleteEdit.from_patch(self._patch(), position)
        return self._make(build)

    def move(self, src: int, dst: int) -> Outcome:
        return self._make(lambda: MoveEdit(src, dst, self._effect(src).fx_id))

    def rename(self, name: str) -> Outcome:
        return self._edit(NameEdit(self._patch().name, name))

    def set_level(self, value: int) -> Outcome:
        return self._edit(LevelEdit(self._patch().level, int(value)))

    # undo / redo ----------------------------------------------------------------------------

    def undo(self) -> Outcome:
        out = self.stack.undo()
        if out is None:
            return Outcome(False, "Nothing to undo")
        if out.ok:
            self._pos -= 1
        return out

    def redo(self) -> Outcome:
        out = self.stack.redo()
        if out is None:
            return Outcome(False, "Nothing to redo")
        if out.ok:
            self._pos += 1
        return out

    # save / replay --------------------------------------------------------------------------

    def save(self, dest: int | None = None, confirm_overwrite: bool = False) -> Outcome:
        """Write `data` to `dest` (default: this patch's own slot). Never raises. The own slot
        must still hold the stored patch, or the edits (AUTO SAVE stored them); when it holds
        anything else, or a library action wrote it, nothing is saved."""
        dest = self.slot if dest is None else dest
        try:
            return self._save(dest, confirm_overwrite)
        except Exception as e:  # never into the UI
            return Outcome(False, f"Save not confirmed: {e}")

    def _changed(self, dest: int) -> Outcome:
        return Outcome(False, f"{slot_label(dest)} changed on the pedal. Save it to another "
                              f"memory or reload it")

    def _own_slot_ok(self, current: bytes) -> bool:
        """The own slot holds the stored patch, or the edits as AUTO SAVE stores them (the
        pedal never sees the locally held name and level)."""
        have = used_bytes(current)
        if have in (used_bytes(self.stored), used_bytes(self.data)):
            return True
        try:
            p, s0 = ptcf.parse(self.data), ptcf.parse(self.stored)
            ptcf.set_name(p, s0.name)
            ptcf.set_level(p, s0.level)
            return have == used_bytes(ptcf.encode(p))
        except Exception:
            return False

    def _save(self, dest: int, confirm_overwrite: bool) -> Outcome:
        own = dest == self.slot
        if own and self.stale:
            return self._changed(dest)
        try:
            current = bytes(self.dev.read_slot(dest))
        except Exception as e:
            return Outcome(False, f"Not saved: could not read {slot_label(dest)}: {e}")
        if own and not self._own_slot_ok(current):
            return self._changed(dest)
        if not own and not is_empty_patch(current):
            if not self.gates.overwrite_slots:
                return self.gates.refusal("overwrite_slots")
            if not confirm_overwrite:
                return Outcome(False, "needs_confirm")
        res = self.writer.write(self.dev, "save", dest, self.data, expected=current,
                                select=True, store_edit=True)
        backups = (res.backup_file,) if res.backup_file else ()
        if not res.ok:
            if res.message == "changed":
                return Outcome(False, self._changed(dest).message, backups)
            return Outcome(False, f"Not saved: {res.message}", backups)
        self.stack.mark_saved()
        self._saved_pos = self._pos
        self._pre = []
        self.slot = dest  # the writer selected dest: the editor follows the pedal
        self.stored = self.data
        self.stale = False
        self.state = EditState.STORED
        self.failed = None
        if self.on_saved is not None:
            try:
                self.on_saved(dest, self.data)
            except Exception:
                pass  # bookkeeping only: the save itself is confirmed
        return Outcome(True, f"Saved to {slot_label(dest)}", backups)

    def edits_since_stored(self) -> list:
        """The structural edits (type, insert, delete, move) that lead from the stored patch to
        the current chain, in order; knob and switch values are not included (replay copies
        them from `data`)."""
        if self._pos >= self._saved_pos:
            edits = list(self._pre) + [e for step in self._log[self._saved_pos:self._pos]
                                       for e in step]
        else:
            undone = self._log[self._pos:self._saved_pos]
            edits = self._pre + [i for step in reversed(undone) for i in self._inverses(step)]
        return [e for e in edits if isinstance(e, _STRUCTURAL)]

    def replay(self) -> Outcome:
        """After the pedal re-selected this patch: redo the chain edits, then copy every knob and
        switch that differs from `data`, and check the pedal against `data`."""
        failed = Outcome(False, "Could not restore your changes on the pedal")
        self.state = EditState.SENDING
        try:
            data = self.stored
            for e in self.edits_since_stored():
                data = e.send(self.dev, data)
            cur = ptcf.parse(bytes(self.dev.read_edit()))
            want = ptcf.parse(self.data)
            if [e.fx_id for e in cur.effects] != [e.fx_id for e in want.effects]:
                raise LiveError("the chain on the pedal differs")
            for i, (c, w) in enumerate(zip(cur.effects, want.effects)):
                if c.is_tail:
                    continue
                for idx, (a, b) in enumerate(zip(c.params, w.params)):
                    if a != b:
                        KnobEdit(i, idx, a, b, w.fx_id).send(
                            self.dev, ptcf.encode(ptcf.parse(bytes(self.dev.read_edit()))))
                if c.on != w.on:
                    OnEdit(i, c.on, w.on, w.fx_id).send(self.dev, self.data)
            got = ptcf.parse(bytes(self.dev.read_edit()))
            same = [e.fx_id for e in got.effects] == [e.fx_id for e in want.effects] and all(
                g.on == w.on and g.params == w.params
                for g, w in zip(got.effects, want.effects) if not w.is_tail)
        except _SEND_ERRORS:
            same = False
        if not same:
            self.failed = failed.message
            self.state = EditState.FAILED
            return failed
        self.failed = None
        self.state = EditState.EDITED
        return Outcome(True, "Restored your changes")

    # pedal side -----------------------------------------------------------------------------

    def pedal_changed(self, entry: int, index: int | None, value: int) -> None:
        """A knob or switch changed on the pedal: `data` follows, no undo step."""
        p = self._patch()
        if index is None:
            ptcf.set_on(p, entry, bool(value))
        else:
            ptcf.set_param(p, entry, index, int(value))
        self.data = ptcf.encode(p)
        self.state = EditState.EDITED

    def revert_to_stored(self) -> None:
        self.data = self.stored
        p = ptcf.parse(self.stored)
        self._name, self._level = p.name, p.level
        self.state = EditState.STORED
