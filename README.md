# AgentLab

Local platform for running tool-using AI agents safely, attacking them, and debugging them.

- Plan: [docs/plan.md](docs/plan.md)
- Progress: [docs/progress.md](docs/progress.md)
- Design: [docs/design.md](docs/design.md)

## Setup

```bash
uv sync
uv run pytest
```

## Use the Gateway from Claude Desktop

Add to `claude_desktop_config.json` (use absolute paths):

```json
{
  "mcpServers": {
    "agentlab": {
      "command": "C:\\path\\to\\agentlab\\.venv\\Scripts\\python.exe",
      "args": ["-m", "agentlab.gateway.server", "--config", "C:\\path\\to\\agentlab\\config\\servers.yaml", "--profile", "coding"]
    }
  }
}
```

Downstream servers are listed in [config/servers.yaml](config/servers.yaml). Profiles are in [config/profiles/](config/profiles/); the default is `readonly`. Unmatched calls are denied.

Check a decision without running anything:

```bash
uv run python -m agentlab.gateway.policy show --profile coding
uv run python -m agentlab.gateway.policy simulate --profile coding --tool fs__read_file --args '{"path": "~/.ssh/id_rsa"}' --risk low
```

## Use the Gateway over HTTP (Cursor, VS Code, your own agent)

```bash
uv run python -m agentlab.gateway.clients new-token          # put the token in an env var named in config/clients.yaml
uv run python -m agentlab.gateway.server --http --config config/servers.yaml
```

Clients connect to `http://127.0.0.1:8000/mcp` with `Authorization: Bearer <token>`. The token picks the client's profile.

## Admin UI

```bash
cd ui && npm install && npm run build && cd ..
uv run python -m agentlab.gateway.server --http --config config/servers.yaml
```

Open the `Admin UI` link the Gateway prints (it carries the admin token once). Pages: live approvals, audit log, servers and tools, policies with a simulator, snapshots with rollback. Approvals from stdio Gateways (Claude Desktop) show up here too.
For UI development, run `npm run dev` in `ui/` and open http://127.0.0.1:5173 (it proxies `/api` to the Gateway).

## Chat agent

A LangGraph agent on a local Ollama model (default `qwen3.5:4b`). It uses tools only through the Gateway.

```bash
uv run python -m agentlab.gateway.clients new-token   # set it as AL_TOKEN_AGENT (client my-agent, profile coding)
uv run python -m agentlab.gateway.server --http --config config/servers.yaml   # Chat page in the UI
uv run python -m agentlab.agent "what is in my agentlab-sandbox folder?"      # or the terminal
```

Each run writes a trace to `~/.agentlab/traces/<run_id>.jsonl`. Settings: `AGENTLAB_MODEL`, `AGENTLAB_NUM_CTX`, `OLLAMA_HOST`.

## Recorder

The Runs page shows every agent run as a timeline: each model call (prompt, thinking, response, tokens, latency) and each tool call with the Gateway's decision for it. In a terminal:

```bash
uv run python -m agentlab.recorder list
uv run python -m agentlab.recorder show <run_id>
```

## Add installed programs as tools

```bash
uv run python -m agentlab.cli_bridge discover --write config/tools   # copy templates for programs found on PATH
uv run python -m agentlab.cli_bridge draft <program>                 # skeleton manifest for anything else
uv run python -m agentlab.cli_bridge list                            # check manifests
```

## Operate

Answer approval requests (calls with the `ask` action wait up to 120 s, then deny):

```bash
uv run python -m agentlab.gateway.approvals watch
```

See every call and its decision:

```bash
uv run python -m agentlab.gateway.audit list --limit 20
```

Undo file changes made through the Gateway:

```bash
uv run python -m agentlab.gateway.snapshots list
uv run python -m agentlab.gateway.snapshots rollback <snapshot_id>
uv run python -m agentlab.gateway.snapshots rollback --session <session_id>
```
