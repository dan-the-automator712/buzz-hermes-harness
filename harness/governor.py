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
from pathlib import Path
from typing import Any, Callable, Optional


class Decision(str, Enum):
    CONTINUE = "continue"
    COMPLETE = "complete"
    CHECKPOINT = "checkpoint"   # budget spent, needs human/orchestrator review


@dataclass
class Criterion:
    """
    One success test. `kind` selects how it is evaluated.

    Text criteria grade the model's REPLY (can be faked by narration):
        contains | regex | json_path_eq | predicate
    Ground-truth criteria grade the real FILESYSTEM in the task's workdir — a model
    that only *says* it did the work fails these:
        file_exists   -> value: relative path that must exist (and be non-empty)
        file_contains -> path: file, value: substring that must be in it
        file_regex    -> path: file, value: regex that must match its contents
    """
    kind: str
    value: Any
    # for json_path_eq: dotted path into parsed JSON, compared == expected
    # for file_contains/file_regex: `path` is the file (relative to workdir)
    path: Optional[str] = None
    expected: Any = None

    def check(self, output: str, parsed: Optional[dict],
              workdir: Optional[str] = None) -> Optional[bool]:
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
            return bool(self.value(output, parsed))

        # --- ground-truth (filesystem) checks ---
        if self.kind in ("file_exists", "file_contains", "file_regex"):
            rel = self.path if self.kind != "file_exists" else self.value
            base = Path(workdir) if workdir else Path.cwd()
            target = (base / str(rel)) if rel else base
            if self.kind == "file_exists":
                try:
                    return target.is_file() and target.stat().st_size > 0
                except OSError:
                    return False
            if not target.is_file():
                return False
            try:
                text = target.read_text(errors="replace")
            except OSError:
                return False
            if self.kind == "file_contains":
                return str(self.value) in text
            return re.search(str(self.value), text, re.MULTILINE | re.DOTALL) is not None
        return None


@dataclass
class TaskState:
    task_id: str
    goal: str
    criteria: list[Criterion]
    max_turns: int = 5
    require_all: bool = True         # all criteria vs. any criterion
    workdir: Optional[str] = None    # base dir for file_* ground-truth checks
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

        results = [c.check(output, parsed, state.workdir) for c in state.criteria]
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
