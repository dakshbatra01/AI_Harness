"""Shared task contract, mutable controller state, and budgets."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from harness.repo.anchors import Anchors

# Phases (controller-owned). ORIENT is deterministic; BASELINE is captured lazily per test
# command (base runs are cached), so the model never waits on a full-suite baseline.
ORIENT, LOCATE, REPRODUCE, EDIT, CHECK, FINAL_VERIFY, RECOVER, DONE = (
    "ORIENT", "LOCATE", "REPRODUCE", "EDIT", "CHECK", "FINAL_VERIFY", "RECOVER", "DONE",
)
DIRECT, LIGHT_PLAN, STRUCTURED = "DIRECT", "LIGHT_PLAN", "STRUCTURED"


@dataclass(frozen=True)
class TaskContract:
    task_id: str
    original_issue: str  # verbatim, immutable
    repo_root: str
    base_revision: str
    anchors: Anchors

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "repo_root": self.repo_root,
            "base_revision": self.base_revision,
            "anchors": self.anchors.to_dict(),
            "issue_chars": len(self.original_issue),
        }


@dataclass
class FailureRecord:
    attempt: int
    strategy: str
    hypothesis: str
    predicted: str
    actual: str
    signature: str
    failure_class: str
    patch_hash: str
    forbidden_repeat: str = ""

    def render(self) -> str:
        return (
            f"- attempt {self.attempt} [{self.failure_class}] hypothesis: {self.hypothesis or '(not recorded)'}\n"
            f"  strategy: {self.strategy or '(n/a)'}\n  outcome: {self.actual[:700]}\n"
            + (f"  do NOT repeat: {self.forbidden_repeat}\n" if self.forbidden_repeat else "")
        )


@dataclass
class Plan:
    hypothesis: str = ""
    plan: str = ""
    notes: str = ""
    acceptance: str = ""

    def render(self) -> str:
        parts = []
        if self.hypothesis:
            parts.append(f"hypothesis: {self.hypothesis}")
        if self.acceptance:
            parts.append(f"acceptance: {self.acceptance}")
        if self.plan:
            parts.append(f"plan:\n{self.plan}")
        if self.notes:
            parts.append(f"notes:\n{self.notes}")
        return "\n".join(parts)


@dataclass
class TaskState:
    phase: str = ORIENT
    route: str = DIRECT
    attempt: int = 1
    step: int = 0
    attempt_step: int = 0
    steps_in_phase: int = 0
    edits_since_check: int = 0
    plan: Plan = field(default_factory=Plan)
    failures: list = field(default_factory=list)
    verify_rounds: int = 0
    failure_signatures: dict = field(default_factory=dict)
    stuck_events: int = 0
    tool_errors: int = 0
    observed_tests: list = field(default_factory=list)  # run_tests commands issued by the model
    observed_scripts: list = field(default_factory=list)  # scratch scripts run via run_command
    candidates: list = field(default_factory=list)  # (tree, commit, report)
    last_report: object = None

    def set_phase(self, phase: str) -> None:
        if phase != self.phase:
            self.phase = phase
            self.steps_in_phase = 0

    def snapshot(self) -> dict:
        d = asdict(self)
        d.pop("last_report", None)
        d.pop("candidates", None)
        return d


@dataclass
class Budget:
    max_steps: int
    max_model_calls: int
    max_total_tokens: int
    max_wall_s: float
    reserve: float
    started: float = field(default_factory=time.time)
    model_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    steps: int = 0

    def elapsed(self) -> float:
        return time.time() - self.started

    def work_fraction_used(self) -> float:
        return max(
            self.steps / max(1, self.max_steps),
            self.model_calls / max(1, self.max_model_calls),
            (self.tokens_in + self.tokens_out) / max(1, self.max_total_tokens),
            self.elapsed() / max(1.0, self.max_wall_s),
        )

    def work_exhausted(self) -> bool:
        """True once only the verification reserve remains."""
        return self.work_fraction_used() >= 1.0 - self.reserve

    def hard_exhausted(self) -> bool:
        return self.work_fraction_used() >= 1.0

    def remaining_steps(self) -> int:
        work_steps = int(self.max_steps * (1.0 - self.reserve))
        by_time = (self.max_wall_s * (1.0 - self.reserve) - self.elapsed()) / 20.0  # ~20 s/step
        return max(0, min(work_steps - self.steps, self.max_model_calls - self.model_calls, int(by_time)))

    def to_dict(self) -> dict:
        return {
            "model_calls": self.model_calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "tokens_cached": self.tokens_cached,
            "steps": self.steps,
            "wall_s": round(self.elapsed(), 1),
        }
