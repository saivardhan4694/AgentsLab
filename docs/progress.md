# Progress

Status against [plan.md](plan.md). Update at the end of every session.

## Milestones

| # | Status | Notes |
|---|---|---|
| 0 | done | `learn/00_raw_mcp/`; SDK client test passes against the raw server |
| 1 | done | `fs`, `system` servers + tests. pytest runs async tests with the anyio plugin (`tests/conftest.py`) |
| 2 | done | `gateway/registry.py`, `config.py`, `server.py`; `config/servers.yaml`. Stdio proxy, `server__tool` names, broken servers skipped. Not yet tried in Claude Desktop |
| 3 | done | `gateway/policy/` (engine, matching, simulator CLI); `config/profiles/` (base, readonly, coding, full-trust, dry-run). Budgets included. `ask` and `allow_with_snapshot` deny until milestone 4 |
| 4 | done | `servers/shell.py`; `gateway/pipeline.py` (request pipeline); `gateway/approvals.py` (SQLite queue + CLI `watch/list/approve/deny`); `gateway/snapshots.py` (content-addressed, rollback per call or session, CLI). State in `~/.agentlab/` (`AGENTLAB_HOME` overrides). Verified end to end with real config + `coding` profile |
| 5 | done | `gateway/http.py` (`--http`, Streamable HTTP on 127.0.0.1:8000/mcp, bearer auth middleware, Host/Origin check via SDK `TransportSecuritySettings`, loopback only); `gateway/clients.py` + `config/clients.yaml` (token via env var or SHA-256, `new-token` CLI); `gateway/audit.py` (`audit.db`, `list` CLI). One pipeline per client, so budgets are per client. Verified end to end |
| 6 | done | `cli_bridge/` (manifest loader, generic server, `serve/list/discover/draft` CLI, templates: git, gh, docker, winget, ffmpeg); `config/tools/` has the 4 found here; `cli` server in `servers.yaml` with `trust_meta`. Process runner moved to `shared/proc.py` (shell uses it). Verified through the Gateway |
| 7 | done | `gateway/api.py` (FastAPI admin API + `/api/ws` live feed + serves `ui/dist`; `Router` sends `/mcp` to the MCP app; admin token from `AL_ADMIN_TOKEN` or `~/.agentlab/admin_token`; TrustedHost + WebSocket Origin check). `ui/` (Vite + React + TS, no UI library): Approvals (live, edit args), Audit (live, filter, details), Servers, Policies (rules + simulator), Snapshots (rollback). API tested; UI built and served, not yet clicked through in a browser |
| 8 | done | `agent/` (LangChain `create_agent` + `ChatOllama` qwen3.5:4b, 16k ctx; own MCP-to-LangChain adapter `mcp_tools.py`; `tracing.py` callback; CLI `python -m agentlab.agent`); `shared/trace.py` (TraceEvent, JSONL in `~/.agentlab/traces/`); `/api/chat` (NDJSON stream) + Chat page. Verified with real Ollama: list+read sandbox file, git status, ~5 s each |
| 9 | done | `recorder/store.py` (SQLite `recorder.db`, incremental JSONL ingest, HTTP ingest, run summaries), `/api/runs`, `/api/runs/{id}`, `/api/recorder/events`; CLI `python -m agentlab.recorder list/show`; Runs page (run list + timeline with latency waterfall, prompts, thinking, Gateway decision per tool step). Agent sends `agentlab/run_id` in MCP `_meta`; audit stores it (`run_id` column, auto-migrated). Verified with real Ollama + screenshots (light/dark) |
| 10 | done | Arena. `gateway/pipeline.py` gained the `Plugin` hook (`before_call`/`after_call`, design.md 4.4 step 5 and 9; `before_call` only runs for allow/ask/allow_with_snapshot, so a denied call cannot affect a stateful plugin). `gateway/plugins.py` holds the generic result filters (`none`, `sanitizer`, `spotlighting`); `Profile` gained a `plugins: list[str]` field (union across `extends`), so real sessions (stdio/HTTP) can turn these on too, not just the Arena. `arena/scenarios/` (inbox, bank, files: in-process fake MCP servers + state); `arena/attacks.py` (12 handwritten attacks: direct, indirect, tool-poisoning, exfiltration); `arena/defenses.py` (reuses the generic plugins, adds the scenario-specific `tool_gating`); `arena/judge.py`; `arena/runner.py` (matrix runner: a no-attack baseline cell per scenario plus N independent trials per cell at temperature 0, Markdown export, CLI `python -m agentlab.arena --trials`). API: `/api/arena/attacks\|defenses\|run\|status\|report` (lazy imports, async `run`, task kept on `AdminState.arena_task`). Arena page (scenario/defenses/trials, run, poll, per-defense rate table, sanitizer cost note). Runs tagged `meta={"arena": {...}}` show up on the Runs page per milestone 11's tagging. Verified with a scripted model (`tests/test_arena.py`), not yet run against real Ollama. Built without hitting a safety block; see the note below about the earlier "blocked" status. |
| 11 | done | Recorder replay/fork/diff/golden. `recorder/replay.py` (ReplayClient substitutes recorded tool results; fork edits message/system-prompt/results), `recorder/diff.py` (LCS alignment of decision paths), `recorder/golden.py` (regression suite). Agent gained `mcp_client` + run `meta`. API: replay, diff, goldens CRUD + run. CLI: diff, golden. UI: Runs actions bar, fork panel, diff view, Goldens panel. Replays/goldens run at temperature 0. Verified end to end with Ollama + screenshots |
| 12 | pending | |

