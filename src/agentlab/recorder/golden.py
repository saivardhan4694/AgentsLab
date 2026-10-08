"""Golden runs: regression tests for the agent.

Save a run you are happy with as a golden. Later (after a prompt, model, or tool change) replay it
with the recorded environment and check the agent still takes the same path of tool calls. The local
model is not fully deterministic, so the check compares the sequence of tool calls (names and
arguments), which is the meaningful regression signal, not the exact wording of the answer.
"""

from typing import Any

from agentlab.recorder.diff import path
from agentlab.recorder.replay import ReplayResult, replay


def expected_from_run(run: dict[str, Any]) -> dict[str, Any]:
    """The baseline to compare future replays against: the ordered tool calls and the final answer."""
    return {
        "tool_path": [n.signature for n in path(run) if n.kind == "tool"],
        "answer": run.get("answer"),
    }


def check(expected: dict[str, Any], new_run: dict[str, Any]) -> dict[str, Any]:
    actual = expected_from_run(new_run)
    return {
        "passed": actual["tool_path"] == expected.get("tool_path"),
        "expected_tool_path": expected.get("tool_path"),
        "actual_tool_path": actual["tool_path"],
    }


async def run_suite(store: Any, agent_factory: Any, inner_factory: Any) -> list[dict[str, Any]]:
    """Replay every golden and check it. `agent_factory()` and `inner_factory()` make a fresh agent and
    a fresh schema client for each golden."""
    results = []
    for g in store.goldens():
        try:
            rr: ReplayResult = await replay(store, g["run_id"], agent_factory(), inner_factory())
            store.ingest_folder()
            new_run = store.run(rr.run_id)
            outcome = check(g["expected"], new_run)
            results.append({"golden_id": g["id"], "label": g["label"], "source_run_id": g["run_id"],
                            "new_run_id": rr.run_id, "unmatched": rr.unmatched, **outcome})
        except Exception as e:  # noqa: BLE001 - one broken golden must not stop the suite
            results.append({"golden_id": g["id"], "label": g["label"], "source_run_id": g["run_id"],
                            "passed": False, "error": f"{type(e).__name__}: {e}"})
    return results
