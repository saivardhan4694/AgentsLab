"""Compare two recorded runs step by step.

The comparison looks at the agent's decisions, not the environment: each model response (its text or
the tool calls it asked for) and each tool call (name and arguments). Tool *result* text is shown but
not used to decide whether a step changed, because a replay holds results fixed on purpose.
"""

import json
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class Node:
    kind: str  # "model" | "tool"
    summary: str  # short label
    signature: str  # what two runs are compared on
    detail: dict[str, Any]


def path(run: dict[str, Any]) -> list[Node]:
    """The ordered decisions of a run: model responses and tool calls."""
    results = {e["parent_step"]: e for e in run["events"] if e["type"] == "tool_result"}
    nodes: list[Node] = []
    for e in run["events"]:
        if e["type"] == "llm_call":
            response = e["payload"].get("response") or {}
            calls = response.get("tool_calls") or []
            if calls:
                summary = "asks: " + ", ".join(c.get("name", "?") for c in calls)
                signature = json.dumps([[c.get("name"), c.get("args")] for c in calls], sort_keys=True, default=str)
            else:
                text = str(response.get("content", ""))
                summary = "answers: " + text[:80]
                signature = "text:" + " ".join(text.split())  # ignore whitespace differences
            nodes.append(Node("model", summary, signature, {"response": response, "tokens_out": e.get("tokens_out")}))
        elif e["type"] == "tool_call":
            name = e["payload"].get("tool", "?")
            args = e["payload"].get("args", {})
            result = results.get(e["step"], {}).get("payload", {})
            nodes.append(Node("tool", f"{name}", json.dumps([name, args], sort_keys=True, default=str),
                              {"name": name, "args": args, "result": result.get("content"),
                               "is_error": result.get("is_error")}))
    return nodes


@dataclass
class DiffRow:
    status: str  # "same" | "changed" | "only_a" | "only_b"
    a: dict[str, Any] | None
    b: dict[str, Any] | None


def diff_runs(run_a: dict[str, Any], run_b: dict[str, Any]) -> dict[str, Any]:
    """A longest-common-subsequence alignment of the two decision paths, plus a summary."""
    a, b = path(run_a), path(run_b)
    rows = _align(a, b)
    tool_calls_a = [n.signature for n in a if n.kind == "tool"]
    tool_calls_b = [n.signature for n in b if n.kind == "tool"]
    return {
        "a": {"run_id": run_a["run_id"], "status": run_a.get("status"), "steps": run_a.get("steps"),
              "tokens": (run_a.get("tokens_in", 0) + run_a.get("tokens_out", 0)), "duration_ms": run_a.get("duration_ms"),
              "answer": run_a.get("answer")},
        "b": {"run_id": run_b["run_id"], "status": run_b.get("status"), "steps": run_b.get("steps"),
              "tokens": (run_b.get("tokens_in", 0) + run_b.get("tokens_out", 0)), "duration_ms": run_b.get("duration_ms"),
              "answer": run_b.get("answer")},
        "same_tool_path": tool_calls_a == tool_calls_b,
        "rows": [asdict(r) for r in rows],
    }


def _align(a: list[Node], b: list[Node]) -> list[DiffRow]:
    # Classic LCS over step signatures, then walk back to a row list.
    n, m = len(a), len(b)
    lcs = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            lcs[i][j] = lcs[i + 1][j + 1] + 1 if a[i].signature == b[j].signature else max(lcs[i + 1][j], lcs[i][j + 1])
    rows: list[DiffRow] = []
    i = j = 0
    while i < n and j < m:
        if a[i].signature == b[j].signature:
            rows.append(DiffRow("same", asdict(a[i]), asdict(b[j])))
            i, j = i + 1, j + 1
        elif a[i].kind == b[j].kind and lcs[i + 1][j] == lcs[i][j + 1]:
            # Same kind of step on both sides, contents differ: show them paired.
            rows.append(DiffRow("changed", asdict(a[i]), asdict(b[j])))
            i, j = i + 1, j + 1
        elif lcs[i + 1][j] >= lcs[i][j + 1]:
            rows.append(DiffRow("only_a", asdict(a[i]), None))
            i += 1
        else:
            rows.append(DiffRow("only_b", None, asdict(b[j])))
            j += 1
    rows += [DiffRow("only_a", asdict(a[k]), None) for k in range(i, n)]
    rows += [DiffRow("only_b", None, asdict(b[k])) for k in range(j, m)]
    return rows
