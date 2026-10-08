"""Recorder in the terminal.

    python -m agentlab.recorder list [--limit N]
    python -m agentlab.recorder show <run_id>
"""

import argparse
import json
import sys

from agentlab.gateway.audit import AuditLog
from agentlab.recorder.store import RecorderStore, attach_audit


def short(value: object, width: int = 100) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = text.replace("\n", " ")
    return text if len(text) <= width else text[: width - 3] + "..."


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m agentlab.recorder", description="Inspect recorded agent runs.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("list")
    p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("show")
    p.add_argument("run_id")
    a = parser.parse_args()

    store = RecorderStore()
    store.ingest_folder()
    if a.command == "list":
        for r in store.runs(a.limit):
            print(f"{r['run_id']}  {r['started_at'][:19]}  {r['status']:<7} {r['steps']:>3} steps "
                  f"{r['tokens_in'] + r['tokens_out']:>6} tok {r['duration_ms'] / 1000:>6.1f}s  {short(r['message'], 60)}")
        return
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
