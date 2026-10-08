"""Replay and fork recorded runs.

Replay re-runs the agent with the recorded tool results substituted for live tool calls, so the
environment is held fixed and only the model (and whatever you edited) can change the outcome.

- Replay: same message and system prompt, recorded results served in call order.
- Fork: change the message, the system prompt, or a specific tool result, then replay from the top.

A replay never touches the real downstream servers and never writes to the audit log; it only needs
the Gateway running to read the tool schemas (so the model sees the same tools). The new run gets its
own trace, tagged with `replay_of`/`forked_from` in its run_start event.
"""

from dataclasses import dataclass, field
from typing import Any

from mcp.types import CallToolResult, TextContent


@dataclass
class RecordedResult:
    tool: str
    call_step: int
    content: str
    is_error: bool


def recorded_results(run: dict[str, Any]) -> list[RecordedResult]:
    """Pair each tool_call with its tool_result, in call order."""
    results_by_parent = {e["parent_step"]: e for e in run["events"] if e["type"] == "tool_result"}
    out = []
    for e in run["events"]:
        if e["type"] == "tool_call":
            result = results_by_parent.get(e["step"])
            payload = result["payload"] if result else {}
            out.append(RecordedResult(
                tool=e["payload"].get("tool", ""),
                call_step=e["step"],
                content=str(payload.get("content", "")),
                is_error=bool(payload.get("is_error", False)),
            ))
    return out


@dataclass
class ReplayClient:
    """Stands in for the Gateway MCP client. Tool schemas come from `inner`; tool *results* come from
    the recording, served per tool name in call order. A call with nothing left recorded is returned as
    an error, so a diverged fork is visible rather than silently hitting a live server."""

    inner: Any  # a real mcp.Client, for list_tools (schemas)
    results: list[RecordedResult]
    _queues: dict[str, list[RecordedResult]] = field(default_factory=dict)
    unmatched: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        for r in self.results:
            self._queues.setdefault(r.tool, []).append(r)

    async def __aenter__(self) -> "ReplayClient":
        await self.inner.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> Any:
        return await self.inner.__aexit__(*exc)

    async def list_tools(self, **kwargs: Any) -> Any:
        return await self.inner.list_tools(**kwargs)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None, **kwargs: Any) -> CallToolResult:
        queue = self._queues.get(name)
        if queue:
            rec = queue.pop(0)
            return CallToolResult(content=[TextContent(type="text", text=rec.content)], is_error=rec.is_error)
        self.unmatched.append(name)
        text = (f"[replay] No recorded result for {name} with these arguments. "
                "The run diverged from the recording, so there is nothing to replay for this call.")
        return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)


def apply_overrides(results: list[RecordedResult], overrides: dict[int, dict[str, Any]] | None) -> list[RecordedResult]:
    """Replace recorded results by tool_call step. `overrides[step] = {"content": ..., "is_error": ...}`."""
    if not overrides:
        return results
    out = []
    for r in results:
        o = overrides.get(r.call_step)
        if o is None:
            out.append(r)
        else:
            out.append(RecordedResult(r.tool, r.call_step, str(o.get("content", r.content)),
                                      bool(o.get("is_error", r.is_error))))
    return out


@dataclass
class ForkSpec:
    message: str | None = None  # new user message; default: the recorded one
    system_prompt: str | None = None  # new system prompt; default: the agent's current one
    result_overrides: dict[int, dict[str, Any]] = field(default_factory=dict)  # tool_call step -> {content, is_error}

    @property
    def is_fork(self) -> bool:
        return bool(self.message or self.system_prompt or self.result_overrides)


@dataclass
class ReplayResult:
    run_id: str  # the new run
    source_run_id: str  # the run that was replayed
    is_fork: bool
    unmatched: list[str]  # tools the new run called that the recording had no result for
    events: list[dict[str, Any]]


async def replay(store: Any, source_run_id: str, agent: Any, inner_client: Any,
                 fork: ForkSpec | None = None) -> ReplayResult:
    """Re-run `source_run_id` with recorded tool results. `agent` supplies the model and settings;
    `inner_client` is a live Gateway client used only for tool schemas. Returns the new run."""
    run = store.run(source_run_id)
    if run is None:
        raise ValueError(f"no run {source_run_id}")
    fork = fork or ForkSpec()
    results = apply_overrides(recorded_results(run), fork.result_overrides)
    message = fork.message or run.get("message") or ""
    if fork.system_prompt is not None:
        agent.config.system_prompt = fork.system_prompt

    agent.mcp_client = ReplayClient(inner_client, results)
    tag = "forked_from" if fork.is_fork else "replay_of"
    events = [e async for e in agent.run(message, meta={tag: source_run_id})]
    return ReplayResult(
        run_id=events[0]["run_id"],
        source_run_id=source_run_id,
        is_fork=fork.is_fork,
        unmatched=agent.mcp_client.unmatched,
        events=events,
    )
