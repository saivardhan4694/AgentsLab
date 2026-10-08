import sys

import pytest
from mcp import Client

from agentlab.servers.system import create_server


@pytest.fixture
async def client():
    async with Client(create_server()) as c:
        yield c


async def test_all_tools_read_only(client):
    tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == {"os_info", "resource_usage", "disk_usage", "installed_apps",
                                       "env_var_names", "which"}
    assert all(t.annotations.read_only_hint for t in tools)


async def test_os_info(client):
    info = (await client.call_tool("os_info", {})).structured_content
    assert info["cpu_cores_logical"] >= 1
    assert info["memory_total_gb"] > 0


async def test_disk_usage(client):
    disks = (await client.call_tool("disk_usage", {})).structured_content["disks"]
    assert disks and all(d["total_gb"] > 0 for d in disks)


async def test_which_finds_python(client):
    exe = "python" if sys.platform == "win32" else "python3"
    result = (await client.call_tool("which", {"command": exe})).structured_content
    assert result["command"] == exe
    missing = (await client.call_tool("which", {"command": "definitely-not-a-real-cmd"})).structured_content
    assert missing["found"] is False


async def test_env_values_only_for_safe_vars(client, monkeypatch):
    monkeypatch.setenv("AGENTLAB_TEST_SECRET", "hunter2")
    result = (await client.call_tool("env_var_names", {})).structured_content
    assert "AGENTLAB_TEST_SECRET" in result["names"]
    assert "hunter2" not in result["safe_values"].values()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows registry")
async def test_installed_apps(client):
    result = (await client.call_tool("installed_apps", {})).structured_content
    assert result["count"] == len(result["apps"]) > 0
