"""LangChain callback handler that turns model and tool activity into trace events."""

import time
from typing import Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.outputs import LLMResult

from agentlab.shared.trace import Tokens, TraceWriter


def _message(m: BaseMessage) -> dict[str, Any]:
    out: dict[str, Any] = {"role": m.type, "content": m.content}
    if getattr(m, "tool_calls", None):
        out["tool_calls"] = m.tool_calls
    if getattr(m, "tool_call_id", None):
        out["tool_call_id"] = m.tool_call_id
    return out


class TraceCallback(AsyncCallbackHandler):
    def __init__(self, writer: TraceWriter):
        self.writer = writer
        self._started: dict[UUID, tuple[float, Any]] = {}
        self._last_llm_step: int | None = None
        self._tool_steps: dict[UUID, int] = {}

    async def on_chat_model_start(self, serialized: dict[str, Any], messages: list[list[BaseMessage]], *,
                                  run_id: UUID, **kwargs: Any) -> None:
        self._started[run_id] = (time.monotonic(), [_message(m) for m in messages[0]])

    async def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        start, prompt = self._started.pop(run_id, (time.monotonic(), []))
        gen = response.generations[0][0]
        msg = getattr(gen, "message", None)
        usage = getattr(msg, "usage_metadata", None) or {}
        payload = {
            "messages": prompt,
            "response": _message(msg) if msg is not None else {"role": "ai", "content": gen.text},
            "reasoning": (msg.additional_kwargs.get("reasoning_content") if msg is not None else None),
            "model": (msg.response_metadata.get("model") if msg is not None else None),
        }
        self._last_llm_step = self.writer.emit(
            "llm_call", payload, latency_ms=round((time.monotonic() - start) * 1000),
            tokens=Tokens(input=usage.get("input_tokens", 0), output=usage.get("output_tokens", 0)))

    async def on_tool_start(self, serialized: dict[str, Any], input_str: str, *, run_id: UUID,
                            inputs: dict[str, Any] | None = None, **kwargs: Any) -> None:
        self._started[run_id] = (time.monotonic(), None)
        self._tool_steps[run_id] = self.writer.emit(
            "tool_call", {"tool": serialized.get("name"), "args": inputs if inputs is not None else input_str},
            parent_step=self._last_llm_step)

    async def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        start, _ = self._started.pop(run_id, (time.monotonic(), None))
        content = getattr(output, "content", output)
        status = getattr(output, "status", "success")
        self.writer.emit("tool_result", {"content": content, "is_error": status == "error"},
                         parent_step=self._tool_steps.pop(run_id, None),
                         latency_ms=round((time.monotonic() - start) * 1000))

    async def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._started.pop(run_id, None)
        self.writer.emit("error", {"where": "tool", "error": f"{type(error).__name__}: {error}"},
                         parent_step=self._tool_steps.pop(run_id, None))

    async def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._started.pop(run_id, None)
        self.writer.emit("error", {"where": "llm", "error": f"{type(error).__name__}: {error}"})
