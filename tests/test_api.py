import json
import socket
import anyio
import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import InvalidStatus

from agentlab.gateway.api import AdminState, build_full_app
from agentlab.gateway.approvals import ApprovalStore
from agentlab.gateway.audit import AuditLog
from agentlab.gateway.clients import ClientIdentity, Clients, sha256
from agentlab.gateway.http import build_mcp
from agentlab.gateway.policy.engine import Profile, Rule
from agentlab.gateway.registry import Registry, ServerEntry
from agentlab.gateway.snapshots import SnapshotStore
from agentlab.recorder.store import RecorderStore
from agentlab.shared.trace import TraceWriter
from agentlab.servers import fs

ADMIN = "admin-secret"
WRITES = {"write_file": ["path"], "delete": ["path"]}
PROFILE = Profile("p", ["*"], None, [], [
    Rule({"tool": "fs__delete"}, "ask", "p.rules[0]"),
    Rule({}, "allow", "p.rules[1]"),
], [])


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def gateway(tmp_path, agentlab_home):
    port = free_port()
    clients = Clients([ClientIdentity("agent", "p", sha256("tok"))], ["http://127.0.0.1:5173"])
    stores = dict(approvals=ApprovalStore(agentlab_home), snapshots=SnapshotStore(agentlab_home),
                  audit=AuditLog(agentlab_home))
    async with Registry([ServerEntry("fs", fs.create_server(roots=[tmp_path]), writes=WRITES)]) as registry:
        mcp_app, mcp_server, gateways = build_mcp(registry, {"p": PROFILE}, clients, port=port,
                                                  approval_timeout=10, **stores)
        state = AdminState(registry, {"p": PROFILE}, clients, gateways, token=ADMIN, port=port,
                           recorder=RecorderStore(agentlab_home), **stores)
        app = build_full_app(state, mcp_app, mcp_server)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on",
                                                    timeout_graceful_shutdown=2))
        async with anyio.create_task_group() as tg:
            tg.start_soon(server.serve)
            while not server.started:
                await anyio.sleep(0.05)
            base = f"127.0.0.1:{port}"
            async with httpx2.AsyncClient(base_url=f"http://{base}", headers={"Authorization": f"Bearer {ADMIN}"}) as api:
                yield base, api, tmp_path
            server.should_exit = True


def mcp_client(base):
    http = httpx2.AsyncClient(headers={"Authorization": "Bearer tok"})
    return Client(streamable_http_client(f"http://{base}/mcp", http_client=http))


async def test_admin_token_required(gateway):
    base, _, _ = gateway
    async with httpx2.AsyncClient() as anon:
        assert (await anon.get(f"http://{base}/api/overview")).status_code == 401
        bad = await anon.get(f"http://{base}/api/overview", headers={"Authorization": "Bearer nope"})
        assert bad.status_code == 401
        rebound = await anon.get(f"http://{base}/api/overview",
                                 headers={"Authorization": f"Bearer {ADMIN}", "Host": "evil.example"})
        assert rebound.status_code == 400


async def test_read_endpoints(gateway):
    _, api, root = gateway
    overview = (await api.get("/api/overview")).json()
    assert overview["clients"][0]["name"] == "agent"
    assert overview["servers"] == {"total": 1, "healthy": 1}
    [server] = (await api.get("/api/servers")).json()
    tools = {t["name"]: t for t in server["tools"]}
    assert tools["fs__delete"]["risk"] == "high" and tools["fs__read_file"]["risk"] == "low"
    [profile] = (await api.get("/api/profiles")).json()
    assert [r["source"] for r in profile["rules"]] == ["p.rules[0]", "p.rules[1]"]
    sim = (await api.post("/api/simulate", json={"profile": "p", "tool": "fs__delete", "args": {"path": str(root)}})).json()
    assert sim == {"tool_exists": True, "risk": "high", "visible": True, "action": "ask",
                   "rule": "p.rules[0]", "reason": "matched p.rules[0]"}


async def approve_from_ui(api):
    while not (pending := (await api.get("/api/approvals")).json()):
        await anyio.sleep(0.1)
    r = await api.post(f"/api/approvals/{pending[0]['id']}", json={"decision": "approve"})
    assert r.status_code == 200
    again = await api.post(f"/api/approvals/{pending[0]['id']}", json={"decision": "deny"})
    assert again.status_code == 409


async def test_approve_in_ui_then_roll_back(gateway):
    base, api, root = gateway
    f = root / "a.txt"
    f.write_text("keep me", encoding="utf-8")
    async with mcp_client(base) as client, anyio.create_task_group() as tg:
        tg.start_soon(approve_from_ui, api)
        result = await client.call_tool("fs__delete", {"path": str(f)})
    assert not result.is_error and not f.exists()

    rows = (await api.get("/api/audit", params={"tool": "fs__*"})).json()
    assert rows[0]["decision"] == "ask" and rows[0]["approval"].endswith(":approved")
    [snap] = (await api.get("/api/snapshots")).json()
    assert snap["tool"] == "fs__delete"
    assert (await api.post(f"/api/snapshots/{snap['id']}/rollback")).status_code == 200
    assert f.read_text(encoding="utf-8") == "keep me"
    assert (await api.post(f"/api/snapshots/{snap['id']}/rollback")).status_code == 409


