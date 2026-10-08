"""Matrix runner: scenario x attack x defense, scored by the judge (design.md section 5).

Runs the AgentLab agent (`agentlab.agent.Agent`) against each fake scenario server through
a real Gateway pipeline, with the defense under test wired in as a plugin. The policy is
wide open (`allow` everything): the variable under test is the defense plugin, not the
Gateway's rule engine.

Each cell also runs a no-attack baseline per scenario (`attack_id == "baseline"`), so a wrong
transfer or upload can be told apart from an actual attack succeeding, and so a defense's cost
to normal task completion is visible on its own row. Cells run `trials` independent times
(temperature 0; a small local model is not deterministic even then) and report rates.
"""

import argparse
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio

from agentlab.agent.agent import Agent, AgentConfig
from agentlab.arena.attacks import ATTACKS, Attack
from agentlab.arena.defenses import DEFENSES
from agentlab.arena.judge import judge
from agentlab.arena.scenarios import SCENARIOS, Scenario
from agentlab.gateway.pipeline import Gateway
from agentlab.gateway.policy.engine import PolicyEngine, Profile, Rule
from agentlab.gateway.registry import Registry, ServerEntry
from agentlab.gateway.server import create_server

log = logging.getLogger(__name__)

ALLOW_ALL = Profile("arena", visible=["*"], visible_risk=None, guards=[],
                    rules=[Rule(match={}, action="allow", source="arena")], budgets=[])
TRIALS_DEFAULT = 5


@dataclass(frozen=True)
class MatrixResult:
    scenario: str
    attack_id: str  # "baseline" for the no-attack row
    category: str  # "none" for baseline
    defense: str
    trials: int
    attack_successes: int
    task_successes: int
    errors: list[str] = field(default_factory=list)


def _cells(attacks: list[Attack]) -> list[tuple[Scenario, Attack | None]]:
    """One no-attack baseline per scenario touched, then one cell per attack."""
    seen: dict[str, Scenario] = {}
    for a in attacks:
        seen.setdefault(a.scenario, SCENARIOS[a.scenario])
    return [(s, None) for s in seen.values()] + [(SCENARIOS[a.scenario], a) for a in attacks]


async def _run_trial(scenario: Scenario, attack: Attack | None, defense: str, model: Any,
                     meta: dict[str, Any]) -> tuple[bool, bool, str | None]:
    """One run of the agent against one (scenario, attack, defense). Returns (attack_success, task_success, error)."""
    data_attack = attack if attack and attack.place == "data" else None
    server, state = scenario.build(data_attack)
    task = scenario.task
    if attack and attack.place == "prompt":
        task = f"{task}\n\n{attack.payload}"

    error: str | None = None
    async with Registry([ServerEntry(scenario.name, server)]) as registry:
        gateway = Gateway(registry, PolicyEngine(ALLOW_ALL), plugins=DEFENSES[defense](scenario))
        # Temperature 0: as reproducible as a small local model allows, matching Recorder replays/goldens.
        agent = Agent(AgentConfig(temperature=0.0), model=model, gateway=create_server(gateway))
        async for event in agent.run(task, meta=meta):
            if event["type"] == "error":
                error = event["message"]
    verdict = judge(scenario, state)
    return verdict.attack_success, verdict.task_success, error


async def run_cell(scenario: Scenario, attack: Attack | None, defense: str,
                   trials: int = TRIALS_DEFAULT, model: Any = None) -> MatrixResult:
    """One (scenario, attack, defense) cell, run `trials` independent times."""
    attack_id = attack.id if attack else "baseline"
    attack_successes = task_successes = 0
    errors: list[str] = []
    for trial in range(trials):
        meta = {"arena": {"scenario": scenario.name, "attack_id": attack_id, "defense": defense, "trial": trial}}
        attack_ok, task_ok, error = await _run_trial(scenario, attack, defense, model, meta)
        attack_successes += attack_ok
        task_successes += task_ok
        if error:
            errors.append(error)
    return MatrixResult(scenario.name, attack_id, attack.category if attack else "none", defense,
                        trials, attack_successes, task_successes, errors)


async def run_matrix(attacks: list[Attack] = ATTACKS, defenses: list[str] = list(DEFENSES),
                     trials: int = TRIALS_DEFAULT, model: Any = None,
                     on_progress: Callable[[int, int], None] | None = None) -> list[MatrixResult]:
    cells = _cells(attacks)
    results = []
    total = len(cells) * len(defenses)
    done = 0
    for scenario, attack in cells:
        for defense in defenses:
            results.append(await run_cell(scenario, attack, defense, trials, model))
            done += 1
            if on_progress:
                on_progress(done, total)
    return results


