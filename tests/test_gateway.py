import sys
from pathlib import Path

from mcp import Client, StdioServerParameters

from agentlab.gateway.config import load_servers
from agentlab.gateway.pipeline import Gateway
from agentlab.gateway.policy.engine import PolicyEngine, Profile, Rule
from agentlab.gateway.registry import Registry, ServerEntry
from agentlab.gateway.server import create_server
from agentlab.servers import fs, system

REPO = Path(__file__).parents[1]


ALLOW_ALL = Profile("allow-all", visible=["*"], visible_risk=None, guards=[],
                    rules=[Rule(match={}, action="allow", source="test")], budgets=[])


def gateway(registry, profile=ALLOW_ALL, **kwargs):
    return Client(create_server(Gateway(registry, PolicyEngine(profile), **kwargs)))


def entries(tmp_path):
    return [
        ServerEntry("fs", fs.create_server(roots=[tmp_path])),
        ServerEntry("system", system.create_server()),
    ]


async def test_tools_are_merged_and_namespaced(tmp_path):
    async with Registry(entries(tmp_path)) as registry:
        async with gateway(registry) as client:
            names = {t.name for t in (await client.list_tools()).tools}
    assert {"fs__read_file", "fs__delete", "system__os_info"} <= names
    assert all("__" in n for n in names)


async def test_call_is_routed_and_annotations_kept(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    async with Registry(entries(tmp_path)) as registry:
        async with gateway(registry) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            result = await client.call_tool("fs__read_file", {"path": str(tmp_path / "a.txt")})
    assert tools["fs__delete"].annotations.destructive_hint is True
    assert not result.is_error
    assert "hello" in result.content[0].text


async def test_unknown_tool_and_downstream_error_are_tool_errors(tmp_path):
    async with Registry(entries(tmp_path)) as registry:
        async with gateway(registry) as client:
            unknown = await client.call_tool("fs__nope", {})
            no_prefix = await client.call_tool("read_file", {})
            outside = await client.call_tool("fs__read_file", {"path": str(REPO / "pyproject.toml")})
    assert unknown.is_error and "Unknown tool" in unknown.content[0].text
    assert no_prefix.is_error
    assert outside.is_error and "outside the allowed roots" in outside.content[0].text


async def test_broken_server_is_skipped(tmp_path):
    broken = ServerEntry("broken", StdioServerParameters(command=sys.executable, args=["-c", "raise SystemExit(1)"]), timeout=10)
    async with Registry([broken, *entries(tmp_path)]) as registry:
        assert registry.servers["broken"].error is not None
        names = {t.name for t in registry.list_tools()}
    assert "system__os_info" in names
    assert not any(n.startswith("broken__") for n in names)


def test_load_servers(tmp_path, monkeypatch):
    monkeypatch.setenv("AL_TEST_ROOT", str(tmp_path))
    cfg = tmp_path / "servers.yaml"
    cfg.write_text(
        "servers:\n"
        "  a: { command: python, args: [x.py, '${AL_TEST_ROOT}'], cwd: sub }\n"
        "  b: { command: python, enabled: false }\n",
        encoding="utf-8",
    )
    [a] = load_servers(cfg)
    assert a.name == "a"
    assert a.target.args == ["x.py", str(tmp_path)]
    assert Path(a.target.cwd) == (tmp_path / "sub").resolve()


async def test_gateway_over_stdio(tmp_path):
    # End to end: the Gateway as a subprocess, with real stdio downstream servers.
    cfg = tmp_path / "servers.yaml"
    cfg.write_text(
        "servers:\n"
        f"  fs: {{ command: '{Path(sys.executable).as_posix()}', args: [-m, agentlab.servers.fs, --root, '{tmp_path.as_posix()}'] }}\n"
        f"  system: {{ command: '{Path(sys.executable).as_posix()}', args: [-m, agentlab.servers.system] }}\n",
        encoding="utf-8",
    )
    params = StdioServerParameters(command=sys.executable, args=["-m", "agentlab.gateway.server", "--config", str(cfg),
                                          "--profiles", str(REPO / "config" / "profiles"), "--profile", "readonly"],
                                    env={"AGENTLAB_HOME": str(tmp_path / "home")})
    async with Client(params) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        result = await client.call_tool("system__os_info", {})
        write = await client.call_tool("fs__write_file", {"path": str(tmp_path / "x.txt"), "content": "x"})
    # readonly hides write tools, and a hidden tool looks unknown.
    assert {"fs__list_dir", "system__os_info"} <= names
    assert "fs__write_file" not in names
    assert write.is_error and "Unknown tool" in write.content[0].text
    assert not (tmp_path / "x.txt").exists()
    assert not result.is_error


async def test_policy_is_enforced(tmp_path):
    profile = Profile(
        "t", visible=["fs__*"], visible_risk=None, budgets=[],
        guards=[Rule(match={"any_arg": "**/secret.txt"}, action="deny", source="g", reason="secrets")],
        rules=[
            Rule(match={"tool": "fs__read_file"}, action="allow", source="r0"),
            Rule(match={"tool": "fs__write_file"}, action="dry_run", source="r1"),
            Rule(match={"tool": "fs__delete"}, action="ask", source="r2"),
        ],
    )
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("hunter2", encoding="utf-8")
    async with Registry(entries(tmp_path)) as registry:
        async with gateway(registry, profile) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            ok = await client.call_tool("fs__read_file", {"path": str(tmp_path / "a.txt")})
            secret = await client.call_tool("fs__read_file", {"path": str(tmp_path / "secret.txt")})
            dry = await client.call_tool("fs__write_file", {"path": str(tmp_path / "b.txt"), "content": "x"})
            ask = await client.call_tool("fs__delete", {"path": str(tmp_path / "a.txt")})
            unmatched = await client.call_tool("fs__stat", {"path": str(tmp_path)})
            hidden = await client.call_tool("system__os_info", {})
    assert not any(n.startswith("system__") for n in names)
    assert not ok.is_error
    assert secret.is_error and "secrets" in secret.content[0].text
    assert dry.is_error and "Dry run" in dry.content[0].text and not (tmp_path / "b.txt").exists()
    assert ask.is_error and "approval" in ask.content[0].text and (tmp_path / "a.txt").exists()
    assert unmatched.is_error and "default deny" in unmatched.content[0].text
    assert hidden.is_error and "Unknown tool" in hidden.content[0].text
