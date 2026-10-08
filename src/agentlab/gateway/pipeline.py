"""The request pipeline for one `tools/call` (design.md section 4.4).

visibility -> policy decision (rules, budgets) -> plugin before_call -> approval if `ask` ->
snapshot -> forward -> plugin after_call (result filters) -> audit.
Every non-allowed path returns a tool error, so the LLM learns why and can choose another way.
"""

import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from mcp import StdioServerParameters
from mcp.types import CallToolResult, Tool

from agentlab.gateway.approvals import ApprovalStore
from agentlab.gateway.audit import AuditLog, AuditRecord
from agentlab.gateway.policy.engine import PolicyEngine, ToolCall
from agentlab.gateway.registry import META_WRITES, Registry, ServerEntry, tool_error, tool_risk
from agentlab.gateway.snapshots import SnapshotError, SnapshotStore

log = logging.getLogger(__name__)

APPROVAL_TIMEOUT_S = 120
META_RUN_ID = "agentlab/run_id"  # request _meta key: the caller's trace run, stored in the audit log


@runtime_checkable
class Plugin(Protocol):
    """A Gateway plugin (the Arena's defenses; see design.md section 5).

    `before_call` runs after rules and budgets, before approval; returning a `CallToolResult`
    blocks the call (recorded as denied). `after_call` runs on the downstream result, before
    audit: the "result filters" step in design.md 4.4 (redact, truncate, wrap untrusted content).
    One plugin instance per session, so it may hold state (for example "has this session seen
    untrusted content yet").
    """

    name: str

    def before_call(self, call: ToolCall) -> "ToolCall | CallToolResult": ...

    def after_call(self, call: ToolCall, result: CallToolResult) -> CallToolResult: ...


class BasePlugin:
    """Plugin base with no-op hooks, so a defense only overrides what it needs."""

    name = "base"

    def before_call(self, call: ToolCall) -> "ToolCall | CallToolResult":
        return call

    def after_call(self, call: ToolCall, result: CallToolResult) -> CallToolResult:
        return result