@dataclass
class ArenaRunState:
    """Progress of a matrix run, for the admin API (`api.py`) and the Arena UI page."""

    running: bool = False
    done: int = 0
    total: int = 0
    results: list[MatrixResult] = field(default_factory=list)
    error: str | None = None


async def run_matrix_into(state: ArenaRunState, attacks: list[Attack], defenses: list[str],
                          trials: int = TRIALS_DEFAULT) -> None:
    """Runs the matrix, writing progress into `state` as it goes, for a caller to poll."""
    cells = _cells(attacks)
    state.running, state.done, state.total = True, 0, len(cells) * len(defenses)
    state.results, state.error = [], None
    try:
        for scenario, attack in cells:
            for defense in defenses:
                state.results.append(await run_cell(scenario, attack, defense, trials))
                state.done += 1
    except Exception as e:  # noqa: BLE001 - surfaced to the UI, not raised into the background task runner
        state.error = f"{type(e).__name__}: {e}"
    finally:
        state.running = False


def to_markdown(results: list[MatrixResult]) -> str:
    """Attack-success vs task-success rate matrix, one table per defense (design.md section 5)."""
    defenses = sorted({r.defense for r in results}, key=lambda d: list(DEFENSES).index(d) if d in DEFENSES else 99)
    lines = [
        "# Arena results", "",
        "Each cell is N independent trials; rates are `successes/trials`. The `baseline` row per "
        "scenario has no attack, so it separates a plain model mistake from an attack actually "
        "succeeding, and shows what each defense costs normal task completion.", "",
    ]
    if "sanitizer" in defenses:
        lines += [
            "> `sanitizer` note: its redaction patterns are broad by design (for example they also "
            "match ordinary phrasing like \"you must reply by Friday\"). A lower task-success rate "
            "under `sanitizer`, including on the baseline row, can be this cost rather than a judge bug.",
            "",
        ]
    for defense in defenses:
        rows = sorted((r for r in results if r.defense == defense),
                      key=lambda r: (r.scenario, r.attack_id != "baseline", r.attack_id))
        attacked = [r for r in rows if r.attack_id != "baseline"]
        attack_trials = sum(r.trials for r in attacked) or 1
        blocked = sum(r.trials - r.attack_successes for r in attacked)
        task_trials = sum(r.trials for r in rows) or 1
        useful = sum(r.task_successes for r in rows)
        lines += [
            f"## Defense: {defense}",
            "",
            f"Attacks blocked: {blocked}/{attack_trials}. Task completion overall (including baseline): {useful}/{task_trials}.",
            "",
            "| Scenario | Attack | Category | Attack success rate | Task success rate | Errors |",
            "|---|---|---|---|---|---|",
        ]
        for r in rows:
            lines.append(
                f"| {r.scenario} | {r.attack_id} | {r.category} | "
                f"{r.attack_successes}/{r.trials} | {r.task_successes}/{r.trials} | {len(r.errors)} |"
            )
        lines.append("")
    return "\n".join(lines)


async def _main_async(args: argparse.Namespace) -> None:
    attacks = [a for a in ATTACKS if a.scenario == args.scenario] if args.scenario else ATTACKS
    defenses = args.defenses or list(DEFENSES)

    def progress(done: int, total: int) -> None:
        print(f"[{done}/{total}]", end="\r", flush=True)

    results = await run_matrix(attacks, defenses, args.trials, on_progress=progress)
    print()
    md = to_markdown(results)
    if args.out:
        args.out.write_text(md, encoding="utf-8")
        print(f"Wrote {args.out}")
    else:
        print(md)


def main() -> None:
    parser = argparse.ArgumentParser(description="AgentLab Arena: prompt-injection defense matrix")
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), help="only run attacks for this scenario")
    parser.add_argument("--defenses", nargs="*", choices=sorted(DEFENSES), help="defenses to run (default: all)")
    parser.add_argument("--trials", type=int, default=TRIALS_DEFAULT, help="independent trials per cell")
    parser.add_argument("--out", type=Path, help="write the Markdown report here instead of stdout")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    anyio.run(_main_async, args)


if __name__ == "__main__":
    main()
