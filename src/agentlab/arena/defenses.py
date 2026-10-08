"""Defenses as Gateway plugins (design.md section 5): none, sanitizer, spotlighting, tool_gating.

`none`, `sanitizer`, and `spotlighting` are generic result filters (`gateway/plugins.py`) that
any profile can also enable for real sessions. `tool_gating` is Arena-specific here because it
needs a scenario's untrusted/sensitive tool lists; see the note under milestone 10 in
progress.md about generalizing it for real profiles.

Each defense factory takes the scenario under test and returns a fresh list of `Plugin`
instances (one per run, since `ToolGatingPlugin` is stateful across the session).
"""

from collections.abc import Callable
from typing import TYPE_CHECKING

from mcp.types import CallToolResult

from agentlab.gateway.pipeline import BasePlugin, Plugin
from agentlab.gateway.plugins import NonePlugin, SanitizerPlugin, SpotlightingPlugin
from agentlab.gateway.policy.engine import ToolCall
from agentlab.gateway.registry import tool_error

if TYPE_CHECKING:
    from agentlab.arena.scenarios import Scenario


class ToolGatingPlugin(BasePlugin):
    """Disables sensitive tools for the rest of the session after any untrusted content is read."""

    name = "tool_gating"

    def __init__(self, untrusted_tools: list[str], sensitive_tools: list[str]):
        self.untrusted_tools = set(untrusted_tools)
        self.sensitive_tools = set(sensitive_tools)
        self.tainted = False

    def before_call(self, call: ToolCall) -> "ToolCall | CallToolResult":
        if self.tainted and call.tool in self.sensitive_tools:
            return tool_error(
                f"Blocked by tool_gating: {call.tool} is disabled this session because untrusted "
                "content was read earlier. Ask the user to confirm this action out of band."
            )
        return call

    def after_call(self, call: ToolCall, result: CallToolResult) -> CallToolResult:
        if call.tool in self.untrusted_tools and not result.is_error:
            self.tainted = True
        return result


DEFENSES: dict[str, Callable[["Scenario"], list[Plugin]]] = {
    "none": lambda scenario: [NonePlugin()],
    "sanitizer": lambda scenario: [SanitizerPlugin()],
    "spotlighting": lambda scenario: [SpotlightingPlugin()],
    "tool_gating": lambda scenario: [ToolGatingPlugin(scenario.untrusted_tools, scenario.sensitive_tools)],
}
