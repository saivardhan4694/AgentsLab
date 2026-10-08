"""Admin API and UI, served next to `/mcp` on the same port.

    /mcp        MCP endpoint for clients (bearer token per client, see http.py)
    /api/...    admin REST API (admin token)
    /api/ws     live approvals and audit rows (admin token as ?token=)
    /api/runs   recorded agent runs (Recorder), joined with Gateway audit rows by run_id
    /           the React UI from ui/dist, if built

The admin token comes from `AL_ADMIN_TOKEN`, or is generated once and kept in `<home>/admin_token`.
Approvals and audit rows are read from the shared SQLite files, so the UI also sees calls from
stdio Gateways that Claude Desktop starts.
"""

import hashlib
import hmac
import json
import os
import secrets
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import anyio
import httpx2
from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.lowlevel import Server
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from agentlab.gateway.approvals import ApprovalStore
from agentlab.gateway.audit import AuditLog
from agentlab.gateway.clients import Clients
from agentlab.gateway.pipeline import Gateway
from agentlab.gateway.policy.engine import PolicyEngine, Profile, Rule, ToolCall
from agentlab.gateway.registry import Registry, tool_risk
from agentlab.gateway.snapshots import SnapshotError, SnapshotStore
from agentlab.recorder.store import RecorderStore, attach_audit
from agentlab.shared.home import agentlab_home
from agentlab.shared.trace import TraceEvent

UI_DIST = Path(__file__).resolve().parents[3] / "ui" / "dist"
ADMIN_TOKEN_ENV = "AL_ADMIN_TOKEN"
WS_POLL_S = 0.5


def admin_token() -> str:
    token = os.environ.get(ADMIN_TOKEN_ENV)
    if token:
        return token
    path = agentlab_home() / "admin_token"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(32)
    path.write_text(token, encoding="utf-8")
    return token


@dataclass
class AdminState:
    registry: Registry
    profiles: dict[str, Profile]
    clients: Clients
    gateways: dict[str, Gateway]
    approvals: ApprovalStore
    snapshots: SnapshotStore
    audit: AuditLog
    token: str
    port: int
    recorder: RecorderStore | None = None
    agent: Any = None  # agentlab.agent.Agent, when the agent has a Gateway token
    agent_problem: str | None = None  # why the chat is unavailable

    @property
    def token_sha256(self) -> str:
        return hashlib.sha256(self.token.encode()).hexdigest()

    @property
    def allowed_origins(self) -> list[str]:
        own = [f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"]
        return own + self.clients.allowed_origins

    def check_token(self, token: str | None) -> bool:
        if not token:
            return False
        return hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), self.token_sha256)


class Decision(BaseModel):
    decision: str  # approve | deny
    args: dict[str, Any] | None = None
    reason: str | None = None


class ChatRequest(BaseModel):
    message: str
    thread_id: str | None = None


class SimulateRequest(BaseModel):
    profile: str
    tool: str
    args: dict[str, Any] = {}
    risk: str | None = None


def _rule(r: Rule) -> dict[str, Any]:
    return {"source": r.source, "action": r.action, "match": r.match, "reason": r.reason}


def _profile(p: Profile) -> dict[str, Any]:
    return {
        "name": p.name,
        "visible": p.visible,
        "visible_risk": p.visible_risk,
        "guards": [_rule(r) for r in p.guards],
        "rules": [_rule(r) for r in p.rules],
        "budgets": [asdict(b) for b in p.budgets],
    }


