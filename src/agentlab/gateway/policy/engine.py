"""Policy engine: profiles, visibility, rules, budgets.

A profile is one YAML file in the profiles folder; its name is the file name.

```yaml
extends: base                     # or a list; parents are merged in order
visible: ["fs__*", "system__*"]   # tools in tools/list; default ["*"]
visible_risk: [low, medium]       # optional: also hide tools above these risk levels
guards:                           # checked first; a child profile cannot override a parent's guards
  - match: { any_arg: ["~/.ssh/**", "**/.env"] }
    action: deny
    reason: secrets
rules:                            # checked after guards; child rules come before parent rules
  - match: { tool: "fs__read_file", args: { path: "~/code/**" } }
    action: allow
  - match: { risk: [high, critical] }
    action: ask
budgets:
  fs__delete: { max_per_session: 20 }
  "*":        { max_per_minute: 60 }
plugins: [sanitizer, spotlighting]   # result filters from gateway/plugins.py; union across the extends chain
```

Match keys (all given keys must match): `tool` (glob or list), `risk` (level or list),
`args` (argument name to matcher, see matching.py), `any_arg` (matcher tried on every string argument).
The first matching guard or rule wins. No match means deny.

`plugins` names Gateway plugins (`gateway/plugins.py`'s `PLUGINS`) to run for every call under this
profile: the same `before_call`/`after_call` hooks the Arena uses to test defenses, now available to
real sessions. Unknown names are only caught when `gateway/plugins.build_plugins` builds them (at
Gateway construction), not at profile load, to avoid a load-time dependency on that module.
"""

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agentlab.gateway.policy.matching import arg_matches, name_matches

ACTIONS = ("allow", "deny", "ask", "dry_run", "allow_with_snapshot")
RISKS = ("low", "medium", "high", "critical")
MATCH_KEYS = {"tool", "risk", "args", "any_arg"}
EXECUTING = {"allow", "allow_with_snapshot", "ask"}  # actions that may run the tool, so they use budget


@dataclass(frozen=True)
class ToolCall:
    tool: str
    args: dict[str, Any]
    risk: str | None = None


@dataclass(frozen=True)
class Rule:
    match: dict[str, Any]
    action: str
    source: str  # where the rule came from, for example "coding.rules[2]"
    reason: str | None = None

    def matches(self, call: ToolCall) -> bool:
        m = self.match
        if "tool" in m and not name_matches(m["tool"], call.tool):
            return False
        if "risk" in m:
            risks = [m["risk"]] if isinstance(m["risk"], str) else m["risk"]
            if call.risk not in risks:
                return False
        for name, matcher in (m.get("args") or {}).items():
            if name not in call.args or not arg_matches(matcher, call.args[name]):
                return False
        if "any_arg" in m:
            values = [v for v in call.args.values() if isinstance(v, str)]
            if not any(arg_matches(m["any_arg"], v) for v in values):
                return False
        return True


@dataclass(frozen=True)
class Budget:
    pattern: str
    max_per_session: int | None = None
    max_per_minute: int | None = None


@dataclass
class Profile:
    name: str
    visible: list[str]
    visible_risk: list[str] | None
    guards: list[Rule]
    rules: list[Rule]
    budgets: list[Budget]
    plugins: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    rule: Rule | None = None


@dataclass
class PolicyEngine:
    """Decides tool calls for one client session under one profile. Holds the budget counters."""

    profile: Profile
    clock: Any = time.monotonic
    _session_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    _recent: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque))

    def is_visible(self, tool: str, risk: str | None = None) -> bool:
        if self.profile.visible_risk is not None and risk not in self.profile.visible_risk:
            return False
        return name_matches(self.profile.visible, tool)

    def evaluate(self, call: ToolCall) -> Decision:
        """Pure rule lookup: no visibility check and no budget use. The simulator uses this."""
        for rule in [*self.profile.guards, *self.profile.rules]:
            if rule.matches(call):
                return Decision(rule.action, rule.reason or f"matched {rule.source}", rule)
        return Decision("deny", "no rule matched (default deny)")

    def decide(self, call: ToolCall) -> Decision:
        if not self.is_visible(call.tool, call.risk):
            return Decision("deny", f"tool is not visible in profile {self.profile.name}")
        decision = self.evaluate(call)
        if decision.action in EXECUTING:
            exceeded = self._use_budget(call.tool)
            if exceeded:
                return Decision("deny", exceeded, decision.rule)
        return decision

    def _use_budget(self, tool: str) -> str | None:
        now = self.clock()
        budgets = [b for b in self.profile.budgets if name_matches(b.pattern, tool)]
        for b in budgets:
            recent = self._recent[b.pattern]
            while recent and now - recent[0] >= 60:
                recent.popleft()
            if b.max_per_session is not None and self._session_counts[b.pattern] >= b.max_per_session:
                return f"budget {b.pattern!r}: max {b.max_per_session} calls per session"
            if b.max_per_minute is not None and len(recent) >= b.max_per_minute:
                return f"budget {b.pattern!r}: max {b.max_per_minute} calls per minute"
        for b in budgets:
            self._session_counts[b.pattern] += 1
            self._recent[b.pattern].append(now)
        return None


