"""Trace events (design.md section 7): what the Recorder ingests.

For now each run is written as JSON lines to `<home>/traces/<run_id>.jsonl`. The Recorder
(milestone 9) reads these files and, later, receives the same events over HTTP.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from agentlab.shared.home import agentlab_home

EventType = Literal["run_start", "llm_call", "tool_call", "tool_result", "policy_decision", "approval", "error", "run_end"]


class Tokens(BaseModel):
    input: int = Field(0, alias="in")
    output: int = Field(0, alias="out")

    model_config = {"populate_by_name": True}


class TraceEvent(BaseModel):
    run_id: str
    step: int
    parent_step: int | None = None
    ts: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    type: EventType
    payload: dict[str, Any] = {}
    latency_ms: int | None = None
    tokens: Tokens | None = None


class TraceWriter:
    """Appends one run's events to a JSONL file and hands out step numbers."""

    def __init__(self, run_id: str, folder: Path | None = None):
        self.run_id = run_id
        self.folder = folder or agentlab_home() / "traces"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / f"{run_id}.jsonl"
        self._step = 0

    def emit(self, type: EventType, payload: dict[str, Any] | None = None, *, parent_step: int | None = None,
             latency_ms: int | None = None, tokens: Tokens | None = None) -> int:
        self._step += 1
        event = TraceEvent(run_id=self.run_id, step=self._step, parent_step=parent_step, type=type,
                           payload=payload or {}, latency_ms=latency_ms, tokens=tokens)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event.model_dump(mode="json", by_alias=True, exclude_none=True), default=str) + "\n")
        return self._step


def read_trace(path: Path) -> list[TraceEvent]:
    return [TraceEvent.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