def create_admin_app(state: AdminState, lifespan: Any = None) -> FastAPI:
    app = FastAPI(title="AgentLab Gateway admin", lifespan=lifespan, docs_url=None, redoc_url=None)

    def require_admin(authorization: str | None = Header(default=None)) -> None:
        token = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else None
        if not state.check_token(token):
            raise HTTPException(401, "missing or invalid admin token", headers={"WWW-Authenticate": "Bearer"})

    admin = [Depends(require_admin)]

    @app.get("/api/overview", dependencies=admin)
    def overview() -> dict[str, Any]:
        return {
            "clients": [{"name": c.name, "profile": c.profile, "session_id": state.gateways[c.name].session_id}
                        for c in state.clients.clients],
            "servers": {"total": len(state.registry.servers),
                        "healthy": sum(e.healthy for e in state.registry.servers.values())},
            "tools": len(state.registry.list_tools()),
            "pending_approvals": len(state.approvals.pending()),
            "home": str(agentlab_home()),
        }

    @app.get("/api/servers", dependencies=admin)
    def servers() -> list[dict[str, Any]]:
        out = []
        for e in state.registry.servers.values():
            tools = []
            for t in e.tools.values():
                a = t.annotations
                tools.append({"name": f"{e.name}__{t.name}", "description": t.description or "",
                              "risk": tool_risk(e, t),
                              "read_only": bool(a and a.read_only_hint),
                              "destructive": bool(a and a.destructive_hint)})
            out.append({"name": e.name, "healthy": e.healthy, "error": e.error, "trust_meta": e.trust_meta,
                        "tools": sorted(tools, key=lambda t: t["name"])})
        return out

    @app.get("/api/profiles", dependencies=admin)
    def profiles() -> list[dict[str, Any]]:
        return [_profile(p) for p in state.profiles.values()]

    @app.post("/api/simulate", dependencies=admin)
    def simulate(req: SimulateRequest) -> dict[str, Any]:
        profile = state.profiles.get(req.profile)
        if profile is None:
            raise HTTPException(404, f"unknown profile {req.profile}")
        risk = req.risk
        resolved = state.registry.resolve(req.tool)
        if risk is None and resolved is not None:
            risk = tool_risk(*resolved)
        engine = PolicyEngine(profile)
        decision = engine.evaluate(ToolCall(req.tool, req.args, risk))
        visible = engine.is_visible(req.tool, risk)
        return {"tool_exists": resolved is not None, "risk": risk, "visible": visible,
                "action": decision.action if visible else "deny",
                "rule": decision.rule.source if decision.rule else None,
                "reason": decision.reason if visible else "tool is not visible in this profile"}

    @app.get("/api/approvals", dependencies=admin)
    def approvals(status: str = "pending", limit: int = Query(50, le=500)) -> list[dict[str, Any]]:
        items = state.approvals.pending() if status == "pending" else state.approvals.recent(limit)
        return [asdict(a) for a in items]

    @app.post("/api/approvals/{approval_id}", dependencies=admin)
    def decide(approval_id: str, body: Decision) -> dict[str, Any]:
        if body.decision not in ("approve", "deny"):
            raise HTTPException(422, "decision must be approve or deny")
        status = "approved" if body.decision == "approve" else "denied"
        if not state.approvals.decide(approval_id, status, args=body.args, reason=body.reason):
            raise HTTPException(409, "request is not pending (answered or expired)")
        return {"id": approval_id, "status": status}

    @app.get("/api/audit", dependencies=admin)
    def audit(client: str | None = None, tool: str | None = None, decision: str | None = None,
              limit: int = Query(100, le=1000), after_id: int | None = None) -> list[dict[str, Any]]:
        return state.audit.query(client, tool, decision, limit, after_id)

    @app.get("/api/snapshots", dependencies=admin)
    def snapshots(session: str | None = None, limit: int = Query(50, le=500)) -> list[dict[str, Any]]:
        return [asdict(s) for s in state.snapshots.recent(session, limit)]

    @app.post("/api/snapshots/{snapshot_id}/rollback", dependencies=admin)
    def rollback(snapshot_id: str) -> dict[str, Any]:
        try:
            return {"restored": state.snapshots.rollback(snapshot_id)}
        except SnapshotError as e:
            raise HTTPException(409, str(e)) from e

    @app.post("/api/sessions/{session_id}/rollback", dependencies=admin)
    def rollback_session(session_id: str) -> dict[str, Any]:
        try:
            return {"rolled_back": state.snapshots.rollback_session(session_id)}
        except SnapshotError as e:
            raise HTTPException(409, str(e)) from e

    def recorder() -> RecorderStore:
        if state.recorder is None:
            raise HTTPException(503, "recorder is off")
        return state.recorder

    @app.get("/api/runs", dependencies=admin)
    def runs(limit: int = Query(100, le=1000), status: str | None = None, q: str | None = None) -> list[dict[str, Any]]:
        store = recorder()
        store.ingest_folder()
        return store.runs(limit, status, q)

    @app.get("/api/runs/{run_id}", dependencies=admin)
    def run(run_id: str) -> dict[str, Any]:
        store = recorder()
        store.ingest_folder()
        found = store.run(run_id)
        if found is None:
            raise HTTPException(404, f"no run {run_id}")
        attach_audit(found, state.audit.query(run_id=run_id, limit=10_000))
        return found

    @app.post("/api/recorder/events", dependencies=admin)
    def ingest(events: list[TraceEvent]) -> dict[str, int]:
        return {"stored": recorder().add_events(events)}

    @app.get("/api/chat/status", dependencies=admin)
    async def chat_status() -> dict[str, Any]:
        if state.agent is None:
            return {"available": False, "reason": state.agent_problem}
        cfg = state.agent.config
        try:
            async with httpx2.AsyncClient(timeout=2) as http:
                tags = (await http.get(f"{cfg.ollama_url.rstrip('/')}/api/tags")).json()
            models = [m["name"] for m in tags.get("models", [])]
        except Exception:  # noqa: BLE001 - any failure means Ollama is not usable
            return {"available": False, "reason": f"Ollama is not reachable at {cfg.ollama_url}"}
        if cfg.model not in models:
            return {"available": False, "reason": f"Model {cfg.model} is not pulled in Ollama"}
        return {"available": True, "model": cfg.model, "num_ctx": cfg.num_ctx}

    @app.post("/api/chat", dependencies=admin)
    async def chat(req: ChatRequest) -> StreamingResponse:
        if state.agent is None:
            raise HTTPException(503, state.agent_problem or "agent unavailable")

        async def lines():
            async for event in state.agent.run(req.message, req.thread_id):
                yield json.dumps(event, default=str) + "\n"

        return StreamingResponse(lines(), media_type="application/x-ndjson")

    @app.websocket("/api/ws")
    async def live(ws: WebSocket, token: str | None = None) -> None:
        # Browsers do not apply CORS to WebSockets, so check the Origin here.
        origin = ws.headers.get("origin")
        if (origin and origin not in state.allowed_origins) or not state.check_token(token):
            await ws.close(code=4401)
            return
        await ws.accept()

        async def push() -> None:
            last_pending: list[dict[str, Any]] | None = None
            latest = state.audit.query(limit=1)
            last_id = latest[0]["id"] if latest else 0
            while True:
                pending = [asdict(a) for a in state.approvals.pending()]
                if pending != last_pending:
                    await ws.send_text(json.dumps({"type": "approvals", "items": pending}, default=str))
                    last_pending = pending
                rows = state.audit.query(after_id=last_id, limit=200)
                if rows:
                    last_id = rows[0]["id"]
                    await ws.send_text(json.dumps({"type": "audit", "items": rows}, default=str))
                await anyio.sleep(WS_POLL_S)

        async def until_closed(cancel: anyio.CancelScope) -> None:
            # The push loop only sends on changes, so it would not notice a closed socket by itself.
            while (await ws.receive())["type"] != "websocket.disconnect":
                pass
            cancel.cancel()

        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(push)
                tg.start_soon(until_closed, tg.cancel_scope)
        except* (WebSocketDisconnect, RuntimeError):
            pass  # the client went away mid-send

    if UI_DIST.is_dir():
        app.mount("/", StaticFiles(directory=UI_DIST, html=True), name="ui")
    else:
        @app.get("/", response_class=HTMLResponse)
        def no_ui() -> str:
            return "<p>UI not built. Run <code>cd ui && npm install && npm run build</code>, then restart.</p>"

    return app


class Router:
    """Send `/mcp` to the MCP app and everything else to the admin app. Lifespan goes to the admin app."""

    def __init__(self, mcp_app: Any, admin_app: Any):
        self.mcp_app, self.admin_app = mcp_app, admin_app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        path = scope.get("path", "")
        if scope["type"] in ("http", "websocket") and (path == "/mcp" or path.startswith("/mcp/")):
            await self.mcp_app(scope, receive, send)
        else:
            await self.admin_app(scope, receive, send)


def build_full_app(state: AdminState, mcp_app: Any, mcp_server: Server) -> Any:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with mcp_server.session_manager.run():  # the MCP app's own lifespan never runs behind the Router
            yield

    admin = create_admin_app(state, lifespan)
    # DNS rebinding: only accept requests addressed to the loopback names.
    guarded = TrustedHostMiddleware(admin, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])
    return Router(mcp_app, guarded)
