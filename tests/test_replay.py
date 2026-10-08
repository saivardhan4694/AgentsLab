"""Replay, fork, diff and golden runs, driven by a scripted model and the in-process Gateway."""

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from mcp import Client

from agentlab.agent.agent import Agent, AgentConfig
from agentlab.gateway.pipeline import Gateway
from agentlab.gateway.policy.engine import PolicyEngine
from agentlab.gateway.registry import Registry, ServerEntry
from agentlab.gateway.server import create_server
from agentlab.recorder.diff import diff_runs
from agentlab.recorder.golden import expected_from_run, run_suite
from agentlab.recorder.replay import ForkSpec, recorded_results, replay
from agentlab.recorder.store import RecorderStore
from agentlab.servers import fs
from test_agent import PROFILE, ScriptedModel, call


@pytest.fixture
async def recorded(tmp_path, agentlab_home):
    """A recorded run that reads a.txt then answers. Returns (store, run_id, server, work_dir)."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.txt").write_text("hello", encoding="utf-8")
    registry = Registry([ServerEntry("fs", fs.create_server(roots=[work]))])
    async with registry:
        server = create_server(Gateway(registry, PolicyEngine(PROFILE)))
        agent = Agent(AgentConfig(), model=ScriptedModel(messages=iter(
            [call("fs__read_file", path=str(work / "a.txt")), AIMessage("The file says hello.")])), gateway=server)
        events = [e async for e in agent.run("read a.txt")]
        store = RecorderStore(agentlab_home)
        store.ingest_folder()
        yield store, events[0]["run_id"], server, work


async def test_replay_uses_recorded_results_not_the_live_server(recorded):
    store, run_id, server, work = recorded
    (work / "a.txt").unlink()  # a live call would now fail; replay must still return the recorded text
    agent = Agent(AgentConfig(), model=ScriptedModel(messages=iter(
        [call("fs__read_file", path=str(work / "a.txt")), AIMessage("The file says hello.")])))
    result = await replay(store, run_id, agent, Client(server))
    store.ingest_folder()

    assert not result.is_fork and result.unmatched == []
    new = store.run(result.run_id)
    assert new["events"][0]["payload"]["replay_of"] == run_id
    tool_result = next(e for e in new["events"] if e["type"] == "tool_result")
    assert tool_result["payload"]["content"].strip('"').find("hello") != -1 or "hello" in str(tool_result["payload"]["content"])
    assert not tool_result["payload"]["is_error"]


async def test_fork_overrides_a_tool_result(recorded):
    store, run_id, server, _ = recorded
    [rec] = recorded_results(store.run(run_id))
    fork = ForkSpec(result_overrides={rec.call_step: {"content": "the file is EMPTY", "is_error": False}})
    agent = Agent(AgentConfig(), model=ScriptedModel(messages=iter(
        [call("fs__read_file", path="x"), AIMessage("done")])))
    result = await replay(store, run_id, agent, Client(server), fork)
    store.ingest_folder()

    assert result.is_fork
    new = store.run(result.run_id)
    assert new["events"][0]["payload"]["forked_from"] == run_id
    tool_result = next(e for e in new["events"] if e["type"] == "tool_result")
    assert "EMPTY" in str(tool_result["payload"]["content"])


async def test_fork_with_new_message(recorded):
    store, run_id, server, _ = recorded
    agent = Agent(AgentConfig(), model=ScriptedModel(messages=iter([AIMessage("hi there")])))
    result = await replay(store, run_id, agent, Client(server), ForkSpec(message="just say hi"))
    store.ingest_folder()
    assert store.run(result.run_id)["message"] == "just say hi"


async def test_diverged_fork_reports_unmatched(recorded):
    store, run_id, server, work = recorded
    # The forked model calls a tool the recording never ran, so there is no recorded result for it.
    agent = Agent(AgentConfig(), model=ScriptedModel(messages=iter(
        [call("fs__stat", path=str(work)), AIMessage("done")])))
    result = await replay(store, run_id, agent, Client(server), ForkSpec(message="stat the folder"))
    assert result.unmatched == ["fs__stat"]


async def test_diff_detects_a_changed_path(recorded):
    store, run_id, server, work = recorded
    agent = Agent(AgentConfig(), model=ScriptedModel(messages=iter(
        [call("fs__stat", path=str(work)), AIMessage("done")])))
    forked = await replay(store, run_id, agent, Client(server), ForkSpec(message="stat it"))
    store.ingest_folder()

    same = diff_runs(store.run(run_id), store.run(run_id))
    assert same["same_tool_path"] and all(r["status"] == "same" for r in same["rows"])

    changed = diff_runs(store.run(run_id), store.run(forked.run_id))
    assert not changed["same_tool_path"]
    assert any(r["status"] in ("changed", "only_a", "only_b") for r in changed["rows"])


async def test_golden_suite_passes_then_fails(recorded):
    store, run_id, server, work = recorded
    expected = expected_from_run(store.run(run_id))
    store.save_golden(run_id, "read a.txt", expected)

    scripts = iter([
        [call("fs__read_file", path=str(work / "a.txt")), AIMessage("same path")],  # reproduces the golden
        [call("fs__stat", path=str(work)), AIMessage("different path")],            # regresses
    ])

    def agent_factory():
        return Agent(AgentConfig(), model=ScriptedModel(messages=iter(next(scripts))))

    passing = await run_suite(store, agent_factory, lambda: Client(server))
    assert passing[0]["passed"] is True

    failing = await run_suite(store, agent_factory, lambda: Client(server))
    assert failing[0]["passed"] is False
    assert failing[0]["actual_tool_path"] != failing[0]["expected_tool_path"]
