"""Policy simulator: see what a profile decides for a tool call, without running anything.

    python -m agentlab.gateway.policy show --profile coding
    python -m agentlab.gateway.policy simulate --profile coding --tool fs__read_file --args '{"path": "~/.ssh/id_rsa"}'

`simulate` takes the tool's risk from `--risk`, or connects to the servers in `--config` to read it.
"""

import argparse
import json
import sys
from pathlib import Path

import anyio

from agentlab.gateway.config import load_servers
from agentlab.gateway.policy.engine import PolicyEngine, Profile, ToolCall, load_profiles
from agentlab.gateway.registry import Registry, tool_risk


def show(profile: Profile) -> None:
    print(f"profile: {profile.name}")
    print(f"visible: {profile.visible}" + (f"  risk: {profile.visible_risk}" if profile.visible_risk else ""))
    for section, rules in (("guards", profile.guards), ("rules", profile.rules)):
        print(f"{section}:")
        for r in rules:
            print(f"  {r.source:<22} {r.action:<20} {json.dumps(r.match)}")
    print("  (no match)             deny")
    for b in profile.budgets:
        print(f"budget {b.pattern}: per_session={b.max_per_session} per_minute={b.max_per_minute}")


async def lookup_risk(config: Path, tool: str) -> str | None:
    async with Registry(load_servers(config)) as registry:
        resolved = registry.resolve(tool)
        if resolved is None:
            raise SystemExit(f"Tool {tool} not found on the servers in {config}")
        return tool_risk(*resolved)


def simulate(profile: Profile, tool: str, args: dict, risk: str | None) -> None:
    engine = PolicyEngine(profile)
    call = ToolCall(tool, args, risk)
    visible = engine.is_visible(tool, risk)
    decision = engine.evaluate(call)
    print(f"tool:     {tool}  (risk: {risk or 'unknown'})")
    print(f"visible:  {'yes' if visible else 'no, the client never sees this tool'}")
    print(f"decision: {decision.action if visible else 'deny'}")
    print(f"rule:     {decision.rule.source if decision.rule else '(none)'}")
    print(f"reason:   {decision.reason}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m agentlab.gateway.policy", description=__doc__.split("\n")[0])
    parser.add_argument("--profiles", type=Path, default=Path("config/profiles"))
    sub = parser.add_subparsers(dest="command", required=True)
    p_show = sub.add_parser("show", help="print a profile's effective rules")
    p_show.add_argument("--profile", required=True)
    p_sim = sub.add_parser("simulate", help="decide one tool call")
    p_sim.add_argument("--profile", required=True)
    p_sim.add_argument("--tool", required=True)
    p_sim.add_argument("--args", default="{}", help="tool arguments as JSON")
    p_sim.add_argument("--risk", choices=["low", "medium", "high", "critical"])
    p_sim.add_argument("--config", type=Path, help="servers.yaml, to read the tool's real risk")
    a = parser.parse_args()

    profiles = load_profiles(a.profiles)
    if a.profile not in profiles:
        sys.exit(f"Unknown profile {a.profile!r}. Available: {', '.join(sorted(profiles))}")
    profile = profiles[a.profile]
    if a.command == "show":
        show(profile)
        return
    risk = a.risk
    if risk is None and a.config:
        risk = anyio.run(lookup_risk, a.config, a.tool)
    simulate(profile, a.tool, json.loads(a.args), risk)


if __name__ == "__main__":
    main()
