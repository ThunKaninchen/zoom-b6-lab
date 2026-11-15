"""Compare: A/B between two open, editable patches; one of them sounds on the pedal."""
from __future__ import annotations

from b6lab.core.patch_editor import PatchEditor


class Compare:
    def __init__(self, a: PatchEditor, b: PatchEditor):
        self.a = a
        self.b = b
        self.sounding = "A"

    @property
    def current(self) -> PatchEditor:
        """The editor of the patch the pedal plays now."""
        return self.a if self.sounding == "A" else self.b

    @property
    def other(self) -> PatchEditor:
        """The hidden one: its edits are kept here, not on the pedal."""
        return self.b if self.sounding == "A" else self.a

    def letter_of(self, slot: int) -> str | None:
        """"A" or "B" for the editor on `slot`, else None."""
        if self.a.slot == slot:
            return "A"
        if self.b.slot == slot:
            return "B"
        return None

    def replace(self, letter: str, editor: PatchEditor) -> None:
        if letter == "A":
            self.a = editor
        else:
            self.b = editor
