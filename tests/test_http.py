import socket

import anyio
import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from agentlab.gateway.audit import AuditLog
from agentlab.gateway.clients import ClientIdentity, Clients, load_clients, sha256
from agentlab.gateway.http import build_app
from agentlab.gateway.policy.engine import Profile, Rule
from agentlab.gateway.registry import Registry, ServerEntry
from agentlab.servers import fs, system

ALLOW = Profile("allow", ["*"], None, [], [Rule({}, "allow", "r")], [])
READ_SYSTEM = Profile("system-only", ["system__*"], None, [], [Rule({}, "allow", "r")], [])


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def http_gateway(tmp_path, agentlab_home):
    port = free_port()
    clients = Clients(
        [ClientIdentity("alice", "allow", sha256("tok-alice")), ClientIdentity("bob", "system-only", sha256("tok-bob"))],
        allowed_origins=["http://127.0.0.1:5173"],
    )
    audit = AuditLog(agentlab_home)
    entries = [ServerEntry("fs", fs.create_server(roots=[tmp_path])), ServerEntry("system", system.create_server())]
    async with Registry(entries) as registry:
        app = build_app(registry, {"allow": ALLOW, "system-only": READ_SYSTEM}, clients, port=port, audit=audit)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
        async with anyio.create_task_group() as tg:
            tg.start_soon(server.serve)
            while not server.started:
                await anyio.sleep(0.05)
            yield f"http://127.0.0.1:{port}/mcp", audit
            server.should_exit = True


def connect(url, token=None, **headers):
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return Client(streamable_http_client(url, http_client=httpx2.AsyncClient(headers=headers)))


async def test_token_selects_client_and_profile(http_gateway):
    url, audit = http_gateway
    async with connect(url, "tok-alice") as alice:
        a_tools = {t.name for t in (await alice.list_tools()).tools}
        assert not (await alice.call_tool("system__os_info", {})).is_error
    async with connect(url, "tok-bob") as bob:
        b_tools = {t.name for t in (await bob.list_tools()).tools}
        denied = await bob.call_tool("fs__list_dir", {"path": "."})
    assert "fs__list_dir" in a_tools
    assert "fs__list_dir" not in b_tools and "system__os_info" in b_tools
    assert denied.is_error
    rows = audit.query()
    assert {(r["client"], r["tool"], r["decision"]) for r in rows} >= {
        ("alice", "system__os_info", "allow"), ("bob", "fs__list_dir", "unknown_tool")}


async def test_missing_or_wrong_token_is_rejected(http_gateway):
    url, _ = http_gateway
    async with httpx2.AsyncClient() as http:
        for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic tok-alice"}):
            r = await http.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=headers)
            assert r.status_code == 401


async def test_foreign_origin_is_rejected(http_gateway):
    url, _ = http_gateway
    async with httpx2.AsyncClient() as http:
        headers = {"Authorization": "Bearer tok-alice", "Origin": "https://evil.example",
                   "Accept": "application/json, text/event-stream"}
        r = await http.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=headers)
    assert r.status_code == 403


async def test_wrong_host_is_rejected(http_gateway):
    # DNS rebinding: a browser reaches 127.0.0.1 through an attacker's domain name.
    url, _ = http_gateway
    async with httpx2.AsyncClient() as http:
        headers = {"Authorization": "Bearer tok-alice", "Host": "evil.example",
                   "Accept": "application/json, text/event-stream"}
        r = await http.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=headers)
    assert r.status_code == 421


def test_load_clients(tmp_path, monkeypatch):
    monkeypatch.setenv("AL_TEST_TOKEN", "secret")
    cfg = tmp_path / "clients.yaml"
    cfg.write_text(
        "clients:\n"
        "  a: { token_env: AL_TEST_TOKEN, profile: coding }\n"
        "  b: { token_env: AL_TEST_UNSET_VAR, profile: coding }\n"
        f"  c: {{ token_sha256: {sha256('other')}, profile: readonly }}\n",
        encoding="utf-8",
    )
    clients = load_clients(cfg)
    assert [c.name for c in clients.clients] == ["a", "c"]  # b has no token, so it is disabled
    assert clients.identify("Bearer secret").name == "a"
    assert clients.identify("bearer other").name == "c"
    assert clients.identify("Bearer wrong") is None
    assert clients.identify(None) is None


def test_audit_shortens_long_args(agentlab_home):
    from agentlab.gateway.audit import AuditRecord
    log = AuditLog(agentlab_home)
    log.write(AuditRecord(tool="fs__write_file", args={"content": "x" * 5000}, decision="allow"))
    [row] = log.query(tool="fs__*")
    assert len(row["args_json"]) < 700
