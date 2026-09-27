"""Stuck detection with action/observation fingerprints (thresholds: repeat 4, repeated error 3, no-action 3, ping-pong 6) plus
edit/revert oscillation. The detector only reports; the controller decides the escalation."""
from __future__ import annotations

import json
from dataclasses import dataclass

from harness.util import normalize_signature, short_hash


def action_fp(tool: str, args: dict) -> str:
    norm = {k: (v.strip() if isinstance(v, str) else v) for k, v in sorted(args.items())}
    return tool + ":" + short_hash(json.dumps(norm, sort_keys=True, default=str), 16)


def obs_fp(output: str, tree: str = "") -> str:
    return short_hash(normalize_signature(output[:4000], 4000) + "|" + tree, 16)


@dataclass
class StuckSignal:
    kind: str
    detail: str


class LoopGuard:
    def __init__(self, cfg: dict) -> None:
        self.same_obs = int(cfg.get("same_action_same_obs", 4))
        self.same_err = int(cfg.get("same_action_error", 3))
        self.no_action = int(cfg.get("no_action_msgs", 3))
        self.ping_pong = int(cfg.get("ping_pong", 6))
        self.hist: list[tuple[str, str, bool]] = []
        self.no_action_run = 0
        self.trees: list[str] = []

    def reset(self) -> None:
        self.hist.clear()
        self.no_action_run = 0
        self.trees.clear()

    def record_no_action(self) -> StuckSignal | None:
        self.no_action_run += 1
        if self.no_action_run >= self.no_action:
            self.no_action_run = 0
            return StuckSignal("no_action", f"{self.no_action} consecutive replies without a valid tool call")
        return None

    def record(self, afp: str, ofp: str, is_error: bool) -> StuckSignal | None:
        self.no_action_run = 0
        self.hist.append((afp, ofp, is_error))
        h = self.hist
        n = self.same_obs
        if len(h) >= n and len({(a, o) for a, o, _ in h[-n:]}) == 1:
            self.hist = h[-1:]
            return StuckSignal("repeat", f"the same action produced the same result {n} times in a row")
        n = self.same_err
        if len(h) >= n and all(e for _, _, e in h[-n:]) and len({a for a, _, _ in h[-n:]}) == 1:
            self.hist = h[-1:]
            return StuckSignal("repeat_error", f"the same action failed {n} times in a row")
        n = self.ping_pong
        if len(h) >= n:
            tail = [a for a, _, _ in h[-n:]]
            a, b = tail[0], tail[1]
            if a != b and all(tail[i] == (a if i % 2 == 0 else b) for i in range(n)):
                self.hist = h[-1:]
                return StuckSignal("ping_pong", "alternating between the same two actions without progress")
        return None

    def record_tree(self, tree: str) -> StuckSignal | None:
        """Detect edit/revert oscillation: returning to an earlier non-adjacent version twice."""
        if self.trees and self.trees[-1] == tree:
            return None
        revisits = sum(1 for i, t in enumerate(self.trees[:-1]) if t == tree)
        self.trees.append(tree)
        if revisits >= 2:
            return StuckSignal("oscillation", "edits keep returning the code to a version already tried")
        return None
