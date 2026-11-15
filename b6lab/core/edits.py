"""One class per kind of change to a patch: live send, offline apply, inverse, label."""
from __future__ import annotations

from collections.abc import Callable, Hashable
from dataclasses import dataclass
from typing import Protocol

from b6lab import ptcf
from b6lab import live

# (fx_id, knob index) -> (knob name, raw value -> display text)
Namer = Callable[[int, int], tuple[str, Callable[[int], str]]]


def effect_name(fx_id: int) -> str:
    """The effect's name, or "Effect" when its files are not available."""
    try:
        from b6lab import effects
        info = effects.lookup(fx_id)
        if info is not None and info.name:
            return info.name
    except Exception:
        pass
    return "Effect" if not fx_id else f"Effect {fx_id:#x}"


def default_namer(fx_id: int, index: int) -> tuple[str, Callable[[int], str]]:
    fallback = (f"Knob {index + 1}", str)
    try:
        from b6lab import effects, knobs
        info = effects.lookup(fx_id)
        if info is None or not 0 <= index < len(info.params):
            return fallback
        param = info.params[index]

        def show(raw, _p=param):
            try:
                v = knobs.display_value(_p, raw)
            except Exception:
                return str(raw)
            return str(v["raw"]) if isinstance(v, dict) else str(v)
        return param.name or fallback[0], show
    except Exception:
        return fallback


def _edited(data: bytes, fn) -> bytes:
    p = ptcf.parse(data)
    fn(p)
    return ptcf.encode(p)


class Edit(Protocol):
    merge_key: Hashable | None

    def apply(self, data: bytes) -> bytes: ...
    def send(self, dev, data: bytes) -> bytes: ...
    def inverse(self) -> "Edit | tuple[Edit, ...]": ...
    def label(self, namer: Namer) -> str: ...


class _ChainEdit:
    """Edits the pedal performs itself: send the command, return its edit buffer."""
    merge_key = None

    def send(self, dev, data):
        self._command(dev)
        return bytes(dev.read_edit())


@dataclass(frozen=True)
class KnobEdit:
    entry: int
    index: int
    old: int
    new: int
    fx_id: int = 0  # for the label only

    @property
    def merge_key(self):
        return ("knob", self.entry, self.index)

    def apply(self, data):
        return _edited(data, lambda p: ptcf.set_param(p, self.entry, self.index, self.new))

    def send(self, dev, data):
        live.send_param(dev, ptcf.parse(data), self.entry, self.index, self.new)
        return bytes(dev.read_edit())

    def inverse(self):
        return KnobEdit(self.entry, self.index, self.new, self.old, self.fx_id)

    def label(self, namer):
        name, show = namer(self.fx_id, self.index)
        return f"{name} {show(self.old)} → {show(self.new)}"


@dataclass(frozen=True)
class OnEdit:
    entry: int
    old: bool
    new: bool
    fx_id: int = 0  # for the label only
    merge_key = None

    def apply(self, data):
        return _edited(data, lambda p: ptcf.set_on(p, self.entry, self.new))

    def send(self, dev, data):
        live.set_effect_on(dev, self.entry, self.new)
        return bytes(dev.read_edit())

    def inverse(self):
        return OnEdit(self.entry, self.new, self.old, self.fx_id)

    def label(self, namer):
        return f"{effect_name(self.fx_id)} {'on' if self.new else 'off'}"


@dataclass(frozen=True)
class TypeEdit(_ChainEdit):
    entry: int
    old_fx: int
    new_fx: int
    wide: bool = False  # new effect is double width: apply adds its tail entry (the pedal does it itself live)

    def _command(self, dev):
        dev.change_type(self.entry, self.new_fx)

    def apply(self, data):
        def go(p):
            nxt = self.entry + 1
            if self.wide and not (nxt < len(p.effects) and p.effects[nxt].is_tail):
                if ptcf.insert_entry(p, nxt):
                    ptcf.set_type(p, nxt, 1)
            ptcf.set_type(p, self.entry, self.new_fx)
        return _edited(data, go)

    def inverse(self):
        return TypeEdit(self.entry, self.new_fx, self.old_fx)

    def label(self, namer):
        return f"{effect_name(self.old_fx)} → {effect_name(self.new_fx)}"


