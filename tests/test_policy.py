from pathlib import Path

import pytest
from mcp.types import Tool, ToolAnnotations

from agentlab.gateway.policy.engine import PolicyEngine, ToolCall, load_profiles
from agentlab.gateway.policy.matching import arg_matches, path_matches
from agentlab.gateway.registry import ServerEntry, tool_risk

REPO = Path(__file__).parents[1]
HOME = Path.home()


def write_profiles(tmp_path, **files):
    for name, text in files.items():
        (tmp_path / f"{name}.yaml").write_text(text, encoding="utf-8")
    return load_profiles(tmp_path)


# matching

def test_path_glob_normalizes_traversal_and_tilde():
    sneaky = str(HOME / "code" / ".." / ".ssh" / "id_rsa")
    assert path_matches("~/.ssh/**", sneaky)
    assert path_matches("~/.ssh/**", str(HOME / ".ssh"))  # "dir/**" covers the dir itself
    assert not path_matches("~/.ssh/**", str(HOME / ".sshx"))
    assert path_matches("**/.env", str(HOME / "proj" / ".env"))
    assert not path_matches("**/.env", str(HOME / "proj" / ".envrc"))


def test_single_star_stays_in_one_folder():
    assert path_matches("~/code/*", str(HOME / "code" / "a.py"))
    assert not path_matches("~/code/*", str(HOME / "code" / "sub" / "a.py"))


def test_arg_matcher_forms():
    assert arg_matches({"regex": r"^git\b"}, "git status")
    assert not arg_matches({"regex": r"^git\b"}, "gitx")
    assert arg_matches({"equals": 3}, 3)
    assert arg_matches(["a*", "b*"], "bob")
    assert arg_matches("feature/*", "feature/x")  # not path-like: plain glob
    assert not arg_matches("~/x/**", 5)  # non-strings never match a glob
    with pytest.raises(ValueError):
        arg_matches({"nope": 1}, "x")


# engine and profiles

def test_first_match_wins_and_default_deny(tmp_path):
    profiles = write_profiles(tmp_path, p="""
rules:
  - match: { tool: "fs__read_file", args: { path: "~/code/**" } }
    action: allow
  - match: { tool: "fs__*" }
    action: ask
""")
    e = PolicyEngine(profiles["p"])
    assert e.decide(ToolCall("fs__read_file", {"path": "~/code/a.py"})).action == "allow"
    assert e.decide(ToolCall("fs__read_file", {"path": "~/other"})).action == "ask"
    d = e.decide(ToolCall("shell__run", {}))
    assert d.action == "deny" and "default deny" in d.reason


def test_child_cannot_override_parent_guards(tmp_path):
    profiles = write_profiles(
        tmp_path,
        base="""
guards:
  - match: { any_arg: "~/.ssh/**" }
    action: deny
rules:
  - match: { tool: "fs__*" }
    action: ask
""",
        child="""
extends: base
rules:
  - match: {}
    action: allow
""",
    )
    child = profiles["child"]
    assert [r.source for r in child.rules] == ["child.rules[0]", "base.rules[0]"]
    e = PolicyEngine(child)
    assert e.decide(ToolCall("fs__move", {"source": "~/a", "destination": "~/.ssh/x"})).action == "deny"
    assert e.decide(ToolCall("fs__read_file", {"path": "~/a"})).action == "allow"


def test_visibility_by_name_and_risk(tmp_path):
    profiles = write_profiles(tmp_path, p="""
visible: ["fs__*"]
visible_risk: [low]
rules:
  - match: {}
    action: allow
""")
    e = PolicyEngine(profiles["p"])
    assert e.is_visible("fs__read_file", "low")
    assert not e.is_visible("fs__delete", "high")
    assert not e.is_visible("system__os_info", "low")
    assert e.decide(ToolCall("fs__delete", {}, "high")).action == "deny"


def test_budgets(tmp_path):
    profiles = write_profiles(tmp_path, p="""
rules:
  - match: {}
    action: allow
budgets:
  fs__delete: { max_per_session: 2 }
  "*": { max_per_minute: 3 }
""")
    now = [0.0]
    e = PolicyEngine(profiles["p"], clock=lambda: now[0])
    actions = [e.decide(ToolCall("fs__delete", {})).action for _ in range(3)]
    assert actions == ["allow", "allow", "deny"]
    assert e.decide(ToolCall("fs__stat", {})).action == "allow"  # 3rd call this minute
    assert e.decide(ToolCall("fs__stat", {})).action == "deny"
    now[0] = 61
    assert e.decide(ToolCall("fs__stat", {})).action == "allow"


@pytest.mark.parametrize(("files", "error"), [
    ({"p": "rules: [{match: {}, action: yolo}]"}, "action"),
    ({"a": "extends: b", "b": "extends: a"}, "cycle"),
    ({"p": "rules: [{match: {path: x}, action: allow}]"}, "match keys"),
    ({"p": "extends: missing"}, "Unknown profile"),
])
def test_invalid_profiles_are_rejected(tmp_path, files, error):
    with pytest.raises(ValueError, match=error):
        write_profiles(tmp_path, **files)


def test_tool_risk():
    entry = ServerEntry("s", None, risk={"run_*": "critical"})

    def tool(name, annotations=None):
        return Tool(name=name, input_schema={"type": "object"}, annotations=annotations)

    assert tool_risk(entry, tool("run_command", ToolAnnotations(read_only_hint=True))) == "critical"
    assert tool_risk(entry, tool("read", ToolAnnotations(read_only_hint=True))) == "low"
    assert tool_risk(entry, tool("write", ToolAnnotations(destructive_hint=False))) == "medium"
    assert tool_risk(entry, tool("unknown")) == "high"


# shipped profiles

@pytest.fixture(scope="module")
def shipped():
    return load_profiles(REPO / "config" / "profiles")


def test_shipped_profiles_guard_secrets(shipped):
    for name in ("readonly", "coding", "full-trust", "dry-run"):
        e = PolicyEngine(shipped[name])
        for path in ("~/.ssh/id_ed25519", "~/repositories/app/.env", "~/agentlab-sandbox/../.aws/credentials"):
            d = e.evaluate(ToolCall("fs__read_file", {"path": path}, "low"))
            assert d.action == "deny", (name, path)


def test_coding_profile(shipped):
    e = PolicyEngine(shipped["coding"])
    sandbox, repos = "~/agentlab-sandbox", "~/repositories"
    assert e.evaluate(ToolCall("fs__read_file", {"path": "~/Documents/a.txt"}, "low")).action == "allow"
    assert e.evaluate(ToolCall("fs__write_file", {"path": f"{sandbox}/a.txt"}, "medium")).action == "allow"
    assert e.evaluate(ToolCall("fs__write_file", {"path": f"{repos}/x/a.py"}, "medium")).action == "allow_with_snapshot"
    assert e.evaluate(ToolCall("fs__write_file", {"path": "~/Desktop/a.txt"}, "medium")).action == "deny"
    # Moving a file out of the repositories folder into the sandbox is neither a sandbox move nor a repo move.
    out = e.evaluate(ToolCall("fs__move", {"source": f"{repos}/x/a.py", "destination": f"{sandbox}/a.py"}, "medium"))
    assert out.action == "deny"
    assert e.evaluate(ToolCall("fs__delete", {"path": f"{repos}/x"}, "high")).action == "ask"
