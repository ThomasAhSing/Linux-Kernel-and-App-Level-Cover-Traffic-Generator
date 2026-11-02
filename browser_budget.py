"""Persistent daily HTTP allowance for scheduled browser cycles.

The caller holds the shared browser flock for the entire activation. Reserve
before sending, so crashes can underuse the allowance but cannot refund it.
"""
import datetime as dt
import json
import os
from pathlib import Path
import tempfile


class DailyBudget:
    def __init__(self, path, limit, today=None):
        self.path = Path(path)
        self.limit = limit
        self.today = today or (lambda: dt.date.today().isoformat())
        self.counts = json.loads(self.path.read_text()) if self.path.exists() else {}
        if (not isinstance(self.counts, dict) or any(
            not isinstance(day, str) or dt.date.fromisoformat(day).isoformat() != day
            or type(count) is not int or count < 0 for day, count in self.counts.items()
        )):
            raise ValueError("invalid persistent daily browser budget")

    def available(self):
        return self.counts.get(self.today(), 0) < self.limit

    def reserve(self):
        day = self.today()
        if not self.available():
            return False
        updated = dict(self.counts)
        updated[day] = updated.get(day, 0) + 1
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent,
                                             prefix=".budget-", delete=False) as f:
                temporary = Path(f.name)
                json.dump(updated, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, self.path)
            if os.name == "posix":
                fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            self.counts = updated
            return True
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