## Decisions and changes

- 2026-10-08: LangChain/LangGraph allowed (was "hand-written loop only").
- 2026-10-08: Gateway is general purpose (any client, whole machine), not only for the Arena.
- 2026-10-08: Launch servers with `python -m`, not uv `.exe` shims. Windows Smart App Control blocks the shims.

- 2026-10-08: Policy format adds `guards` (parent-first, children cannot override), `visible_risk`, and `any_arg`. Unannotated tools count as high risk (MCP spec default). Dry run returns a tool error, because a success without structured content fails client validation for tools with an output schema.

- 2026-10-08: Approvals go through a SQLite queue answered from a separate CLI, so they work when Claude Desktop launches the Gateway. Edited arguments are re-checked by the policy. Any call with `writes` metadata is snapshotted; `allow_with_snapshot` denies if no snapshot is possible. Snapshot id is returned in result `_meta["agentlab/snapshot"]`.
- 2026-10-08: fs and shell roots in `servers.yaml` are `~/agentlab-sandbox` and `~/repositories`. Widen them there for whole-machine access.

- 2026-10-08: Plain Starlette app from the SDK for `/mcp`, not FastAPI yet. FastAPI arrives with the admin API (milestone 7) and mounts this app.

- 2026-10-08: Manifests use `argv` (program from `binary`, not repeated), typed args, `when/then/else` blocks. String values starting with "-" are refused (argument injection); `.bat`/`.cmd` programs are refused (cmd.exe re-parses arguments). Tool risk and writes travel in tool `_meta`, trusted only with `trust_meta: true`.

- 2026-10-08: The UI reads approvals and audit from the shared SQLite files, so one `--http` Gateway shows requests from stdio Gateways (Claude Desktop) too. Live feed polls SQLite every 0.5 s instead of an in-process event bus, for that reason.

- 2026-10-08: `langchain-mcp-adapters` 0.3.1 imports MCP SDK v1 modules (`mcp.server.fastmcp`), so it is replaced by `agent/mcp_tools.py`.
- 2026-10-08: Hardware: RTX 4060 8 GB VRAM, 16 GB RAM. Model `qwen3.5:4b` (Ollama 0.40, llamacpp runner). `num_ctx` set per request by the agent (`AGENTLAB_NUM_CTX`). Ollama shows the model twice: one copy per runner (llamacpp, ggml); kept.
- 2026-10-08: The chat agent is the `my-agent` client (`AL_TOKEN_AGENT`), profile `coding`. It reconnects to `/mcp` per turn; chat memory is in-process (`InMemorySaver`).

