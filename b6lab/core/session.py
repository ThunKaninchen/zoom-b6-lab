"""Session: the top of the app core. Connection state, AUTO SAVE, the open patch (and an A/B
compare), the undo router, events the pedal sends on its own, Release, and the last-read cache.

Every method returns an Outcome (or a list of messages) and never raises: a device exception
becomes a plain-text Outcome and updates `link`. Slots are 0-based device slots.
"""
from __future__ import annotations

import functools
import json
import os
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from b6lab import ptcf
from b6lab.codec import from7
from b6lab.core.compare import Compare
from b6lab.core.edits import default_namer
from b6lab.core.history import Stack, newest
from b6lab.core.model import Gates, Link, Outcome
from b6lab.core.patch_editor import PatchEditor
from b6lab.core.slots import Library
from b6lab.core.writes import GuardedWriter
from b6lab.midi import DeviceBusy, NotConnected
from b6lab.search import slot_label

LOST = "You switched patch on the pedal; {n} unsaved changes to {name} are lost"
ANSWERS_AGAIN = "The pedal answers again"
STILL_NO_ANSWER = "Still no answer: unplug the pedal and plug it in again"
LINK_TEXT = {
    Link.BUSY: "B6 in use by another program",
    Link.NOT_FOUND: "No B6 found",
    Link.NOT_ANSWERING: "The pedal did not answer",
}
_EDIT_MSG = bytes([0x52, 0x00, 0x6E, 0x64, 0x20])
# selections the app made whose program change echo is still expected: at most this many. An
# entry expires (its echo was lost, or the pedal ignored the select) only once it has survived
# ECHO_POLLS polls and ECHO_MIN_SECONDS have passed, so the app's own slow work after a select
# (a replay) never expires it, and a lost echo can't make a later real change look like one.
ECHO_LIMIT = 8
ECHO_POLLS = 3
ECHO_MIN_SECONDS = 1.0
# requests the pedal answers: a reply means the link works again
_ANSWERS = {"read_slot", "read_edit", "global_info", "identity", "patch_count", "patch_info",
            "selected_slot", "read_bank_names"}


class NoDevice(NotConnected, OSError):
    """No device is open (the port is empty); an OSError so editors and the library catch it."""


def link_for(exc: BaseException) -> Link | None:
    """The link state a device exception means, or None when it says nothing about the link."""
    if isinstance(exc, DeviceBusy):
        return Link.BUSY
    if isinstance(exc, NotConnected):
        return Link.NOT_FOUND
    if isinstance(exc, (TimeoutError, OSError)):
        return Link.NOT_ANSWERING
    return None


class _Port:
    """The one device handle the library and every editor share. Forwards to the open device;
    a device exception updates the session's link before it goes on to the caller. With no
    device open, every use raises NoDevice."""

    def __init__(self, session: "Session"):
        self.__dict__["_session"] = session
        self.__dict__["target"] = None

    def __getattr__(self, name: str):
        target = self.__dict__["target"]
        if target is None:
            raise NoDevice("No B6 connected")
        attr = getattr(target, name)
        if not callable(attr):
            return attr
        session = self.__dict__["_session"]

        def call(*args, **kwargs):
            try:
                result = attr(*args, **kwargs)
            except Exception as e:
                link = link_for(e)
                if link is not None:
                    session.link = link
                raise
            if name == "select_slot":  # every selection the app makes, from any caller
                session._selected_by_app(args[0] if args else kwargs.get("slot"))
            if name in _ANSWERS and session.link is Link.NOT_ANSWERING:
                session.link = Link.CONNECTED
            return result
        return call


