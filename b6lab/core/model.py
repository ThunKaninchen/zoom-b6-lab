"""Shared small types of the core: link state, edit state, outcomes, feature gates."""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

DEFAULT_GATES = Path(__file__).resolve().parent.parent / "data" / "gates.json"


class Link(Enum):
    CONNECTED = "connected"
    BUSY = "busy"
    NOT_FOUND = "not_found"
    NOT_ANSWERING = "not_answering"


class EditState(Enum):
    STORED = "stored"
    EDITED = "edited"
    SENDING = "sending"
    FAILED = "failed"


@dataclass(frozen=True)
class Outcome:
    ok: bool
    message: str
    backups: tuple[Path, ...] = ()


REASONS = {
    "replay_edits": "Not yet tested on the pedal: restoring unsaved changes after switching patches",
    "overwrite_slots": "Not yet tested on the pedal: writing over a memory that holds a patch",
    "release": "Not yet tested on the pedal: the release sequence",
    "write_bank_names": "Not yet tested on the pedal: renaming banks",
    "auto_save_known": "Auto save is on, and what it stores is not yet tested: switch it off on the pedal to compare edited patches",
}


@dataclass(frozen=True)
class Gates:
    replay_edits: bool = False
    overwrite_slots: bool = False
    release: bool = False
    write_bank_names: bool = False
    auto_save_known: bool = False

    @classmethod
    def load(cls, path: Path = DEFAULT_GATES) -> "Gates":
        try:
            data = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        return cls(**{k: data.get(k) is True for k in REASONS})

    def refusal(self, name: str) -> Outcome:
        return Outcome(False, REASONS[name])
