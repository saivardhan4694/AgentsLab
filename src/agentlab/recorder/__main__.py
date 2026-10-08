"""Recorder in the terminal.

    python -m agentlab.recorder list [--limit N]
    python -m agentlab.recorder show <run_id>
"""

import argparse
import json
import sys
from typing import Any

from agentlab.gateway.audit import AuditLog
from agentlab.recorder.diff import diff_runs
from agentlab.recorder.golden import expected_from_run, run_suite
from agentlab.recorder.store import RecorderStore, attach_audit


def short(value: object, width: int = 100) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = text.replace("\n", " ")
    return text if len(text) <= width else text[: width - 3] + "..."


def golden_cli(store: RecorderStore, a: Any) -> None:
    if a.action == "list":
        for g in store.goldens():
            steps = len(g["expected"].get("tool_path", []))
            print(f"{g['id']}  run {g['run_id']}  {steps} tool calls  {g['label']}")
    elif a.action == "add":
        if not a.value:
            sys.exit("golden add needs a run_id")
        run = store.run(a.value)
        if run is None:
            sys.exit(f"no run {a.value}")
        print("saved golden", store.save_golden(a.value, a.label, expected_from_run(run)))
    elif a.action == "rm":
        sys.exit(None if store.delete_golden(a.value) else f"no golden {a.value}")
    elif a.action == "run":
        import anyio

        from agentlab.agent.agent import Agent, AgentConfig

        results = anyio.run(run_suite, store, lambda: Agent(AgentConfig()), lambda: AgentConfig().gateway_client())
        for r in results:
            mark = "PASS" if r["passed"] else "FAIL"
            print(f"[{mark}] {r['label']}  (golden {r['golden_id']} -> run {r.get('new_run_id', '-')})")
        if any(not r["passed"] for r in results):
            sys.exit("some goldens failed")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m agentlab.recorder", description="Inspect recorded agent runs.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("list")
    p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("show")
    p.add_argument("run_id")
    p = sub.add_parser("diff")
    p.add_argument("run_a")
    p.add_argument("run_b")
    p = sub.add_parser("golden")
    p.add_argument("action", choices=["list", "add", "rm", "run"])
    p.add_argument("value", nargs="?", help="run_id for add, golden id for rm")
    p.add_argument("--label", default="")
    a = parser.parse_args()

    store = RecorderStore()
    store.ingest_folder()
    if a.command == "list":
        for r in store.runs(a.limit):
            print(f"{r['run_id']}  {r['started_at'][:19]}  {r['status']:<7} {r['steps']:>3} steps "
                  f"{r['tokens_in'] + r['tokens_out']:>6} tok {r['duration_ms'] / 1000:>6.1f}s  {short(r['message'], 60)}")
        return
    if a.command == "diff":
        run_a, run_b = store.run(a.run_a), store.run(a.run_b)
        if run_a is None or run_b is None:
            sys.exit("run not found")
        d = diff_runs(run_a, run_b)
        print(f"same tool path: {d['same_tool_path']}")
        for row in d["rows"]:
            mark = {"same": "  ", "changed": "~ ", "only_a": "- ", "only_b": "+ "}[row["status"]]
            left = short((row["a"] or {}).get("summary", ""), 50)
            right = short((row["b"] or {}).get("summary", ""), 50)
            print(f"{mark}{left:<52} | {right}")
        return
    if a.command == "golden":
        return golden_cli(store, a)
    run = store.run(a.run_id)
    if run is None:
        sys.exit(f"No run {a.run_id}")
    attach_audit(run, AuditLog().query(run_id=a.run_id, limit=10_000))
    print(f"run {run['run_id']}  model={run['model']}  status={run['status']}  {run['duration_ms'] / 1000:.1f}s")
    print(f"task: {run['message']}\n")
    for e in run["events"]:
        indent = "    " if e["parent_step"] else ""
        p = e["payload"]
        detail = {
            "llm_call": lambda: short((p.get("response") or {}).get("tool_calls") or (p.get("response") or {}).get("content")),
            "tool_call": lambda: f"{p.get('tool')} {short(p.get('args'), 80)}"
                                 + (f"  [gateway: {e['gateway']['decision']} -> {e['gateway']['outcome']}]" if e.get("gateway") else ""),
            "tool_result": lambda: ("ERROR " if p.get("is_error") else "") + short(p.get("content")),
        }.get(e["type"], lambda: short(p))()
        timing = f"{e['latency_ms']}ms" if e["latency_ms"] is not None else ""
        print(f"{indent}{e['step']:>3} {e['type']:<12} {timing:>8}  {detail}")


if __name__ == "__main__":
    main()
