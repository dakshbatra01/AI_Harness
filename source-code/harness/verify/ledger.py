"""Append-only evidence ledger. Every item is bound to a candidate hash; items for any other
hash are STALE for the current candidate by construction."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from harness.util import REDACT

SUPPORTED, CONTRADICTED, UNKNOWN, STALE = "SUPPORTED", "CONTRADICTED", "UNKNOWN", "STALE"


@dataclass
class EvidenceItem:
    evidence_id: str
    claim_id: str
    status: str
    candidate_hash: str
    environment_hash: str
    test_manifest_hash: str = ""
    commands: list[str] = field(default_factory=list)
    test_ids: list[str] = field(default_factory=list)
    detail: str = ""
    log_ids: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


class Ledger:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.items: list[EvidenceItem] = []

    def add(self, item: EvidenceItem) -> EvidenceItem:
        self.items.append(item)
        if self.path:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(REDACT(json.dumps(asdict(item), default=str)) + "\n")
        return item

    def current(self, candidate_hash: str) -> list[EvidenceItem]:
        return [i for i in self.items if i.candidate_hash == candidate_hash]

    def status_of(self, claim_id: str, candidate_hash: str) -> str:
        items = [i for i in self.items if i.claim_id == claim_id]
        if not items:
            return UNKNOWN
        cur = [i for i in items if i.candidate_hash == candidate_hash]
        return cur[-1].status if cur else STALE
