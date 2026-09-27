"""Lightweight SQLite memory (stdlib only).

  facts     - semantic memory: ONLY tool/test-observed statements, with provenance + file hash
  episodes  - episodic memory: one FailureRecord per failed strategy (written after execution signals)
  notes     - working notes/hypotheses from the model (never promoted to facts automatically)

Cross-task memory is intentionally off: each run gets a fresh database inside its run dir."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from harness.util import REDACT


class MemoryStore:
    def __init__(self, path: Path | None) -> None:
        self.db = sqlite3.connect(str(path) if path else ":memory:")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS facts (id INTEGER PRIMARY KEY, statement TEXT UNIQUE, source_id TEXT, "
            "source_kind TEXT, file_hash TEXT, confidence REAL, status TEXT DEFAULT 'valid', created REAL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS episodes (id INTEGER PRIMARY KEY, attempt INTEGER, strategy TEXT, hypothesis TEXT, "
            "predicted TEXT, actual TEXT, signature TEXT, failure_class TEXT, patch_hash TEXT, evidence TEXT, created REAL)"
        )
        self.db.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, kind TEXT, body TEXT, created REAL)")
        try:
            self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(statement)")
            self.fts = True
        except sqlite3.OperationalError:
            self.fts = False
        self.db.commit()

    def add_fact(self, statement: str, source_id: str, source_kind: str, file_hash: str = "", confidence: float = 1.0) -> None:
        if source_kind not in ("tool", "test", "repo"):
            raise ValueError("facts must come from tool/test/repo observations")
        statement = REDACT(statement)[:500]
        cur = self.db.execute(
            "INSERT OR IGNORE INTO facts (statement, source_id, source_kind, file_hash, confidence, created) VALUES (?,?,?,?,?,?)",
            (statement, source_id, source_kind, file_hash, confidence, time.time()),
        )
        if cur.rowcount and self.fts:
            self.db.execute("INSERT INTO facts_fts (statement) VALUES (?)", (statement,))
        self.db.commit()

    def invalidate_file(self, file_hash: str) -> None:
        if file_hash:
            self.db.execute("UPDATE facts SET status='stale' WHERE file_hash=?", (file_hash,))
            self.db.commit()

    def facts(self, limit: int = 20) -> list[str]:
        rows = self.db.execute("SELECT statement, source_id FROM facts WHERE status='valid' ORDER BY id DESC LIMIT ?", (limit,))
        return [f"{s} [src {sid}]" for s, sid in rows]

    def search_facts(self, query: str, limit: int = 5) -> list[str]:
        if self.fts:
            try:
                rows = self.db.execute("SELECT statement FROM facts_fts WHERE facts_fts MATCH ? LIMIT ?", (query, limit))
                return [r[0] for r in rows]
            except sqlite3.OperationalError:
                pass
        rows = self.db.execute("SELECT statement FROM facts WHERE statement LIKE ? LIMIT ?", (f"%{query}%", limit))
        return [r[0] for r in rows]

    def add_episode(self, attempt: int, strategy: str, hypothesis: str, predicted: str, actual: str, signature: str,
                    failure_class: str, patch_hash: str, evidence: dict | None = None) -> None:
        self.db.execute(
            "INSERT INTO episodes (attempt, strategy, hypothesis, predicted, actual, signature, failure_class, patch_hash, evidence, created) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (attempt, REDACT(strategy)[:600], REDACT(hypothesis)[:600], REDACT(predicted)[:300], REDACT(actual)[:900],
             signature[:300], failure_class, patch_hash, json.dumps(evidence or {})[:2000], time.time()),
        )
        self.db.commit()

    def episodes(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT attempt, strategy, hypothesis, predicted, actual, failure_class, patch_hash FROM episodes ORDER BY id"
        )
        keys = ("attempt", "strategy", "hypothesis", "predicted", "actual", "failure_class", "patch_hash")
        return [dict(zip(keys, r)) for r in rows]

    def add_note(self, kind: str, body: str) -> None:
        self.db.execute("INSERT INTO notes (kind, body, created) VALUES (?,?,?)", (kind, REDACT(body)[:4000], time.time()))
        self.db.commit()

    def close(self) -> None:
        try:
            self.db.close()
        except sqlite3.Error:
            pass