- 2026-10-08: Recorder runs inside the Gateway's admin app (same port and UI), not as a separate service on :8100. Simpler to run; the store and ingest endpoint are separate modules, so it can split out later.

- 2026-10-08 (earlier session): Arena (milestone 10) reported as blocked by a safety classifier and skipped; AgentDojo suggested as a substitute. Milestone 11 was built instead.
- 2026-10-08 (later session): Arena built successfully with no safety block, using fake in-process scenario servers and clearly-labeled handwritten attack text (a defensive security benchmark, not real attack payloads). The earlier "blocked" status did not reproduce; AgentDojo was not needed. Likely cause of the earlier block: unclear from this session; worth comparing prompts/phrasing if it recurs.
- 2026-10-08: Replay holds the environment fixed by serving recorded tool results (no live calls, no audit rows); it still needs the Gateway up for tool schemas. Goldens compare the ordered tool calls (names + args), the stable regression signal; answer wording is shown but not asserted. The 4B model still diverges sometimes even at temperature 0.

## Known gaps

- Path guards only see path arguments. Shell commands get a keyword guard only; real protection is that shell needs approval except read-only `git` commands (coding profile).
- The shell server is not a sandbox: the cwd jail limits where a command starts, not what it touches.
- Upstream clients may time out before the 120 s approval window ends (`--approval-timeout`). Progress notifications or MCP elicitation could fix that later.
- Downstream servers are stdio only; `url:` entries for third-party HTTP servers are not supported yet.
- HTTP budgets and sessions are per client for the Gateway's lifetime, not per MCP session.
- `discover` only knows the built-in templates; `draft` writes a skeleton. LLM-assisted manifest drafting is later.
- Agent writes traces as files; the HTTP ingest endpoint exists but the agent does not push to it yet. Gateway decisions are joined from the audit log by run_id (tool name + order), not stored in the trace.
- Chat memory is lost when the Gateway restarts.
- UI cannot edit profiles or restart/disable servers yet (read-only views + simulator). Chat page comes with the agent (milestone 8).
- Profiles load at Gateway start; edits need a restart.
- Rollback restores exact state, so it also removes later changes at the same path. No undo of a rollback.
- No JSON Schema validation of arguments in the Gateway yet; downstream servers validate.
- `fs__search` can list file names inside guarded folders (contents stay blocked).

Arena-specific:
- `tool_gating` still needs scenario-supplied `untrusted_tools`/`sensitive_tools` lists, so it is not in `gateway/plugins.py`'s generic registry a real profile can pick from yet. A risk/annotation-driven version (gate high/critical-risk tools after any open-world read) would generalize it.
- `sanitizer`'s patterns are broad by design and will also redact ordinary phrasing (e.g. "you must reply by Friday"); the Arena report calls this out, but the patterns themselves are not tuned.
- Default 5 trials per cell is a guess, not a calibrated number; real-Ollama variance (temperature 0, but a 4B model still diverges) hasn't been measured yet.
- 12 handwritten attacks across 3 scenarios is a small, illustrative set, not a benchmark. A larger or standard set (e.g. AgentDojo) would give more confidence in the numbers.
- Not yet run against real Ollama end to end, only against a scripted model in tests.

## Next

Arena v1 (milestone 10) is built; run it against real Ollama to see real attack/defense numbers (so far only exercised with a scripted model in tests). Consider adding AgentDojo as a second, larger attack set later rather than as a replacement.

Remaining: Milestone 12 (polish: per-part READMEs, architecture diagram, demo GIFs). Earlier Gateway gaps: edit profiles in the UI, enable/restart servers, downstream servers by URL, approvals via MCP elicitation.
