import json

from langchain_core.messages import AIMessage

from agentlab.agent.agent import Agent, AgentConfig
from agentlab.gateway.audit import AuditLog
from agentlab.gateway.pipeline import Gateway
from agentlab.gateway.policy.engine import PolicyEngine
from agentlab.gateway.registry import Registry, ServerEntry
from agentlab.gateway.server import create_server
from agentlab.recorder.store import RecorderStore, attach_audit
from agentlab.servers import fs
from agentlab.shared.trace import TraceWriter
from test_agent import PROFILE, ScriptedModel, call


def test_ingest_is_incremental_and_waits_for_whole_lines(agentlab_home):
    store = RecorderStore(agentlab_home)
    w = TraceWriter("r1", agentlab_home / "traces")
    w.emit("run_start", {"message": "hi", "model": "m", "thread_id": "t"})
    w.emit("llm_call", {"response": {"role": "ai", "content": "x"}}, latency_ms=40)
    assert store.ingest_folder() == 2
    assert store.ingest_folder() == 0  # nothing new
    assert store.runs()[0]["status"] == "running"

    with w.path.open("a", encoding="utf-8") as f:
        f.write('{"run_id": "r1", "step": 3, "ty')  # a writer mid-line
    assert store.ingest_folder() == 0
    with w.path.open("a", encoding="utf-8") as f:
        f.write('pe": "run_end", "ts": "2026-01-01T00:00:00+00:00", "payload": {"answer": "done"}}\n')
    assert store.ingest_folder() == 1

    [run] = store.runs()
    assert run["status"] == "ok" and run["answer"] == "done" and run["llm_calls"] == 1 and run["steps"] == 3


async def test_run_is_joined_with_gateway_audit(tmp_path, agentlab_home):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    audit = AuditLog(agentlab_home)
    async with Registry([ServerEntry("fs", fs.create_server(roots=[tmp_path]))]) as registry:
        server = create_server(Gateway(registry, PolicyEngine(PROFILE), audit=audit, client="my-agent"))
        script = [call("fs__read_file", path=str(tmp_path / "a.txt")),
                  call("fs__delete", path=str(tmp_path / "a.txt")), AIMessage("done")]
        agent = Agent(AgentConfig(), model=ScriptedModel(messages=iter(script)), gateway=server)
        events = [e async for e in agent.run("read then delete")]
    run_id = events[0]["run_id"]

    rows = audit.query(run_id=run_id)
    assert {r["tool"] for r in rows} == {"fs__read_file", "fs__delete"}  # the run id travelled in _meta

    store = RecorderStore(agentlab_home)
    store.ingest_folder()
    run = store.run(run_id)
    attach_audit(run, rows)
    calls = {e["payload"]["tool"]: e for e in run["events"] if e["type"] == "tool_call"}
    assert calls["fs__read_file"]["gateway"]["decision"] == "allow"
    assert calls["fs__delete"]["gateway"]["decision"] == "deny"
    assert run["tool_errors"] == 1 and run["status"] == "ok"
    assert run["message"] == "read then delete"


def test_http_ingest_is_idempotent(agentlab_home):
    from agentlab.shared.trace import TraceEvent

    store = RecorderStore(agentlab_home)
    events = [TraceEvent(run_id="r2", step=1, type="run_start", payload={"message": "m"}),
              TraceEvent(run_id="r2", step=2, type="run_end", payload={"answer": "a"})]
    store.add_events(events)
    store.add_events(events)
    assert len(store.run("r2")["events"]) == 2
    assert json.dumps(store.runs()[0]["answer"]) == '"a"'


def test_runs_are_tagged_and_filtered_by_kind(agentlab_home):
    traces = agentlab_home / "traces"
    for run_id, extra in [("chat1", {}), ("arena1", {"arena": {"scenario": "inbox", "defense": "none", "trial": 0}}),
                          ("rep1", {"replay_of": "chat1"}), ("fork1", {"forked_from": "chat1"})]:
        w = TraceWriter(run_id, traces)
        w.emit("run_start", {"message": run_id, **extra})
        w.emit("run_end", {"answer": "ok"})
    store = RecorderStore(agentlab_home)
    store.ingest_folder()

    def ids(kind):
        return sorted(r["run_id"] for r in store.runs(kind=kind))

    assert ids("chat") == ["chat1"]
    assert ids("arena") == ["arena1"]
    assert ids("replay") == ["fork1", "rep1"]
    assert len(ids(None)) == 4
    assert store.run("arena1")["tags"]["arena"]["scenario"] == "inbox"


def test_old_recorder_db_gains_tags_column(agentlab_home):
    import sqlite3

    w = TraceWriter("old1", agentlab_home / "traces")
    w.emit("run_start", {"message": "m", "replay_of": "x"})
    w.emit("run_end", {"answer": "a"})
    RecorderStore(agentlab_home).ingest_folder()
    with sqlite3.connect(agentlab_home / "recorder.db") as db:  # simulate a database from before run tags
        db.execute("ALTER TABLE runs DROP COLUMN meta_json")
    store = RecorderStore(agentlab_home)
    assert store.run("old1")["tags"] == {"replay_of": "x"}