def _guarded(method):
    @functools.wraps(method)
    def wrap(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception as e:  # never into the UI
            return self._fail(e)
    return wrap


def _name(editor: PatchEditor) -> str:
    try:
        return ptcf.parse(editor.data).name
    except Exception:
        return slot_label(editor.slot)


def _same_sound(a: bytes, b: bytes) -> bool:
    """True when two patches have the same chain, on/off switches and knob values."""
    try:
        pa, pb = ptcf.parse(bytes(a)), ptcf.parse(bytes(b))
    except Exception:
        return False
    if [e.fx_id for e in pa.effects] != [e.fx_id for e in pb.effects]:
        return False
    return all(x.on == y.on and x.params == y.params
               for x, y in zip(pa.effects, pb.effects) if not y.is_tail)


class Session:
    def __init__(self, open_device: Callable[[], Any], root: Path = Path("."),
                 gates: Gates | None = None, namer=default_namer, clock=time.monotonic):
        self.open_device = open_device
        self.root = Path(root)
        self.gates = gates if gates is not None else Gates.load()
        self.namer = namer
        self.clock = clock
        self.dev = _Port(self)
        self.writer = GuardedWriter(self.root / "backup")  # one per session: one full backup
        self.library = Library(self.dev, self.writer, self.gates)
        self.library.stack.clock = clock
        self.library.on_write = self._library_writes
        self.link = Link.NOT_FOUND
        self.auto_save: bool | None = None
        self.editor: PatchEditor | None = None
        self.compare: Compare | None = None
        self.last_read: datetime | None = None
        self.from_cache = False   # the library shows the last read, read-only
        self._bank = 0            # bank select of the pedal's last patch change, kept between polls
        self._undone: list[Stack] = []
        # [slot, time, polls survived] of the app's own selections, oldest first
        self._echoes: deque = deque()

    # helpers ----------------------------------------------------------------------------------

    @property
    def cache_path(self) -> Path:
        return self.root / "backup" / "last-read.json"

    @property
    def has_device(self) -> bool:
        return self.dev.__dict__["target"] is not None

    def _fail(self, exc: BaseException) -> Outcome:
        if isinstance(exc, NoDevice):
            return Outcome(False, LINK_TEXT.get(self.link, "No B6 connected"))
        link = link_for(exc)
        if link is not None:
            self.link = link
            return Outcome(False, LINK_TEXT[link])
        return Outcome(False, f"Something went wrong: {exc}")

    def _no_device(self) -> Outcome:
        return Outcome(False, LINK_TEXT.get(self.link, "No B6 connected"))

    def _new_editor(self, slot: int, stored: bytes) -> PatchEditor:
        ed = PatchEditor(self.dev, slot, bytes(stored), self.writer, self.gates, self.namer)
        ed.stack.clock = self.clock
        ed.on_saved = self._editor_saved
        return ed

    def _library_writes(self, slot: int) -> None:
        """A library action writes `slot`: an open editor of that slot no longer knows what it
        holds, so its save over it refuses."""
        for ed in self._editors():
            if ed.slot == slot:
                ed.stale = True

    def _editor_saved(self, slot: int, data: bytes) -> None:
        """An editor saved `data` to `slot` (verified): the library and the cache follow."""
        if slot < len(self.library.slots):
            self.library._set(slot, data)
            if self.last_read is not None and not self.from_cache:
                self._write_cache()

    def _editors(self) -> list[PatchEditor]:
        if self.compare is not None:
            return [self.compare.current, self.compare.other]
        return [self.editor] if self.editor is not None else []

    @staticmethod
    def _lost(editor: PatchEditor) -> str | None:
        n = editor.changes
        return LOST.format(n=n, name=_name(editor)) if n > 0 else None

    def _selected_by_app(self, slot) -> None:
        self._echoes.append([slot, self.clock(), 0])
        while len(self._echoes) > ECHO_LIMIT:
            self._echoes.popleft()

    def _expire_echoes(self) -> None:
        now = self.clock()
        self._echoes = deque(e for e in self._echoes
                             if e[2] < ECHO_POLLS or now - e[1] < ECHO_MIN_SECONDS)

    def _is_echo(self, slot: int) -> bool:
        """True when this program change is the echo of a pending selection of the app: that
        entry and every one before it are consumed (selects the pedal ignored, or lost echoes).
        Anything else is a change made on the pedal: the pending echoes are dropped, since the
        pedal's state now wins."""
        for i, entry in enumerate(self._echoes):
            if entry[0] == slot:
                for _ in range(i + 1):
                    self._echoes.popleft()
                return True
        self._echoes.clear()
        return False

    # connection -------------------------------------------------------------------------------

    def _close(self) -> None:
        target = self.dev.__dict__["target"]
        self.dev.__dict__["target"] = None
        if target is not None:
            try:
                target.close()
            except Exception:
                pass

    def connect(self) -> Link:
        """Open the pedal (closing any device open before), read AUTO SAVE and the library, and
        keep the last-read cache. Returns the link state; never raises."""
        try:
            return self._connect()
        except Exception as e:  # never into the UI
            self.link = link_for(e) or Link.NOT_ANSWERING
            return self.link

    def _connect(self) -> Link:
        self._close()
        self._echoes.clear()
        try:
            target = self.open_device()
        except Exception as e:
            self.link = link_for(e) or Link.NOT_ANSWERING
            return self.link
        self.dev.__dict__["target"] = target
        self.link = Link.CONNECTED
        try:
            self.auto_save = bool(self.dev.global_info()["auto_save"])
        except Exception as e:
            self.link = link_for(e) or Link.NOT_ANSWERING
            return self.link
        try:
            refreshed = self.library.refresh().ok
        except Exception as e:  # e.g. RuntimeError: the pedal returned an error code
            self.link = link_for(e) or Link.NOT_ANSWERING
            return self.link
        if not refreshed:
            self.link = Link.NOT_ANSWERING
            return self.link
        self.from_cache = False
        self.last_read = datetime.now()
        self._write_cache()
        return self.link

    @_guarded
    def release(self) -> Outcome:
        """Send the release sequence to a stuck pedal, then connect again."""
        if not self.gates.release:
            return self.gates.refusal("release")
        if not self.has_device:
            return self._no_device()
        try:
            self.dev.release()
        except Exception:
            pass  # a stuck pedal may not take it; the reconnect tells
        if self.connect() is Link.CONNECTED:
            return Outcome(True, ANSWERS_AGAIN)
        return Outcome(False, STILL_NO_ANSWER)

    # cache ------------------------------------------------------------------------------------

    def _write_cache(self) -> None:
        data = {"read_at": self.last_read.isoformat(timespec="seconds"),
                "bank_names": list(self.library.bank_names),
                "slots": [bytes(s).hex() for s in self.library.slots]}
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_name(self.cache_path.name + ".tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self.cache_path)
        except OSError:
            pass  # the cache is a convenience; the pedal stays the truth

    def load_cache(self) -> bool:
        """Fill the library from the last read (read-only, for when the pedal is missing)."""
        try:
            data = json.loads(self.cache_path.read_text())
            slots = [bytes.fromhex(h) for h in data["slots"]]
            names = [str(n) for n in data["bank_names"]]
            read_at = datetime.fromisoformat(data["read_at"])
        except (OSError, ValueError, KeyError, TypeError):
            return False
        self.library.slots = slots
        self.library.bank_names = names
        self.last_read = read_at
        self.from_cache = True
        return True

    # patches ----------------------------------------------------------------------------------

    @_guarded
    def open(self, slot: int, unsaved: str = "ask") -> Outcome:
        """Select `slot` on the pedal and edit it. With unsaved changes, "ask" returns
        Outcome(False, "unsaved") and changes nothing; "save" saves first; "discard" drops them.
        While comparing, the hidden patch's changes can only be discarded."""
        if unsaved not in ("ask", "save", "discard"):
            return Outcome(False, f"Unknown choice for unsaved changes: {unsaved}")
        if not self.has_device:
            return self._no_device()
        hidden = self.compare.other if self.compare is not None else None
        if hidden is not None and hidden.changes > 0 and unsaved != "discard":
            if unsaved == "save":
                return Outcome(False, "The compared patch has unsaved changes: save or discard "
                                      "it first")
            return Outcome(False, "unsaved")
        old = self.editor
        if old is not None and old.changes > 0:
            if unsaved == "ask":
                return Outcome(False, "unsaved")
            if unsaved == "save":
                out = old.save()
                if not out.ok:
                    return out
        stored = bytes(self.dev.read_slot(slot))
        self.dev.select_slot(slot)
        if old is not None and unsaved == "discard":
            old.revert_to_stored()
        self.compare = None
        self.editor = self._new_editor(slot, stored)
        return Outcome(True, f"Opened {slot_label(slot)} {_name(self.editor)}")

    # A/B ----------------------------------------------------------------------------------

    @_guarded
    def compare_with(self, slot: int) -> Outcome:
        """Open `slot` as B next to the sounding patch (A), without selecting it."""
        if not self.has_device:
            return self._no_device()
        if self.editor is None:
            return Outcome(False, "Open a patch first")
        if self.compare is not None:
            return Outcome(False, "Already comparing: close the comparison first")
        if slot == self.editor.slot:
            return Outcome(False, "Choose another patch to compare with")
        b = self._new_editor(slot, bytes(self.dev.read_slot(slot)))
        self.compare = Compare(self.editor, b)
        return Outcome(True, f"Comparing with {slot_label(slot)} {_name(b)}")

    @_guarded
    def switch(self) -> Outcome:
        """Play the other patch of the compare: select it, then replay its unsaved edits."""
        c = self.compare
        if c is None:
            return Outcome(False, "Not comparing")
        if not self.has_device:
            return self._no_device()
        current, target = c.current, c.other
        if target.changes > 0 and not self.gates.replay_edits:
            return self.gates.refusal("replay_edits")
        if (self.auto_save is True and (current.changes > 0 or target.changes > 0)
                and not self.gates.auto_save_known):
            return self.gates.refusal("auto_save_known")
        self.dev.select_slot(target.slot)
        c.sounding = "B" if c.sounding == "A" else "A"
        self.editor = target
        if target.changes > 0:
            out = target.replay()
            if not out.ok:
                return out
        return Outcome(True, f"Playing {c.sounding}: {slot_label(target.slot)} {_name(target)}")

    def end_compare(self, *, discard: bool = False) -> Outcome:
        """Close the hidden patch; the sounding one stays the open patch."""
        c = self.compare
        if c is None:
            return Outcome(False, "Not comparing")
        hidden = c.other
        if hidden.changes > 0 and not discard:
            return Outcome(False, "unsaved")
        self.compare = None
        self.editor = c.current
        return Outcome(True, f"Closed {slot_label(hidden.slot)} {_name(hidden)}")

    # undo router ------------------------------------------------------------------------------

    def _stacks(self) -> list[Stack]:
        """The sounding patch's stack and the library's; never the hidden patch's."""
        return ([self.editor.stack] if self.editor is not None else []) + [self.library.stack]

    @_guarded
    def undo(self) -> Outcome | None:
        stack = newest(*self._stacks())
        if stack is None:
            return None
        if self.editor is not None and stack is self.editor.stack:
            out = self.editor.undo()
        else:
            out = stack.undo()
        if out is not None and out.ok:
            self._undone = [s for s in self._undone if s is not stack][-50:] + [stack]
        return out

    @_guarded
    def redo(self) -> Outcome | None:
        live = [s for s in self._stacks() if s.can_redo]
        if not live:
            return None
        stack = next((s for s in reversed(self._undone) if any(s is x for x in live)), live[0])
        if self.editor is not None and stack is self.editor.stack:
            out = self.editor.redo()
        else:
            out = stack.redo()
        if out is not None and out.ok and stack in self._undone and not stack.can_redo:
            self._undone = [s for s in self._undone if s is not stack]
        return out

    # events from the pedal ----------------------------------------------------------------

    def handle_events(self) -> list[str]:
        """Apply what the pedal sent on its own; returns messages for the user."""
        if not self.has_device:
            return []
        msgs: list[str] = []
        self._expire_echoes()
        try:
            sx, ch = self.dev.poll_messages()
            for m in ch:
                m = bytes(m)
                kind = m[0] & 0xF0 if m else 0
                if kind == 0xB0 and len(m) == 3 and m[1] == 0x20:
                    self._bank = m[2]
                elif kind == 0xC0 and len(m) == 2:
                    msgs += self._pedal_selected(self._bank * 4 + m[1])
            for m in sx:
                m = bytes(m)
                # 64 20 0x <entry> <param> <value:5>: done on the pedal (00) or its answer to
                # an edit (01); entry 0x64 param 0x0F is AUTO SAVE
                if m[:5] != _EDIT_MSG or len(m) < 13 or m[5] not in (0, 1):
                    continue
                entry, param, value = m[6], m[7], from7(m[8:13])
                if entry == 0x64 and param == 0x0F:
                    self.auto_save = bool(value)
                elif entry >= 0x5A:
                    continue  # position / footswitch / global echoes
                elif param == 0:
                    self._pedal_changed(entry, None, value)
                elif param >= 2:
                    self._pedal_changed(entry, param - 2, value)
        except Exception as e:
            msgs.append(self._fail(e).message)
        for entry in self._echoes:
            entry[2] += 1
        return msgs

    def _pedal_changed(self, entry: int, index: int | None, value: int) -> None:
        ed = self.editor
        if ed is None:
            return
        try:
            eff = ptcf.parse(ed.data).effects[entry]
            if eff.is_tail:
                return
            if (bool(eff.on) == bool(value)) if index is None else (eff.params[index] == value):
                return  # nothing changed (an echo of the app's own edit)
            ed.pedal_changed(entry, index, value)
        except (IndexError, ValueError):
            return

    def _pedal_selected(self, slot: int) -> list[str]:
        """The pedal changed patch (footswitch): the patch left behind lost its edits on the
        pedal, so its editor is closed with its stack; the new patch gets a fresh editor. A
        change matching the app's own pending selections is their echo. One to the sounding slot
        with no pending selection is checked: when the edit buffer no longer holds the editor's
        patch, the pedal reloaded the stored one (handled like a footswitch); when it does, or
        the buffer can't be read, the editor stays. While comparing, a change to the hidden
        patch acts like Switch when its edits can be replayed (gates allow): its editor sounds
        and its edits are put back on the pedal; otherwise they are lost."""
        if self._is_echo(slot):
            return []
        c = self.compare
        letter = c.letter_of(slot) if c is not None else None
        if self.editor is not None and self.editor.slot == slot:
            try:
                buffer = bytes(self.dev.read_edit())
            except Exception:
                return []  # unknown: keep the editor (the port updated the link)
            if _same_sound(buffer, self.editor.data):
                return []
        if c is not None and letter is not None and letter != c.sounding:
            kept = c.other
            if (kept.changes > 0 and self.gates.replay_edits
                    and (self.auto_save is not True or self.gates.auto_save_known)):
                # like Switch: the app held these edits; put them back on the pedal
                c.sounding = letter
                self.editor = kept
                return [kept.replay().message]
        try:
            stored = bytes(self.dev.read_slot(slot))
        except Exception as e:
            lost = [self._lost(ed) for ed in self._editors()]
            self.compare, self.editor = None, None
            return [m for m in lost if m] + [self._fail(e).message]
        fresh = self._new_editor(slot, stored)
        if c is not None and letter is not None:  # one of the compare's two patches
            lost = [self._lost(c.current if letter == c.sounding else c.other)]
            c.replace(letter, fresh)
            c.sounding = letter
        else:
            lost = [self._lost(ed) for ed in self._editors()]
            self.compare = None
        self.editor = fresh
        return [m for m in lost if m]
