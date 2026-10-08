"""Rule-based judge: attack success and task success from a scenario's final state."""

from dataclasses import dataclass
from typing import Any

from agentlab.arena.scenarios import Scenario


@dataclass(frozen=True)
class Verdict:
    attack_success: bool
    task_success: bool


def judge(scenario: Scenario, state: Any) -> Verdict:
    return Verdict(scenario.attack_success(state), scenario.task_success(state))
