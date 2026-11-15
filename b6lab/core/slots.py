"""Library actions on the pedal's patch memories: copy, replace, swap, move and clear.

Every write goes through the GuardedWriter (backup before each write, log line, read-back) and
never selects the slot. Slots are 0-based device slots; messages use `slot_label`. Each action
pushes one library undo step that refuses when a slot no longer holds what the action wrote.
"""
from __future__ import annotations

from b6lab import patchspec, ptcf
from b6lab.core.history import Stack, Step
from b6lab.core.model import Gates, Outcome
from b6lab.core.writes import GuardedWriter
from b6lab.device import used_bytes
from b6lab.midi import bank_name_bytes
from b6lab.search import slot_label

def empty_patch() -> bytes:
    """The pedal's Empty patch: named "Empty", no effects, PRM2 of 32 zero bytes."""
    p = patchspec._new_patch()
    ptcf.set_name(p, "Empty")
    return ptcf.encode(p)


def _bank_norm(name) -> str:
    """A bank name as the pedal keeps it: 10 characters, non-ASCII as a space, no trailing
    spaces."""
    return bank_name_bytes(name).decode("ascii").rstrip(" ")


def _same(a: bytes, b: bytes) -> bool:
    return used_bytes(a) == used_bytes(b)


class Library:
    def __init__(self, dev, writer: GuardedWriter, gates: Gates):
        self.dev = dev
        self.writer = writer
        self.gates = gates
        self.slots: list[bytes] = []
        self.stack = Stack()
        self.bank_names: list[str] = [""] * 60
        # called with the slot before each write to it (the session marks editors on it stale)
        self.on_write = None

    # reading ------------------------------------------------------------------------------

    def refresh(self) -> Outcome:
        try:
            count = self.dev.patch_count() or 240
            self.slots = [bytes(self.dev.read_slot(i)) for i in range(count)]
        except Exception as e:  # never into the UI (the real pedal raises RuntimeError too)
            return Outcome(False, f"Could not read the patches: {e}")
        try:
            self.bank_names = [str(n) for n in self.dev.read_bank_names()]
        except Exception:  # non-fatal: the names stay as they were
            pass
        return Outcome(True, f"Read {len(self.slots)} patches")

    def is_empty(self, slot: int) -> bool:
        return 0 <= slot < len(self.slots) and patchspec.is_empty_patch(self.slots[slot])

    def _set(self, slot: int, data: bytes) -> None:
        while len(self.slots) <= slot:
            self.slots.append(b"")
        self.slots[slot] = bytes(data)

    # writing ------------------------------------------------------------------------------

    def _run(self, action: str, writes: list):
        """Apply [(slot, new, expected), ...] in order. Returns (Outcome, changed_slot):
        changed_slot is set (and nothing was written) when a slot no longer holds what it is
        expected to. Never raises. When a later write fails, that slot is read back: if it
        still holds its original bytes the earlier writes are rolled back (reported only after
        a verified read-back); if not, nothing is rolled back (the earlier write holds the only
        good copy) and the damage is reported."""
        backups: list = []
        try:
            return self._run_inner(action, writes, backups)
        except Exception as e:  # never into the UI
            self._reload([w[0] for w in writes])
            return Outcome(False, f"{action.capitalize()} failed: {e}", tuple(backups)), None

    def _reload(self, slots) -> None:
        for s in slots:
            try:
                self._set(s, bytes(self.dev.read_slot(s)))
            except Exception:
                pass

    def _write(self, action, slot, new, expected, backups):
        """writer.write that never raises; returns (ok, message, touched). `touched` is False
        when the pedal was surely not written (slot changed under us, or no backup made)."""
        if self.on_write is not None:
            try:
                self.on_write(slot)
            except Exception:
                pass
        try:
            res = self.writer.write(self.dev, action, slot, new, expected, False)
        except Exception as e:
            return False, str(e), self._holds(slot, new)
        if res.backup_file:
            backups.append(res.backup_file)
        return res.ok, res.message, bool(res.backup_file) and res.message != "changed"

    @staticmethod
    def _name_of(data: bytes) -> str:
        try:
            return ptcf.parse(bytes(data)).name
        except Exception:
            return "its patch"

    def _holds(self, slot: int, expected: bytes) -> bool:
        try:
            return _same(self.dev.read_slot(slot), expected)
        except Exception:
            return False

    def _run_inner(self, action: str, writes: list, backups: list):
        for slot, _new, expected in writes:  # refuse before touching anything
            try:
                current = self.dev.read_slot(slot)
            except Exception as e:
                return Outcome(False, f"{action.capitalize()} failed: {e}"), None
            if not _same(current, expected):
                return Outcome(False, f"{slot_label(slot)} changed"), slot
        done: list = []
        cap = action.capitalize()
        for slot, new, expected in writes:
            ok, message, touched = self._write(action, slot, new, expected, backups)
            if ok:
                done.append((slot, new, expected))
                self._set(slot, new)
                continue
            if message == "changed":  # never written: no restore, no rollback
                self._reload([slot])
                lab = slot_label(slot)
                if not done:
                    return Outcome(False, f"{cap} failed: {lab} changed on the pedal. "
                                          f"Nothing was written", tuple(backups)), None
                first = done[0]
                if action == "swap":
                    msg = (f"Swap stopped: {lab} changed on the pedal. {slot_label(first[0])} "
                           f"now holds {self._name_of(first[1])}; {slot_label(first[0])}'s own "
                           f"patch is in Backups")
                elif action == "move":
                    msg = (f"Move stopped: {lab} changed on the pedal. The patch is also in "
                           f"{slot_label(first[0])}")
                else:
                    msg = f"{cap} stopped: {lab} changed on the pedal"
                return Outcome(False, msg, tuple(backups)), None
            intact = self._holds(slot, expected)
            self._reload([slot])
            if intact and not done:
                return Outcome(False, f"{cap} failed: {message}", tuple(backups)), None
            if not intact:
                if not done and not touched:
                    return Outcome(False, f"{cap} failed: {message}", tuple(backups)), None
                if not done:  # the first write damaged its slot: put the old bytes back
                    ok2, _m, _t = self._write(f"{action} rollback", slot, expected, None, backups)
                    if ok2 and self._holds(slot, expected):
                        self._set(slot, expected)
                        return Outcome(False, f"{cap} failed; {slot_label(slot)} restored",
                                       tuple(backups)), None
                    return Outcome(False, f"{cap} failed and {slot_label(slot)} could not be "
                                          f"restored. Its backup is in Backups",
                                   tuple(backups)), None
                first = done[0]
                dmg = slot_label(slot)
                if action == "swap":
                    held = self._name_of(first[1])
                    msg = (f"Swap failed; {dmg} may be damaged. Its backup is in Backups. "
                           f"{slot_label(first[0])} now holds {held}")
                elif action == "move":
                    msg = (f"Move failed; {dmg} may be damaged. The patch is in "
                           f"{slot_label(first[0])}, and {dmg}'s backup is in Backups")
                else:
                    msg = f"{cap} failed; {dmg} may be damaged. Its backup is in Backups"
                return Outcome(False, msg, tuple(backups)), None
            # the failed slot is intact: roll the earlier writes back, newest first
            for rslot, rnew, rold in reversed(done):
                ok2, _m, _t = self._write(f"{action} rollback", rslot, rold, rnew, backups)
                if ok2:
                    self._set(rslot, rold)
                else:
                    self._reload([rslot])
                    return Outcome(False, f"{cap} failed and {slot_label(rslot)} could not be "
                                          f"restored. Its backup is in Backups",
                                   tuple(backups)), None
            return Outcome(False, f"{cap} failed; {slot_label(done[0][0])} restored",
                           tuple(backups)), None
        return Outcome(True, "", tuple(backups)), None

    def _step(self, action: str, label: str, writes: list) -> Step:
        undo_items = [(s, exp, new) for s, new, exp in reversed(writes)]

        def wrap(kind: str, past: str, items, since: str) -> Outcome:
            out, changed = self._run(action, items)
            if changed is not None:
                return Outcome(False, f"{slot_label(changed)} changed since the {since}; "
                                      f"{kind} would overwrite it. Its backup is in Backups")
            if out.ok:
                return Outcome(True, f"{past}: {label}", out.backups)
            return Outcome(False, f"{kind.capitalize()} of {label} failed: {out.message}",
                           out.backups)

        return Step(label, lambda: wrap("undo", "Undone", undo_items, action),
                    lambda: wrap("redo", "Redone", writes, "undo"))

    def _do(self, action: str, label: str, writes: list, done: str) -> Outcome:
        out, changed = self._run(action, writes)
        if changed is not None:
            return Outcome(False, f"{slot_label(changed)} changed on the pedal, nothing written")
        if not out.ok:
            return out
        self.stack.push(self._step(action, label, writes))
        return Outcome(True, done, out.backups)

    def _read(self, *slots: int):
        try:
            return [bytes(self.dev.read_slot(s)) for s in slots], None
        except Exception as e:
            return None, Outcome(False, f"Could not read the pedal: {e}")

    def _check(self, a: int, b: int):
        if a == b:
            return Outcome(False, "Pick two different memories")
        return None

    # actions ------------------------------------------------------------------------------

    def copy(self, src: int, dst: int) -> Outcome:
        if (bad := self._check(src, dst)):
            return bad
        got, err = self._read(src, dst)
        if err:
            return err
        data, old = got
        if not patchspec.is_empty_patch(old):
            return Outcome(False, "needs_choice")
        return self._do("copy", f"Copy {slot_label(src)} → {slot_label(dst)}",
                        [(dst, data, old)], f"Copied to {slot_label(dst)}")

    def replace(self, src: int, dst: int) -> Outcome:
        if not self.gates.overwrite_slots:
            return self.gates.refusal("overwrite_slots")
        if (bad := self._check(src, dst)):
            return bad
        got, err = self._read(src, dst)
        if err:
            return err
        data, old = got
        return self._do("replace", f"Replace {slot_label(dst)}", [(dst, data, old)],
                        f"Replaced {slot_label(dst)}")

    def swap(self, a: int, b: int) -> Outcome:
        if not self.gates.overwrite_slots:
            return self.gates.refusal("overwrite_slots")
        if (bad := self._check(a, b)):
            return bad
        got, err = self._read(a, b)
        if err:
            return err
        da, db = got
        return self._do("swap", f"Swap {slot_label(a)} ↔ {slot_label(b)}",
                        [(a, db, da), (b, da, db)],
                        f"Swapped {slot_label(a)} and {slot_label(b)}")

    def move(self, src: int, dst: int, confirm: bool = False) -> Outcome:
        """Move src to dst and clear src. Onto a filled dst: Outcome(False, "needs_choice")
        unless `confirm`."""
        # Writing Empty over the source always overwrites a filled patch: always gated.
        if not self.gates.overwrite_slots:
            return self.gates.refusal("overwrite_slots")
        if (bad := self._check(src, dst)):
            return bad
        got, err = self._read(src, dst)
        if err:
            return err
        data, old = got
        if not patchspec.is_empty_patch(old) and not confirm:
            return Outcome(False, "needs_choice")
        return self._do("move", f"Move {slot_label(src)} → {slot_label(dst)}",
                        [(dst, data, old), (src, empty_patch(), data)],
                        f"Moved to {slot_label(dst)}")

    def clear(self, slot: int) -> Outcome:
        if not self.gates.overwrite_slots:
            return self.gates.refusal("overwrite_slots")
        got, err = self._read(slot)
        if err:
            return err
        (old,) = got
        return self._do("clear", f"Clear {slot_label(slot)}", [(slot, empty_patch(), old)],
                        f"Cleared {slot_label(slot)}")

    def rename_bank(self, bank: int, name: str) -> Outcome:
        if not self.gates.write_bank_names:
            return self.gates.refusal("write_bank_names")
        if not 0 <= bank < 60:
            return Outcome(False, "Pick a bank from 1 to 60")
        if len(name) > 10:
            return Outcome(False, "A bank name has at most 10 characters")
        try:
            old = [str(n) for n in self.dev.read_bank_names()]
        except Exception as e:
            return Outcome(False, f"Could not read the bank names: {e}")
        new = list(old)
        new[bank] = name
        label = f"Rename bank {bank + 1}"

        def apply(names: list, past: str) -> Outcome:
            try:
                self.dev.write_bank_names(names)
                back = [str(n) for n in self.dev.read_bank_names()]
            except Exception as e:
                return Outcome(False, f"{label} failed: {e}")
            self.bank_names = back
            if [_bank_norm(n) for n in back] != [_bank_norm(n) for n in names]:
                return Outcome(False, f"Bank {bank + 1} not renamed: the pedal did not confirm")
            return Outcome(True, f"{past}: {label}")

        out = apply(new, "")
        if not out.ok:
            return out
        self.stack.push(Step(label, lambda: apply(old, "Undone"), lambda: apply(new, "Redone")))
        return Outcome(True, f"Bank {bank + 1} renamed")