async def test_websocket_pushes_approvals_and_audit(gateway):
    base, api, root = gateway
    messages: list[dict] = []

    async def listen():
        async with ws_connect(f"ws://{base}/api/ws?token={ADMIN}", origin=f"http://{base}") as ws:
            async for raw in ws:
                messages.append(json.loads(raw))
                if any(m["type"] == "audit" for m in messages):
                    return

    async def approve_once_seen():
        # Answer only after the live feed showed the request, as a person in the UI would.
        while not any(m["type"] == "approvals" and m["items"] for m in messages):
            await anyio.sleep(0.05)
        await approve_from_ui(api)

    async with mcp_client(base) as client, anyio.create_task_group() as tg:
        tg.start_soon(listen)
        tg.start_soon(approve_once_seen)
        await client.call_tool("fs__delete", {"path": str(root / "missing.txt")})
    kinds = [m["type"] for m in messages]
    assert "approvals" in kinds and "audit" in kinds
    assert any(m["type"] == "approvals" and m["items"] and m["items"][0]["tool"] == "fs__delete" for m in messages)


async def test_websocket_rejects_foreign_origin_and_bad_token(gateway):
    base, _, _ = gateway
    for url, origin in ((f"ws://{base}/api/ws?token={ADMIN}", "https://evil.example"),
                        (f"ws://{base}/api/ws?token=nope", f"http://{base}")):
        with pytest.raises(InvalidStatus):
            async with ws_connect(url, origin=origin) as ws:
                await ws.recv()


async def test_mcp_still_served_through_router(gateway):
    base, _, _ = gateway
    async with mcp_client(base) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert "fs__read_file" in names


async def test_diff_and_goldens_endpoints(gateway, agentlab_home):
    _, api, _ = gateway
    w = TraceWriter("g1", agentlab_home / "traces")
    w.emit("run_start", {"message": "stat it", "model": "x"})
    w.emit("llm_call", {"response": {"role": "ai", "tool_calls": [{"name": "fs__stat", "args": {}}]}})
    w.emit("tool_call", {"tool": "fs__stat", "args": {}}, parent_step=2)
    w.emit("tool_result", {"content": "ok", "is_error": False}, parent_step=3)
    w.emit("llm_call", {"response": {"role": "ai", "content": "done"}})
    w.emit("run_end", {"answer": "done"})

    d = (await api.get("/api/runs/g1/diff/g1")).json()
    assert d["same_tool_path"] and all(r["status"] == "same" for r in d["rows"])
    assert (await api.get("/api/runs/g1/diff/nope")).status_code == 404

    gid = (await api.post("/api/goldens", json={"run_id": "g1", "label": "stat"})).json()["id"]
    [g] = (await api.get("/api/goldens")).json()
    assert g["id"] == gid and g["expected"]["tool_path"]
    assert (await api.delete(f"/api/goldens/{gid}")).status_code == 200
    assert (await api.get("/api/goldens")).json() == []


async def test_runs_endpoints(gateway, agentlab_home):
    _, api, _ = gateway
    w = TraceWriter("run1", agentlab_home / "traces")
    w.emit("run_start", {"message": "hello", "model": "m"})
    w.emit("tool_call", {"tool": "fs__stat", "args": {}}, parent_step=1)
    w.emit("run_end", {"answer": "bye"})
    [run] = (await api.get("/api/runs")).json()
    assert run["run_id"] == "run1" and run["status"] == "ok"
    detail = (await api.get("/api/runs/run1")).json()
    assert [e["type"] for e in detail["events"]] == ["run_start", "tool_call", "run_end"]
    assert detail["events"][1]["gateway"] is None  # no audit row for this fake run
    assert (await api.get("/api/runs/nope")).status_code == 404


async def test_arena_run_starts_in_background_and_rejects_overlap(gateway, monkeypatch):
    import agentlab.arena.runner as runner

    release = anyio.Event()

    async def fake_matrix(state, attacks, defenses, trials=1):
        state.running, state.total = True, len(attacks) * len(defenses)
        await release.wait()
        state.done, state.running = state.total, False

    monkeypatch.setattr(runner, "run_matrix_into", fake_matrix)
    _, api, _ = gateway
    started = await api.post("/api/arena/run", json={"scenario": "inbox", "defenses": ["none"]})
    assert started.status_code == 200 and started.json()["started"]
    assert (await api.post("/api/arena/run", json={})).status_code == 409  # one matrix at a time

    release.set()
    for _ in range(50):
        status = (await api.get("/api/arena/status")).json()
        if not status["running"]:
            break
        await anyio.sleep(0.05)
    assert not status["running"] and status["done"] == status["total"] > 0
    assert (await api.post("/api/arena/run", json={"scenario": "nope"})).status_code == 404
