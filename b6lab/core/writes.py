"""GuardedWriter: the one place that writes a patch to a pedal slot safely.

A full backup before the first write of the writer's life, a per-slot backup before every
write (done by `device.save_slot`), a line in `writes.jsonl`, a read-back check, and a refusal
when the slot no longer holds what the caller expects. Slots are 0-based device slots.
"""
import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from b6lab import ptcf
from b6lab.device import SaveResult, save_slot, used_bytes
from b6lab.library import backup_all


def _sha(data: bytes) -> str:
    return hashlib.sha256(used_bytes(data)).hexdigest()


def _name(data: bytes) -> str:
    try:
        return ptcf.parse(bytes(data)).name
    except Exception:
        return ""


class GuardedWriter:
    def __init__(self, backup_root: Path, write_pause: float = 1.0):
        self.backup_root = Path(backup_root)
        self.overwritten = self.backup_root / "overwritten"
        self.log_path = self.backup_root / "writes.jsonl"
        self.write_pause = write_pause
        self.full_backup_path: Optional[Path] = None

    def full_backup(self, dev) -> Path:
        """Back up the whole pedal now (does not count as the once-per-writer backup)."""
        return Path(backup_all(dev, self.backup_root).path)

    def _log(self, action, slot, old, new, backup_file, verified, message):
        line = {"time": datetime.now().isoformat(timespec="seconds"), "action": action,
                "slot": slot, "old_name": _name(old), "new_name": _name(new),
                "old_sha256": _sha(old), "new_sha256": _sha(new),
                "backup_file": str(backup_file) if backup_file else None,
                "verified": bool(verified), "message": message}
        self.backup_root.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "a") as f:
            f.write(json.dumps(line) + "\n")

    def write(self, dev, action: str, slot: int, data: bytes, expected: Optional[bytes],
              select: bool, *, store_edit: bool = False,
              log_slot: Optional[int] = None) -> SaveResult:
        """Write `data` to 0-based `slot`. `expected` is what the slot must still hold (None:
        no check); when it differs nothing is written and the result is
        SaveResult(False, "changed")."""
        if self.full_backup_path is None:
            self.full_backup_path = self.full_backup(dev)
        res = save_slot(dev, slot, data, expected, self.overwritten, lambda: False,
                        store_edit=store_edit, select=select)
        if res.message == "cancelled" and not res.ok:
            return SaveResult(False, "changed", res.backup_file, res.error)
        if res.backup_file is not None:  # the pedal was (or may have been) written
            old = expected if expected is not None else res.backup_file.read_bytes()
            self._log(action, slot + 1 if log_slot is None else log_slot, old, data,
                      res.backup_file, res.ok, res.message)
            time.sleep(self.write_pause)
        return res