@dataclass(frozen=True)
class MoveEdit(_ChainEdit):
    src: int
    dst: int
    fx_id: int = 0  # for the label only

    def _command(self, dev):
        dev.move_effect(self.src, self.dst)

    def apply(self, data):
        return _edited(data, lambda p: ptcf.move_entry(p, self.src, self.dst))

    def inverse(self):
        return MoveEdit(self.dst, self.src, self.fx_id)

    def label(self, namer):
        return f"Move {effect_name(self.fx_id)}"


@dataclass(frozen=True)
class InsertEdit(_ChainEdit):
    position: int

    def _command(self, dev):
        dev.insert_effect(self.position)

    def apply(self, data):
        return _edited(data, lambda p: ptcf.insert_entry(p, self.position))

    def inverse(self):
        return DeleteEdit(self.position, ptcf.Effect(
            slot=self.position, fx_id=0, on=True, params=[0] * len(ptcf.PARAM_WIDTHS),
            is_tail=False, raw=1))

    def label(self, namer):
        return "Add effect"


@dataclass(frozen=True)
class DeleteEdit(_ChainEdit):
    position: int
    removed: ptcf.Effect
    tail: bool = False  # the removed head had a tail entry behind it (double-width effect)

    @classmethod
    def from_patch(cls, patch, position):
        effects = patch.effects
        tail = position + 1 < len(effects) and effects[position + 1].is_tail
        return cls(position, effects[position], tail)

    def _command(self, dev):
        dev.delete_effect(self.position)

    def apply(self, data):
        return _edited(data, lambda p: ptcf.delete_entry(p, self.position))

    def inverse(self):
        r, pos = self.removed, self.position
        # the pedal gives a new effect the knob values that type last had on it (see
        # B6.change_type), so every knob is set back, zeros included
        out: list = [InsertEdit(pos), TypeEdit(pos, 0, r.fx_id, self.tail),
                     RestoreKnobsEdit(pos, tuple(r.params), r.fx_id)]
        if not r.on:
            out.append(OnEdit(pos, True, False, r.fx_id))
        return tuple(out)

    def label(self, namer):
        return f"Remove {effect_name(self.removed.fx_id)}"


@dataclass(frozen=True)
class RestoreKnobsEdit:
    """Set every knob of the effect at `entry` to `params`: live, only the knobs whose value in
    the pedal's edit buffer differs are sent. Part of the inverse of a DeleteEdit."""
    entry: int
    params: tuple
    fx_id: int = 0  # for the label only
    merge_key = None

    def apply(self, data):
        def go(p):
            for i, v in enumerate(self.params):
                ptcf.set_param(p, self.entry, i, v)
        return _edited(data, go)

    def send(self, dev, data):
        buf = ptcf.parse(bytes(dev.read_edit()))
        have = buf.effects[self.entry].params
        for i, (a, b) in enumerate(zip(have, self.params)):
            if a != b:
                live.send_param(dev, buf, self.entry, i, b)
        return bytes(dev.read_edit())

    def inverse(self):
        return self  # only ever sent as part of an inverse; restoring twice is the same

    def label(self, namer):
        return f"{effect_name(self.fx_id)} knobs"


@dataclass(frozen=True)
class NameEdit:
    old: str
    new: str
    merge_key = None

    def apply(self, data):
        return _edited(data, lambda p: ptcf.set_name(p, self.new))

    def send(self, dev, data):
        return self.apply(data)

    def inverse(self):
        return NameEdit(self.new, self.old)

    def label(self, namer):
        return f"Name {self.old} → {self.new}"


@dataclass(frozen=True)
class LevelEdit:
    old: int
    new: int
    merge_key = None

    def apply(self, data):
        return _edited(data, lambda p: ptcf.set_level(p, self.new))

    def send(self, dev, data):
        return self.apply(data)

    def inverse(self):
        return LevelEdit(self.new, self.old)

    def label(self, namer):
        return f"Level {self.old} → {self.new}"
