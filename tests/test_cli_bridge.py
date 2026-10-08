import sys
from pathlib import Path

import pytest
from mcp import Client

from agentlab.cli_bridge import manifest as manifest_mod
from agentlab.cli_bridge.manifest import ArgumentError, ManifestError, load_manifest, load_manifests
from agentlab.cli_bridge.server import create_server
from agentlab.gateway.pipeline import Gateway, write_paths
from agentlab.gateway.policy.engine import PolicyEngine, Profile, Rule
from agentlab.gateway.registry import Registry, ServerEntry, tool_risk
from agentlab.gateway.server import create_server as gateway_server
from agentlab.gateway.snapshots import SnapshotStore

PY = Path(sys.executable).as_posix()
TEMPLATES = Path(manifest_mod.__file__).parent / "templates"
ECHO = "import json, sys; print(json.dumps(sys.argv[1:]))"
WRITE = "import sys, pathlib; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])"

MANIFEST = f"""
name: py
binary: "{PY}"
tools:
  - name: echo
    description: Print the arguments
    risk: low
    args:
      text:   {{ type: string }}
      count:  {{ type: integer, default: 1, min: 1, max: 5 }}
      loud:   {{ type: boolean, default: false }}
      target: {{ type: path, required: false }}
    argv: ["-c", "{ECHO}", "{{text}}", "n={{count}}", {{when: loud, then: ["LOUD"], else: ["quiet"]}},
           {{when: target, then: ["--", "{{target}}"]}}]
  - name: write
    risk: medium
    args:
      path:    {{ type: path }}
      content: {{ type: string, allow_dash: true }}
    argv: ["-c", "{WRITE}", "{{path}}", "{{content}}"]
    writes: [path]
"""


@pytest.fixture
def manifests(tmp_path):
    folder = tmp_path / "tools"
    folder.mkdir()
    (folder / "py.yaml").write_text(MANIFEST, encoding="utf-8")
    return folder


def tool(manifests, name):
    return {t.name: t for t in load_manifest(manifests / "py.yaml")}[name]


def test_render_with_defaults_and_blocks(manifests, tmp_path):
    echo = tool(manifests, "py_echo")
    argv = echo.render(echo.bind({"text": "hi"}))
    assert argv[0] == str(Path(sys.executable))
    assert argv[3:] == ["hi", "n=1", "quiet"]
    argv = echo.render(echo.bind({"text": "hi", "loud": True, "target": str(tmp_path / ".." / "x")}))
    assert argv[-3:] == ["LOUD", "--", str((tmp_path.parent / "x").resolve())]


@pytest.mark.parametrize(("args", "error"), [
    ({"text": "--upload-pack=evil"}, "may not start with '-'"),
    ({"text": "hi", "count": 9}, "between"),
    ({"text": "hi", "count": "2"}, "integer"),
    ({"text": "hi", "loud": "yes"}, "true or false"),
    ({"text": "hi", "extra": 1}, "Unknown arguments"),
    ({}, "Missing argument: text"),
])
def test_bad_arguments(manifests, args, error):
    echo = tool(manifests, "py_echo")
    with pytest.raises(ArgumentError, match=error):
        echo.bind(args)


@pytest.mark.parametrize(("tool_yaml", "error"), [
    ('{name: t, args: {o: {type: string, required: false}}, argv: ["{o}"]}', "inside a 'when: o' block"),
    ('{name: t, argv: ["{nope}"]}', "not a declared argument"),
    ('{name: t, args: {s: {type: string}}, argv: [], writes: [s]}', "path argument"),
    ('{name: t, risk: scary, argv: []}', "risk"),
    ('{name: t, argv: [{when: x, then: []}]}', "block"),
    ('{name: t, args: {a: {type: string, colour: red}}, argv: []}', "unknown keys"),
])
def test_bad_manifests(tmp_path, tool_yaml, error):
    (tmp_path / "m.yaml").write_text(f'binary: "{PY}"\ntools: [{tool_yaml}]\n', encoding="utf-8")
    with pytest.raises(ManifestError, match=error):
        load_manifest(tmp_path / "m.yaml")


def test_batch_files_are_refused(tmp_path):
    bat = tmp_path / "tool.cmd"
    bat.write_text("@echo off", encoding="utf-8")
    (tmp_path / "m.yaml").write_text(f'binary: "{bat.as_posix()}"\ntools: []\n', encoding="utf-8")
    with pytest.raises(ManifestError, match="batch file"):
        load_manifest(tmp_path / "m.yaml")


def test_missing_binary_is_skipped(tmp_path):
    (tmp_path / "m.yaml").write_text("binary: no-such-program-xyz\ntools: []\n", encoding="utf-8")
    tools, notes = load_manifests(tmp_path)
    assert tools == [] and "not found" in notes[0]


def test_all_templates_are_valid(monkeypatch):
    monkeypatch.setattr(manifest_mod, "resolve_binary", lambda b: Path(sys.executable))
    for template in TEMPLATES.glob("*.yaml"):
        assert load_manifest(template), template.name


async def test_server_runs_without_a_shell(manifests, tmp_path):
    tools, _ = load_manifests(manifests)
    async with Client(create_server(tools, roots=[tmp_path])) as client:
        listed = {t.name: t for t in (await client.list_tools()).tools}
        result = await client.call_tool("py_echo", {"text": "a; rm -rf / & echo $(whoami)", "loud": True})
        outside = await client.call_tool("py_echo", {"text": "x", "target": str(Path.home())})
    assert listed["py_echo"].annotations.read_only_hint is True
    assert listed["py_write"].meta == {"agentlab/risk": "medium", "agentlab/writes": ["path"]}
    assert not result.is_error
    # The whole value arrives as one argument; nothing was interpreted.
    assert result.structured_content["stdout"].strip() == '["a; rm -rf / & echo $(whoami)", "n=1", "LOUD"]'
    assert outside.is_error and "outside the allowed roots" in outside.content[0].text


async def test_gateway_uses_trusted_meta_for_risk_and_snapshots(manifests, tmp_path, agentlab_home):
    tools, _ = load_manifests(manifests)
    target = tmp_path / "out.txt"
    target.write_text("old", encoding="utf-8")
    trusted = ServerEntry("cli", create_server(tools, roots=[tmp_path]), trust_meta=True)
    untrusted = ServerEntry("other", create_server(tools, roots=[tmp_path]))
    profile = Profile("p", ["*"], None, [], [Rule({"risk": "medium"}, "allow_with_snapshot", "r")], [])
    snapshots = SnapshotStore(agentlab_home)
    async with Registry([trusted, untrusted]) as registry:
        _, write_tool = registry.resolve("cli__py_write")
        assert tool_risk(trusted, write_tool) == "medium"
        assert tool_risk(untrusted, write_tool) == "medium"  # from annotations (destructive_hint=False)
        assert write_paths(untrusted, write_tool, {"path": "x"}) is None  # meta not trusted
        gw = Gateway(registry, PolicyEngine(profile), snapshots=snapshots)
        async with Client(gateway_server(gw)) as client:
            ok = await client.call_tool("cli__py_write", {"path": str(target), "content": "new"})
            no_meta = await client.call_tool("other__py_write", {"path": str(target), "content": "x"})
    assert not ok.is_error and target.read_text(encoding="utf-8") == "new"
    assert no_meta.is_error and "writes" in no_meta.content[0].text
    snapshots.rollback(ok.meta["agentlab/snapshot"])
    assert target.read_text(encoding="utf-8") == "old"