@dataclass
class Gateway:
    """The pipeline for one upstream client: its policy engine (and budgets), session, and stores."""

    registry: Registry
    engine: PolicyEngine
    approvals: ApprovalStore | None = None  # None: `ask` denies
    snapshots: SnapshotStore | None = None  # None: `allow_with_snapshot` denies, nothing is snapshotted
    approval_timeout: float = APPROVAL_TIMEOUT_S
    audit: AuditLog | None = None
    client: str = "stdio"
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    plugins: list[Plugin] = field(default_factory=list)  # empty: built from the profile's `plugins:` list

    def __post_init__(self) -> None:
        # A caller that passes its own plugins (the Arena, per defense under test) keeps them.
        names = self.engine.profile.plugins
        if self.plugins or not names:
            return
        from agentlab.gateway.plugins import PLUGINS, build_plugins  # plugins.py imports this module

        unknown = [n for n in names if n not in PLUGINS]
        if unknown:
            raise ValueError(f"Profile {self.engine.profile.name}: unknown plugins {unknown}; "
                             f"known: {sorted(PLUGINS)}")
        # Fresh instances per pipeline, so plugin state stays per client session.
        self.plugins = build_plugins(names)

    def visible(self, name: str) -> bool:
        resolved = self.registry.resolve(name)
        return resolved is not None and self.engine.is_visible(name, tool_risk(*resolved))

    def list_tools(self) -> list[Tool]:
        return [t for t in self.registry.list_tools() if self.visible(t.name)]

    async def call_tool(self, name: str, arguments: dict[str, Any] | None,
                        meta: dict[str, Any] | None = None) -> CallToolResult:
        run_id = (meta or {}).get(META_RUN_ID)
        rec = AuditRecord(tool=name, args=arguments or {}, session_id=self.session_id, client=self.client,
                          profile=self.engine.profile.name, run_id=str(run_id) if run_id else None)
        start = time.monotonic()
        result = await self._run(name, arguments or {}, rec)
        rec.duration_ms = round((time.monotonic() - start) * 1000)
        if rec.outcome is None:  # never reached the downstream server
            rec.outcome = "not_run"
        if self.audit is not None:
            try:
                self.audit.write(rec)
            except Exception as e:  # noqa: BLE001 - a broken audit log must not break the call
                log.error("Audit write failed: %s", e)
        return result

    async def _run(self, name: str, args: dict[str, Any], rec: AuditRecord) -> CallToolResult:
        if not self.visible(name):
            rec.decision, rec.reason = "unknown_tool", "unknown or hidden tool"
            return tool_error(f"Unknown tool: {name}")  # hidden tools look exactly like missing ones
        entry, tool = self.registry.resolve(name)
        call = ToolCall(name, args, tool_risk(entry, tool))
        decision = self.engine.decide(call)
        rec.risk, rec.decision, rec.reason = call.risk, decision.action, decision.reason
        rec.rule = decision.rule.source if decision.rule else None
        log.info("[%s] %s risk=%s -> %s (%s)", self.client, call.tool, call.risk, decision.action, decision.reason)
        action = decision.action

        # Plugins only see calls the policy would let run; a denied call never reaches them,
        # so stateful plugins (for example taint tracking) are not changed by it.
        for plugin in self.plugins if action in ("allow", "ask", "allow_with_snapshot") else []:
            verdict = plugin.before_call(call)
            if isinstance(verdict, CallToolResult):
                rec.decision, rec.reason = "deny", f"blocked by plugin {plugin.name}"
                return verdict
            call = verdict

        if action == "ask":
            if self.approvals is None:
                return deny("needs human approval, and no approval channel is configured")
            approved = await self._ask(call, decision.reason, rec)
            if isinstance(approved, CallToolResult):
                return approved
            if approved.args != call.args:
                rec.approval += " (arguments edited)"
                rec.args = approved.args
                # Edited arguments must pass the policy again (guards included).
                recheck = self.engine.evaluate(approved)
                if recheck.action == "deny":
                    return deny(f"edited arguments: {recheck.reason}")
                # "ask" again counts as approved; dry_run and allow_with_snapshot still apply.
                action = "allow" if recheck.action == "ask" else recheck.action
            else:
                action = "allow"
            call = approved

        if action == "dry_run":
            # Sent as an error: the call did not run, and a tool with an output schema
            # would fail client validation on a success result without structured content.
            return tool_error(f"Dry run, not executed: would call {call.tool} with {call.args}")
        if action not in ("allow", "allow_with_snapshot"):
            return deny(decision.reason)

        paths = write_paths(entry, tool, call.args)
        required = action == "allow_with_snapshot"
        if required and (self.snapshots is None or paths is None):
            return deny("needs a file snapshot, but this tool has no `writes` metadata or snapshots are off")
        if paths and self.snapshots is not None:
            try:
                rec.snapshot = self.snapshots.take(self.session_id, call.tool, call.args, paths)
            except (SnapshotError, OSError) as e:
                if required:
                    return deny(f"snapshot failed: {e}")
                log.warning("Snapshot for %s failed, running without one: %s", call.tool, e)

        result = await self.registry.call_tool(name, call.args)
        for plugin in self.plugins:
            result = plugin.after_call(call, result)
        rec.outcome = "tool_error" if result.is_error else "ok"
        if rec.snapshot:
            result.meta = {**(result.meta or {}), "agentlab/snapshot": rec.snapshot}
        return result

    async def _ask(self, call: ToolCall, reason: str, rec: AuditRecord) -> ToolCall | CallToolResult:
        assert self.approvals is not None
        approval_id = self.approvals.create(self.session_id, call.tool, call.args, call.risk,
                                            f"[{self.client}] {reason}")
        log.warning("Approval %s pending for %s. Answer with: python -m agentlab.gateway.approvals watch",
                    approval_id, call.tool)
        answer = await self.approvals.wait(approval_id, self.approval_timeout)
        rec.approval = f"{approval_id}:{answer.status}"
        log.info("Approval %s: %s", approval_id, answer.status)
        if answer.status != "approved":
            why = f"{answer.status}: {answer.decided_reason}" if answer.decided_reason else answer.status
            return deny(f"a human did not approve this call ({why})")
        return ToolCall(call.tool, answer.decided_args if answer.decided_args is not None else call.args, call.risk)


def deny(reason: str) -> CallToolResult:
    return tool_error(f"Denied by policy: {reason}")


def write_paths(entry: ServerEntry, tool: Tool, args: dict[str, Any]) -> list[Path] | None:
    """Paths a call will change, from the server's `writes` metadata. None means unknown."""
    names = entry.writes.get(tool.name)
    if names is None and entry.trust_meta:
        names = (tool.meta or {}).get(META_WRITES)
    if not isinstance(names, list):
        return None
    base = Path.cwd()
    if isinstance(entry.target, StdioServerParameters) and entry.target.cwd:
        base = Path(entry.target.cwd)  # the downstream server resolves relative paths from its own cwd
    paths = []
    for n in names:
        value = args.get(n)
        if isinstance(value, str) and value:
            p = Path(value).expanduser()
            paths.append((p if p.is_absolute() else base / p).resolve())
    return paths
