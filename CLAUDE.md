# AgentLab

Read `docs/plan.md` (baseline plan) and `docs/progress.md` (status) first. Design details: `docs/design.md`.
At the end of a session, update `docs/progress.md`. Change `docs/plan.md` only when the plan itself changes, and log why in progress.md.

- Python 3.12, `uv`, pytest (`uv run pytest`). MCP SDK `mcp` v2.
- Windows: launch Python servers with `python -m ...`; uv `.exe` shims can be blocked by Smart App Control.
- UI: `ui/` (Vite + React + TS). `npm run build` there; the Gateway serves `ui/dist` in `--http` mode.
