"""Structured JSONL telemetry + human progress output. Secrets are always redacted."""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

from harness.util import REDACT


class Telemetry:
    def __init__(self, run_dir: Path | None, verbose: bool = True, stream=None) -> None:
        self.run_dir = run_dir
        self.verbose = verbose
        self.stream = stream or sys.stderr
        self._lock = threading.Lock()
        self._fh = None
        self.t0 = time.time()
        self.counters: dict[str, float] = {}
        if run_dir is not None:
            run_dir.mkdir(parents=True, exist_ok=True)
            self._fh = open(run_dir / "trace.jsonl", "a", encoding="utf-8")

    def event(self, event_name: str, **fields) -> None:
        rec = {"t": round(time.time() - self.t0, 3), "event": event_name, **fields}
        line = REDACT(json.dumps(rec, ensure_ascii=False, default=str))
        with self._lock:
            if self._fh:
                self._fh.write(line + "\n")
                self._fh.flush()

    def count(self, name: str, inc: float = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + inc

    def say(self, msg: str) -> None:
        if self.verbose:
            elapsed = time.time() - self.t0
            print(REDACT(f"[{elapsed:7.1f}s] {msg}"), file=self.stream, flush=True)

    def close(self) -> None:
        with self._lock:
            if self._fh:
                self._fh.close()
                self._fh = None


NULL_TELEMETRY = Telemetry(None, verbose=False)
