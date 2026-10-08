"""The AgentLab agent: a LangGraph agent on a local Ollama model, using tools only through the Gateway.

It connects to the Gateway's `/mcp` endpoint like any other client, so every tool call goes
through the same policy, approvals, snapshots, and audit. Each run writes a trace (shared/trace.py).
"""

import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx2
from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from agentlab.agent.mcp_tools import current_run, load_tools
from agentlab.agent.tracing import TraceCallback
from agentlab.shared.trace import TraceWriter

SYSTEM_PROMPT = """You are the AgentLab assistant. You act on the user's own computer through tools \
provided by the AgentLab Gateway. Tool names look like server__tool.

Rules:
- Use absolute paths. On Windows the home folder is %USERPROFILE%; find it with system__os_info if needed.
- Prefer reading and checking before changing anything.
- Some calls are denied by policy, and some wait for a human to approve. If a call is denied, do not \
retry the same call; explain what was blocked and suggest another way.
- Keep answers short and concrete. Report what you did and what happened."""


@dataclass
class AgentConfig:
    model: str = os.environ.get("AGENTLAB_MODEL", "qwen3.5:4b")
    num_ctx: int = int(os.environ.get("AGENTLAB_NUM_CTX", "16384"))
    reasoning: bool = True  # qwen3.5 thinks before answering; better tool use, a bit slower
    temperature: float = 0.2
    ollama_url: str = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    gateway_url: str = os.environ.get("AGENTLAB_GATEWAY_URL", "http://127.0.0.1:8000/mcp")
    token_env: str = "AL_TOKEN_AGENT"
    system_prompt: str = SYSTEM_PROMPT
    max_steps: int = 40  # LangGraph recursion limit: model and tool steps together

    def chat_model(self) -> BaseChatModel:
        from langchain_ollama import ChatOllama

        url = self.ollama_url if self.ollama_url.startswith("http") else f"http://{self.ollama_url}"
        return ChatOllama(model=self.model, base_url=url, num_ctx=self.num_ctx, reasoning=self.reasoning,
                          temperature=self.temperature)

    def gateway_client(self) -> Client:
        token = os.environ.get(self.token_env)
        if not token:
            raise RuntimeError(f"Set {self.token_env} to the agent's Gateway token (see config/clients.yaml)")
        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=httpx2.Timeout(30, read=None))
        return Client(streamable_http_client(self.gateway_url, http_client=http))


@dataclass
class Agent:
    """Holds chat memory across runs. Each `run` connects to the Gateway, runs one turn, and disconnects."""

    config: AgentConfig = field(default_factory=AgentConfig)
    model: BaseChatModel | None = None  # tests pass a fake model
    gateway: Any = None  # anything mcp.Client accepts; default: the Gateway URL with the agent token
    checkpointer: InMemorySaver = field(default_factory=InMemorySaver)

    async def run(self, message: str, thread_id: str | None = None) -> AsyncIterator[dict[str, Any]]:
        """Stream one turn as events: run_start, thinking, token, tool_call, tool_result, done, error."""
        thread_id = thread_id or uuid.uuid4().hex
        run_id = uuid.uuid4().hex[:12]
        writer = TraceWriter(run_id)
        writer.emit("run_start", {"thread_id": thread_id, "message": message, "model": self.config.model})
        yield {"type": "run_start", "run_id": run_id, "thread_id": thread_id}
        final = ""
        token = current_run.set(run_id)
        try:
            client = Client(self.gateway) if self.gateway is not None else self.config.gateway_client()
            async with client:
                tools = await load_tools(client)
                agent = create_agent(self.model or self.config.chat_model(), tools,
                                     system_prompt=self.config.system_prompt, checkpointer=self.checkpointer)
                stream = agent.astream(
                    {"messages": [HumanMessage(message)]},
                    config={"configurable": {"thread_id": thread_id}, "callbacks": [TraceCallback(writer)],
                            "recursion_limit": self.config.max_steps},
                    stream_mode=["messages", "updates"],
                )
                async for mode, data in stream:
                    if mode == "messages":
                        chunk, _ = data
                        if isinstance(chunk, AIMessage):  # chunks when streaming, whole messages otherwise
                            thinking = chunk.additional_kwargs.get("reasoning_content")
                            if thinking:
                                yield {"type": "thinking", "text": thinking}
                            if isinstance(chunk.content, str) and chunk.content:
                                yield {"type": "token", "text": chunk.content}
                        continue
                    for update in data.values():
                        for m in (update or {}).get("messages", []):
                            if isinstance(m, AIMessage):
                                for call in m.tool_calls:
                                    yield {"type": "tool_call", "id": call["id"], "name": call["name"], "args": call["args"]}
                                if not m.tool_calls and isinstance(m.content, str):
                                    final = m.content
                            elif isinstance(m, ToolMessage):
                                yield {"type": "tool_result", "id": m.tool_call_id, "name": m.name,
                                       "content": m.content if isinstance(m.content, str) else str(m.content),
                                       "is_error": m.status == "error"}
        except Exception as e:  # noqa: BLE001 - report to the caller and the trace, then end the run
            writer.emit("error", {"where": "run", "error": describe(e)})
            yield {"type": "error", "message": describe(e)}
        finally:
            current_run.reset(token)
        writer.emit("run_end", {"answer": final})
        yield {"type": "done", "run_id": run_id, "trace": str(writer.path)}


def describe(e: BaseException) -> str:
    """Error text with task-group wrappers removed, so the real cause shows."""
    while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:
        e = e.exceptions[0]
    if isinstance(e, BaseExceptionGroup):
        return "; ".join(describe(x) for x in e.exceptions)
    return f"{type(e).__name__}: {e}"
