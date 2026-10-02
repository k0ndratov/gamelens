"""Counts API tokens and turns them into an estimated cost.

The numbers come from the usage block Claude returns with every answer, so the
token counts are exact; the dollar figure is an estimate, because it depends on
the per-million-token prices in config.yaml.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path


class UsageTracker:
    def __init__(self, path: Path | None, price_in: float = 3.0,
                 price_out: float = 15.0):
        self.path = path
        self.price_in = float(price_in)
        self.price_out = float(price_out)
        self._lock = threading.Lock()
        self.session = {"questions": 0, "input": 0, "output": 0}
        self.total = {"questions": 0, "input": 0, "output": 0}
        self._load()

    def _load(self) -> None:
        if not self.path:
            return
        try:
            data = json.loads(Path(self.path).read_text(encoding="utf-8"))
            for key in self.total:
                self.total[key] = int(data.get("total", {}).get(key, 0))
        except (OSError, ValueError, TypeError):
            pass

    def _save(self) -> None:
        if not self.path:
            return
        try:
            Path(self.path).write_text(
                json.dumps({"total": self.total}, indent=2), encoding="utf-8")
        except OSError:
            pass

    def cost(self, bucket: dict) -> float:
        return (bucket["input"] / 1e6 * self.price_in
                + bucket["output"] / 1e6 * self.price_out)

    def record(self, input_tokens: int, output_tokens: int) -> None:
        with self._lock:
            for bucket in (self.session, self.total):
                bucket["questions"] += 1
                bucket["input"] += int(input_tokens or 0)
                bucket["output"] += int(output_tokens or 0)
            self._save()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "session": {**self.session, "cost": self.cost(self.session)},
                "total": {**self.total, "cost": self.cost(self.total)},
            }