# Loading


def load_profiles(folder: Path) -> dict[str, Profile]:
    raw = {p.stem: (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) for p in sorted(folder.glob("*.yaml"))}
    resolved: dict[str, Profile] = {}

    def build(name: str, chain: tuple[str, ...]) -> Profile:
        if name in resolved:
            return resolved[name]
        if name in chain:
            raise ValueError(f"Profile cycle: {' -> '.join([*chain, name])}")
        if name not in raw:
            raise ValueError(f"Unknown profile {name!r}" + (f" (extended by {chain[-1]})" if chain else ""))
        data = raw[name]
        unknown = set(data) - {"extends", "visible", "visible_risk", "guards", "rules", "budgets", "plugins"}
        if unknown:
            raise ValueError(f"Profile {name}: unknown keys {sorted(unknown)}")
        parents_names = data.get("extends") or []
        parents = [build(p, (*chain, name)) for p in ([parents_names] if isinstance(parents_names, str) else parents_names)]

        visible = data.get("visible")
        if visible is None:
            visible = [v for p in parents for v in p.visible] if parents else ["*"]
        visible_risk = data.get("visible_risk")
        if visible_risk is None:
            visible_risk = next((p.visible_risk for p in parents if p.visible_risk is not None), None)
        elif isinstance(visible_risk, str):
            visible_risk = [visible_risk]
        if visible_risk is not None and any(r not in RISKS for r in visible_risk):
            raise ValueError(f"Profile {name}: visible_risk must be among {RISKS}")
        budgets = {b.pattern: b for p in parents for b in p.budgets}
        for pattern, spec in (data.get("budgets") or {}).items():
            budgets[pattern] = Budget(pattern, spec.get("max_per_session"), spec.get("max_per_minute"))
        # Union, order preserved: a child adds plugins on top of its parents', never removes one.
        plugins = list(dict.fromkeys([p for parent in parents for p in parent.plugins] + list(data.get("plugins") or [])))

        profile = Profile(
            name=name,
            visible=[visible] if isinstance(visible, str) else list(visible),
            visible_risk=visible_risk,
            guards=_dedupe([g for p in parents for g in p.guards] + _rules(name, "guards", data.get("guards"))),
            rules=_dedupe(_rules(name, "rules", data.get("rules")) + [r for p in parents for r in p.rules]),
            budgets=list(budgets.values()),
            plugins=plugins,
        )
        resolved[name] = profile
        return profile

    for name in raw:
        build(name, ())
    return resolved


def _dedupe(rules: list[Rule]) -> list[Rule]:
    # A diamond (two parents sharing a grandparent) would list the shared rules twice.
    seen: set[str] = set()
    return [r for r in rules if not (r.source in seen or seen.add(r.source))]


def _rules(profile: str, section: str, items: list[dict[str, Any]] | None) -> list[Rule]:
    rules = []
    for i, item in enumerate(items or []):
        source = f"{profile}.{section}[{i}]"
        match = item.get("match") or {}
        action = item.get("action")
        if action not in ACTIONS:
            raise ValueError(f"{source}: action must be one of {ACTIONS}, got {action!r}")
        if set(match) - MATCH_KEYS:
            raise ValueError(f"{source}: unknown match keys {sorted(set(match) - MATCH_KEYS)}")
        risks = match.get("risk", [])
        if any(r not in RISKS for r in ([risks] if isinstance(risks, str) else risks)):
            raise ValueError(f"{source}: risk must be one of {RISKS}")
        rules.append(Rule(match=match, action=action, source=source, reason=item.get("reason")))
    return rules
