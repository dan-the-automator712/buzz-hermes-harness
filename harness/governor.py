"""
governor.py — the turn budget + success-criteria gate.

Hard rule from the operator: no task runs more than `max_turns` (default 5)
before it is checked. After each turn the governor evaluates the task's success
criteria against the worker's latest output and returns a decision:

    CONTINUE  — criteria not yet met, budget remains -> run another turn
    COMPLETE  — criteria satisfied -> stop, report success
    CHECKPOINT — budget exhausted without success -> stop and hand back to the
                 orchestrator to decide whether continuing is worthwhile

Success criteria are declarative and checked cheaply first (deterministic
string/regex/JSON probes). An optional `judge` callable (e.g. a Hermes/DeepSeek
call) can be supplied for fuzzy "is this good enough?" evaluation, but it is only
consulted when the deterministic probes are inconclusive — so we never burn a
model call we don't need.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional


class Decision(str, Enum):
    CONTINUE = "continue"
    COMPLETE = "complete"
    CHECKPOINT = "checkpoint"   # budget spent, needs human/orchestrator review


@dataclass
class Criterion:
    """One success test. `kind` selects how `value` is interpreted."""
    kind: str          # "contains" | "regex" | "json_path_eq" | "predicate"
    value: Any
    # for json_path_eq: dotted path into a parsed-JSON result, compared == expected
    path: Optional[str] = None
    expected: Any = None

    def check(self, output: str, parsed: Optional[dict]) -> Optional[bool]:
        """Return True/False, or None if this criterion can't judge (inconclusive)."""
        if self.kind == "contains":
            return str(self.value) in output
        if self.kind == "regex":
            return re.search(str(self.value), output, re.MULTILINE | re.DOTALL) is not None
        if self.kind == "json_path_eq":
            if parsed is None or not self.path:
                return None
            cur: Any = parsed
            for part in self.path.split("."):
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                else:
                    return False
            return cur == self.expected
        if self.kind == "predicate":
            # value is a callable(output, parsed) -> bool
            return bool(self.value(output, parsed))
        return None


@dataclass
class TaskState:
    task_id: str
    goal: str
    criteria: list[Criterion]
    max_turns: int = 5
    require_all: bool = True         # all criteria vs. any criterion
    turn: int = 0
    history: list[str] = field(default_factory=list)


@dataclass
class Governor:
    # optional fuzzy judge: (goal, output) -> bool ; consulted only when needed
    judge: Optional[Callable[[str, str], bool]] = None

    def evaluate(self, state: TaskState, output: str, parsed: Optional[dict]) -> Decision:
        """Called once per completed turn. Advances turn count and returns a Decision."""
        state.turn += 1
        state.history.append(output)

        if self._criteria_met(state, output, parsed):
            return Decision.COMPLETE

        # Not yet satisfied. Do we still have budget?
        if state.turn >= state.max_turns:
            return Decision.CHECKPOINT
        return Decision.CONTINUE

    def _criteria_met(self, state: TaskState, output: str, parsed: Optional[dict]) -> bool:
        if not state.criteria:
            # No declarative criteria -> fall back to the fuzzy judge if present,
            # otherwise never auto-complete (force a checkpoint at budget).
            return bool(self.judge and self.judge(state.goal, output))

        results = [c.check(output, parsed) for c in state.criteria]
        decisive = [r for r in results if r is not None]

        if state.require_all:
            if any(r is False for r in results):
                return False
            if decisive and all(r is True for r in decisive) and len(decisive) == len(results):
                return True
        else:  # require any
            if any(r is True for r in decisive):
                return True

        # Inconclusive (some criteria couldn't judge) -> optionally ask the judge.
        if self.judge is not None:
            return self.judge(state.goal, output)
        return False


def criteria_from_spec(spec: list[dict]) -> list[Criterion]:
    """Build Criterion objects from the JSON spec used in jobs.json."""
    out: list[Criterion] = []
    for c in spec or []:
        out.append(
            Criterion(
                kind=c["kind"],
                value=c.get("value"),
                path=c.get("path"),
                expected=c.get("expected"),
            )
        )
    return out
