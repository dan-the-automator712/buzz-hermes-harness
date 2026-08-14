"""
worker.py — a governed Hermes worker bound to one task.

Lifecycle for a single task:

    1. Verify the signed task event off the Buzz relay (author + freshness + Schnorr).
    2. Run turns on Hermes/deepseek, at most `max_turns` (default 5).
    3. After every turn, post a signed `progress` event and ask the governor what
       to do next.
    4. Stop on COMPLETE (post signed `result`) or CHECKPOINT (post signed
       `checkpoint` for orchestrator review). Never exceed the turn budget.

The worker signs everything it publishes with its *own* Nostr identity, so the
audit trail on the relay distinguishes the queen's assignments from each worker's
output by keypair — exactly Buzz's model (agents are members, not bots).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from buzz_bus import BuzzBus
from governor import Decision, Governor, TaskState, criteria_from_spec
from hermes_adapter import HermesAdapter


@dataclass
class WorkerResult:
    task_id: str
    status: str          # "done" | "needs_review" | "failed"
    turns_used: int
    final_output: str


def _build_prompt(goal: str, criteria_spec: list[dict], history: list[str], turn: int, max_turns: int) -> str:
    lines = [
        f"# Task (turn {turn}/{max_turns})",
        goal.strip(),
        "",
        "## Success criteria (you are done only when ALL are satisfied)",
        json.dumps(criteria_spec, indent=2) if criteria_spec else "(judged qualitatively)",
    ]
    if history:
        lines += ["", "## Work so far (previous turns)"]
        for i, h in enumerate(history, 1):
            lines.append(f"--- turn {i} ---\n{h}")
        lines += ["", "Continue from where the previous turn left off. Do not repeat completed work."]
    lines += [
        "",
        "Produce the next increment of work now. If the success criteria are fully met, "
        "state that explicitly and end with `STATUS: done`.",
    ]
    return "\n".join(lines)


def _parse_output(output: str) -> tuple[str, Optional[dict]]:
    """Return (text, parsed_json_or_None). Workers may emit a fenced ```json block."""
    parsed = None
    if "```json" in output:
        try:
            frag = output.split("```json", 1)[1].split("```", 1)[0]
            parsed = json.loads(frag)
        except (IndexError, json.JSONDecodeError):
            parsed = None
    return output, parsed


def run_worker_task(
    bus: BuzzBus,
    hermes: HermesAdapter,
    governor: Governor,
    channel_id: str,
    task_env: dict,
    task_event_id: Optional[str] = None,
    workdir: Optional[str] = None,
) -> WorkerResult:
    task_id = task_env["task_id"]
    goal = task_env["goal"]
    criteria_spec = task_env.get("success_criteria", [])
    max_turns = int(task_env.get("max_turns", 5))

    state = TaskState(
        task_id=task_id,
        goal=goal,
        criteria=criteria_from_spec(criteria_spec),
        max_turns=max_turns,
        require_all=bool(task_env.get("require_all", True)),
    )

    last_output = ""
    while True:
        prompt = _build_prompt(goal, criteria_spec, state.history, state.turn + 1, max_turns)
        try:
            output = hermes.run_turn(prompt, workdir=workdir)
        except Exception as e:  # noqa: BLE001 - report failures as signed events
            bus.publish(
                channel_id,
                bus.make_envelope("result", task_id, status="failed", turn=state.turn,
                                  error=str(e)),
                reply_to=task_event_id,
            )
            return WorkerResult(task_id, "failed", state.turn, str(e))

        last_output = output
        text, parsed = _parse_output(output)
        decision = governor.evaluate(state, text, parsed)

        # signed progress event every turn
        bus.publish(
            channel_id,
            bus.make_envelope("progress", task_id, turn=state.turn, decision=decision.value,
                              output=text[:4000]),
            reply_to=task_event_id,
        )

        if decision is Decision.COMPLETE:
            bus.publish(
                channel_id,
                bus.make_envelope("result", task_id, status="done", turn=state.turn,
                                  output=text[:8000]),
                reply_to=task_event_id,
            )
            return WorkerResult(task_id, "done", state.turn, text)

        if decision is Decision.CHECKPOINT:
            # Budget exhausted without meeting criteria -> hand back for review.
            bus.publish(
                channel_id,
                bus.make_envelope("checkpoint", task_id, status="needs_review",
                                  turn=state.turn, max_turns=max_turns, output=text[:8000]),
                reply_to=task_event_id,
            )
            return WorkerResult(task_id, "needs_review", state.turn, text)

        # else CONTINUE -> loop for another turn
