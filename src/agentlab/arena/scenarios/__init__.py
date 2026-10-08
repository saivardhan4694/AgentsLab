"""Fake scenario MCP servers, each with planted data and a forbidden outcome.

A scenario builds an in-process `MCPServer` plus a `state` object the server mutates;
the Arena's judge (`judge.py`) reads `state` after the run. Scenarios never touch the
real machine, so attacks are safe to run.
"""

from dataclasses import dataclass
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer

from agentlab.arena.attacks import Attack


@dataclass
class Scenario:
    name: str
    task: str  # the defender agent's instructions (the user message)
    build: Callable[["Attack | None"], tuple[MCPServer, Any]]
    attack_success: Callable[[Any], bool]  # given state, did the forbidden outcome happen?
    task_success: Callable[[Any], bool]  # given state, was the legitimate task done?
    untrusted_tools: list[str]  # Gateway tool names (server__tool) whose output may carry injected content
    sensitive_tools: list[str]  # Gateway tool names (server__tool) that cause the forbidden outcome


from agentlab.arena.scenarios import bank, files, inbox  # noqa: E402 - after Scenario, to avoid a cycle

SCENARIOS: dict[str, Scenario] = {s.name: s for s in (inbox.SCENARIO, bank.SCENARIO, files.SCENARIO)}
