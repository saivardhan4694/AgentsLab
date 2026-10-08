# Plan

Baseline plan. Change it only when the plan changes, and note why in [progress.md](progress.md). Details: [design.md](design.md).

## Goal

Local platform (no cloud) to run tool-using AI agents safely, attack them, and debug them. Python + FastAPI backends, React UI on localhost, Ollama for local LLMs, LangChain/LangGraph for the agent.

## Parts (build in this order)

1. **Gateway**: one MCP endpoint that gives any LLM client (Claude Desktop, Cursor, VS Code, our agent) access to the machine: first-party servers, installed CLIs, third-party MCP servers. Every call passes a customizable policy pipeline: profiles, visibility, rules, default deny, approvals, snapshots/rollback, audit log.
2. **Arena**: measures prompt-injection defenses. Fake scenario servers (inbox, bank, files). Defender agent does the task through the Gateway. Attacks are handwritten first; an attacker agent mutates them later. A judge (rule checks first, LLM judge later) scores attack success and task success. Defenses are Gateway plugins. Output: defense vs attack matrix.
3. **Recorder** (the observer): ingests trace events, shows each run as a timeline, then replay, fork at a step, diff two runs, golden regression runs.

## Milestones

| # | Milestone |
|---|---|
| 0 | Hand-written raw JSON-RPC MCP server and client |
| 1 | `fs` and `system` servers with the SDK; tests |
| 2 | Registry: connect to N servers, merge tools; pass-through proxy usable from Claude Desktop |
| 3 | Policy engine: profiles, visibility, rules, default deny, simulator CLI |
| 4 | `shell` server, approvals (CLI first), snapshots and rollback |
| 5 | HTTP transport, tokens, Origin check, audit log |
| 6 | CLI bridge with YAML manifests and `discover` |
| 7 | React admin UI |
| 8 | LangGraph agent through `/mcp`, emitting trace events |
| 9 | Recorder v1: ingest, run list, timeline |
| 10 | Arena v1: scenarios, attacks, defenses, judge, matrix runner |
| 11 | Recorder replay, fork, diff, golden runs |
| 12 | Polish: READMEs, diagrams, demo GIFs, results tables |

## Later ideas

Attacker agent, LLM judge, utility score, more first-party servers (`process`, `clipboard`, `notify`, `git`, `windows_ui`). Possible extra project: small model learns tool use via rejection-sampling fine-tuning.
