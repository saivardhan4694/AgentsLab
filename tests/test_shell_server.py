import sys

import pytest
from mcp import Client

from agentlab.servers.shell import create_server

SHELL = "powershell" if sys.platform == "win32" else "bash"


@pytest.fixture
async def shell(tmp_path):
    async with Client(create_server(roots=[tmp_path])) as client:
        yield client, tmp_path


async def run(client, command, **kwargs):
    result = await client.call_tool("run_command", {"command": command, **kwargs})
    return result if result.is_error else result.structured_content


async def test_annotations(shell):
    client, _ = shell
    [tool] = (await client.list_tools()).tools
    assert tool.annotations.destructive_hint is True
    assert tool.annotations.open_world_hint is True


async def test_runs_in_root_by_default(shell):
    client, root = shell
    out = await run(client, "echo hello")
    assert out["exit_code"] == 0
    assert "hello" in out["stdout"]
    assert out["cwd"] == str(root)


async def test_exit_code_and_stderr(shell):
    client, _ = shell
    cmd = "[Console]::Error.WriteLine('bad'); exit 3" if SHELL == "powershell" else "echo bad >&2; exit 3"
    out = await run(client, cmd)
    assert out["exit_code"] == 3
    assert "bad" in out["stderr"]


async def test_cwd_jail(shell, tmp_path_factory):
    client, root = shell
    (root / "sub").mkdir()
    assert (await run(client, "echo ok", cwd=str(root / "sub")))["exit_code"] == 0
    outside = tmp_path_factory.mktemp("outside")
    result = await run(client, "echo ok", cwd=str(outside))
    assert result.is_error and "outside the allowed roots" in result.content[0].text
    escape = await run(client, "echo ok", cwd=str(root / "sub" / ".." / ".."))
    assert escape.is_error


async def test_secrets_not_in_environment(shell, monkeypatch):
    client, _ = shell
    monkeypatch.setenv("AGENTLAB_TEST_TOKEN", "hunter2")
    cmd = "echo $env:AGENTLAB_TEST_TOKEN" if SHELL == "powershell" else "echo $AGENTLAB_TEST_TOKEN"
    out = await run(client, cmd)
    assert "hunter2" not in out["stdout"]


async def test_timeout_kills_process(shell):
    client, _ = shell
    cmd = "Start-Sleep -Seconds 30" if SHELL == "powershell" else "sleep 30"
    out = await run(client, cmd, timeout_s=2)
    assert out["timed_out"] is True
    assert out["duration_ms"] < 15_000


async def test_output_is_capped(shell):
    client, _ = shell
    cmd = "'x' * 300000" if SHELL == "powershell" else "head -c 300000 /dev/zero | tr '\\0' x"
    out = await run(client, cmd)
    assert out["truncated"] is True
    assert len(out["stdout"]) == 100_000
