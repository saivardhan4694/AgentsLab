"""HTTP mode: the Gateway as a Streamable HTTP MCP endpoint at http://127.0.0.1:8000/mcp.

- Every request needs `Authorization: Bearer <token>`. The token picks the client and its profile
  (clients.yaml). Each client gets its own pipeline, so budgets are per client.
- Host and Origin headers are checked (DNS rebinding protection). Only the origins in
  clients.yaml may call from a browser; non-browser clients send no Origin.
- Binds to loopback only.

The full app (serve_http) also serves the admin API and UI; see api.py.
"""

import json
import logging
import uuid
from pathlib import Path
from typing import Any

import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings

from agentlab.gateway.approvals import ApprovalStore
from agentlab.gateway.audit import AuditLog
from agentlab.gateway.clients import Clients, load_clients
from agentlab.gateway.config import load_servers
from agentlab.gateway.pipeline import Gateway
from agentlab.gateway.policy.engine import PolicyEngine, Profile, load_profiles
from agentlab.gateway.registry import Registry
from agentlab.gateway.server import create_server
from agentlab.gateway.snapshots import SnapshotStore
from agentlab.recorder.store import RecorderStore

log = logging.getLogger(__name__)

LOOPBACK = ("127.0.0.1", "localhost", "::1")


class BearerAuth:
    """ASGI middleware: reject requests to the MCP path without a known bearer token."""

    def __init__(self, app: Any, clients: Clients, path: str = "/mcp"):
        self.app, self.clients, self.path = app, clients, path

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope["path"].startswith(self.path):
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            if self.clients.identify(headers.get("authorization")) is None:
                body = json.dumps({"error": "missing or invalid bearer token"}).encode()
                await send({"type": "http.response.start", "status": 401, "headers": [
                    (b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def build_mcp(registry: Registry, profiles: dict[str, Profile], clients: Clients, *, port: int,
              approvals: ApprovalStore | None = None, snapshots: SnapshotStore | None = None,
              audit: AuditLog | None = None, approval_timeout: float = 120) -> tuple[Any, Server, dict[str, Gateway]]:
    """The authenticated `/mcp` ASGI app, its MCP server (owner of the session manager), and the per-client pipelines."""
    missing = {c.profile for c in clients.clients} - set(profiles)
    if missing:
        raise ValueError(f"clients.yaml uses unknown profiles: {sorted(missing)}")
    run_id = uuid.uuid4().hex[:6]
    gateways = {
        c.name: Gateway(registry, PolicyEngine(profiles[c.profile]), approvals, snapshots, approval_timeout,
                        audit, client=c.name, session_id=f"{c.name}-{run_id}")
        for c in clients.clients
    }

    def pick(ctx: Any) -> Gateway:
        request = ctx.request
        client = clients.identify(request.headers.get("authorization")) if request is not None else None
        if client is None:  # BearerAuth already stops these; this is a second check
            raise PermissionError("unauthenticated request")
        return gateways[client.name]

    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"],
        allowed_origins=clients.allowed_origins,
    )
    server = create_server(pick)
    app = server.streamable_http_app(transport_security=security)
    return BearerAuth(app, clients), server, gateways


def build_app(registry: Registry, profiles: dict[str, Profile], clients: Clients, *, port: int, **kwargs: Any) -> Any:
    """MCP endpoint only (no admin API). Its Starlette lifespan runs the session manager."""
    app, _, _ = build_mcp(registry, profiles, clients, port=port, **kwargs)
    return app


def make_agent(clients: Clients, port: int) -> tuple[Any, str | None]:
    """The chat agent, as a normal client of this Gateway's own /mcp endpoint."""
    import os

    from agentlab.agent.agent import Agent, AgentConfig

    config = AgentConfig(gateway_url=f"http://127.0.0.1:{port}/mcp")
    token = os.environ.get(config.token_env)
    if not token:
        return None, f"Set {config.token_env} to a client token from clients.yaml to enable the chat"
    client = clients.identify(f"Bearer {token}")
    if client is None:
        return None, f"{config.token_env} does not match any client in clients.yaml"
    log.info("Chat agent: client %s (profile %s), model %s", client.name, client.profile, config.model)
    return Agent(config), None


async def serve_http(config: Path, profiles_dir: Path, clients_path: Path, host: str, port: int,
                     approval_timeout: float) -> None:
    from agentlab.gateway.api import AdminState, admin_token, build_full_app

    if host not in LOOPBACK:
        raise SystemExit(f"Refusing to bind to {host}: the Gateway listens on loopback only")
    clients = load_clients(clients_path)
    if not clients.clients:
        log.warning("No usable clients in %s: /mcp accepts nobody, the admin UI still works", clients_path)
    profiles = load_profiles(profiles_dir)
    approvals, snapshots, audit = ApprovalStore(), SnapshotStore(), AuditLog()
    async with Registry(load_servers(config)) as registry:
        mcp_app, mcp_server, gateways = build_mcp(registry, profiles, clients, port=port, approvals=approvals,
                                                  snapshots=snapshots, audit=audit, approval_timeout=approval_timeout)
        token = admin_token()
        state = AdminState(registry, profiles, clients, gateways, approvals, snapshots, audit, token, port)
        state.recorder = RecorderStore()
        state.agent, state.agent_problem = make_agent(clients, port)
        app = build_full_app(state, mcp_app, mcp_server)
        log.info("Clients: %s", ", ".join(f"{c.name} ({c.profile})" for c in clients.clients) or "none")
        server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="on",
                                                    timeout_graceful_shutdown=5))
        log.info("MCP endpoint: http://%s:%d/mcp", host, port)
        log.info("Admin UI:     http://%s:%d/?token=%s", host, port, token)
        await server.serve()
