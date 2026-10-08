"""The agent loop through the real Gateway pipeline, with a scripted model instead of Ollama."""

from typing import Any

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from mcp import Client

from agentlab.agent.agent import Agent, AgentConfig
from agentlab.agent.mcp_tools import load_tools
from agentlab.gateway.pipeline import Gateway
from agentlab.gateway.policy.engine import PolicyEngine, Profile, Rule
from agentlab.gateway.registry import Registry, ServerEntry
from agentlab.gateway.server import create_server
from agentlab.servers import fs
from agentlab.shared.trace import read_trace


class ScriptedModel(GenericFakeChatModel):
    """Replies with the next scripted message; accepts tools like a real chat model."""

    disable_streaming: bool = True  # the fake cannot stream a tool-call message that has no text

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedModel":
        return self


PROFILE = Profile("p", ["fs__*"], None, [], [
    Rule({"tool": "fs__read_file"}, "allow", "p.rules[0]"),
    Rule({"tool": "fs__delete"}, "deny", "p.rules[1]", reason="no deleting"),
], [])


async def run_agent(tmp_path, script: list[AIMessage]) -> list[dict]:
    async with Registry([ServerEntry("fs", fs.create_server(roots=[tmp_path]))]) as registry:
        server = create_server(Gateway(registry, PolicyEngine(PROFILE)))
        agent = Agent(AgentConfig(), model=ScriptedModel(messages=iter(script)), gateway=server)
        return [e async for e in agent.run("read a.txt", thread_id="t1")]


def call(name: str, **args: Any) -> AIMessage:
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": f"call-{name}", "type": "tool_call"}])


async def test_adapter_exposes_schema_and_errors(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    async with Registry([ServerEntry("fs", fs.create_server(roots=[tmp_path]))]) as registry:
        async with Client(create_server(Gateway(registry, PolicyEngine(PROFILE)))) as client:
            tools = {t.name: t for t in await load_tools(client)}
            ok = await tools["fs__read_file"].ainvoke({"path": str(tmp_path / "a.txt")})
            denied = await tools["fs__delete"].ainvoke(
                {"type": "tool_call", "name": "fs__delete", "id": "1", "args": {"path": str(tmp_path / "a.txt")}})
    assert set(tools) == {"fs__list_dir", "fs__read_file", "fs__stat", "fs__search", "fs__write_file",
                          "fs__make_dir", "fs__move", "fs__delete"}
    assert tools["fs__read_file"].args_schema["required"] == ["path"]
    assert "hello" in ok
    assert denied.status == "error" and "no deleting" in denied.content


async def test_agent_runs_tools_through_gateway_and_traces(tmp_path, agentlab_home):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    events = await run_agent(tmp_path, [
        call("fs__read_file", path=str(tmp_path / "a.txt")),
        call("fs__delete", path=str(tmp_path / "a.txt")),
        AIMessage("The file says hello. Deleting it was blocked by policy."),
    ])
    kinds = [e["type"] for e in events]
    assert kinds[0] == "run_start" and kinds[-1] == "done" and "error" not in kinds
    results = {e["name"]: e for e in events if e["type"] == "tool_result"}
    assert "hello" in results["fs__read_file"]["content"] and not results["fs__read_file"]["is_error"]
    assert results["fs__delete"]["is_error"] and (tmp_path / "a.txt").exists()
    assert "".join(e["text"] for e in events if e["type"] == "token").endswith("blocked by policy.")

    trace = read_trace(agentlab_home / "traces" / f"{events[0]['run_id']}.jsonl")
    types = [t.type for t in trace]
    assert types[0] == "run_start" and types[-1] == "run_end"
    assert types.count("llm_call") == 3 and types.count("tool_call") == 2 and types.count("tool_result") == 2
    first_tool = next(t for t in trace if t.type == "tool_call")
    assert trace[first_tool.parent_step - 1].type == "llm_call"  # tool steps point at the model step
    assert [t.step for t in trace] == list(range(1, len(trace) + 1))


async def test_gateway_unreachable_is_reported(tmp_path, agentlab_home):
    agent = Agent(AgentConfig(gateway_url="http://127.0.0.1:9/mcp"), model=ScriptedModel(messages=iter([])),
                  gateway="http://127.0.0.1:9/mcp")
    events = [e async for e in agent.run("hi")]
    assert [e["type"] for e in events] == ["run_start", "error", "done"]
