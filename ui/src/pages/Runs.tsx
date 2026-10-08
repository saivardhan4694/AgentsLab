import { useEffect, useMemo, useState } from "react";
import { api, post } from "../api";
import type { AuditRow, DiffResult, Golden, GoldenRunResult, RunDetail, RunSummary, TraceEventRow } from "../api";
import { Action } from "./common";

const secs = (ms: number) => (ms >= 10_000 ? `${(ms / 1000).toFixed(0)} s` : `${(ms / 1000).toFixed(1)} s`);
const when = (iso: string) => new Date(iso).toLocaleString(undefined, { hour12: false, month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });

type View = { mode: "list" } | { mode: "run"; id: string } | { mode: "diff"; a: string; b: string };

export function RunsPage() {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [q, setQ] = useState("");
  const [kind, setKind] = useState("");
  const [view, setView] = useState<View>({ mode: "list" });

  useEffect(() => {
    const params = new URLSearchParams({ limit: "200" });
    if (q) params.set("q", q);
    if (kind) params.set("kind", kind);
    const load = () => api<RunSummary[]>(`/api/runs?${params}`).then(setRuns).catch(() => {});
    load();
    const timer = window.setInterval(load, 3000); // trace files grow while a run is going
    return () => window.clearInterval(timer);
  }, [q, kind]);

  if (view.mode === "run")
    return <RunView runId={view.id} runs={runs} onBack={() => setView({ mode: "list" })}
      onOpen={(id) => setView({ mode: "run", id })} onCompare={(a, b) => setView({ mode: "diff", a, b })} />;
  if (view.mode === "diff")
    return <DiffView a={view.a} b={view.b} onBack={() => setView({ mode: "run", id: view.b })} onOpen={(id) => setView({ mode: "run", id })} />;

  return (
    <>
      <header>
        <h2>Runs</h2>
        <p className="muted">Every agent run, recorded step by step. Open one to replay it, fork it with edits, compare two runs, or save it as a regression test.</p>
      </header>
      <Goldens onOpen={(id) => setView({ mode: "run", id })} />
      <div className="row toolbar">
        <input className="grow" placeholder="Search task or answer" value={q} onChange={(e) => setQ(e.target.value)} />
        <select value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="">All runs</option>
          <option value="chat">Chat</option>
          <option value="arena">Arena</option>
          <option value="replay">Replays and forks</option>
        </select>
      </div>
      <div className="card flush">
        <table>
          <thead>
            <tr><th>Started</th><th>Task</th><th>Status</th><th className="right">Model calls</th><th className="right">Tool calls</th><th className="right">Tokens</th><th className="right">Time</th></tr>
          </thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.run_id} className="clickable" onClick={() => setView({ mode: "run", id: r.run_id })}>
                <td className="nowrap muted">{when(r.started_at)}</td>
                <td>{r.message} <RunTags tags={r.tags} /></td>
                <td><span className={`pill status-${r.status}`}>{r.status}</span></td>
                <td className="right">{r.llm_calls}</td>
                <td className="right">{r.tool_calls}{r.tool_errors > 0 && <span className="error"> ({r.tool_errors} failed)</span>}</td>
                <td className="right muted">{(r.tokens_in + r.tokens_out).toLocaleString()}</td>
                <td className="right muted">{secs(r.duration_ms)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {runs.length === 0 && <div className="empty">No runs yet. Chat with the agent, then come back.</div>}
      </div>
    </>
  );
}

function RunTags({ tags }: { tags: RunSummary["tags"] | undefined }) {
  if (!tags) return null;
  const arena = tags.arena as { scenario?: string; attack_id?: string; defense?: string; trial?: number } | undefined;
  return (
    <>
      {arena && (
        <span className="pill tag-arena" title="Arena matrix cell">
          arena · {[arena.scenario, arena.attack_id, arena.defense].filter(Boolean).join(" · ")}
          {arena.trial !== undefined && ` · trial ${arena.trial}`}
        </span>
      )}
      {tags.replay_of && <span className="pill">replay</span>}
      {tags.forked_from && <span className="pill">fork</span>}
    </>
  );
}

function Goldens({ onOpen }: { onOpen: (id: string) => void }) {
  const [goldens, setGoldens] = useState<Golden[]>([]);
  const [results, setResults] = useState<GoldenRunResult[] | null>(null);
  const [running, setRunning] = useState(false);
  const load = () => api<Golden[]>("/api/goldens").then(setGoldens).catch(() => {});
  useEffect(() => { load(); }, []);

  const runAll = async () => {
    setRunning(true);
    setResults(null);
    try {
      setResults(await post<GoldenRunResult[]>("/api/goldens/run"));
    } catch (e) {
      alert(String((e as Error).message));
    }
    setRunning(false);
    load();
  };
  const remove = async (id: string) => {
    await api(`/api/goldens/${id}`, { method: "DELETE" });
    load();
  };
  if (goldens.length === 0) return null;

  const byId = new Map((results ?? []).map((r) => [r.golden_id, r]));
  return (
    <div className="card">
      <div className="row">
        <h3 className="grow">Golden runs <span className="muted small">regression tests</span></h3>
        <button className="primary" disabled={running} onClick={runAll}>{running ? "Replaying…" : "Run all"}</button>
      </div>
      <table>
        <tbody>
          {goldens.map((g) => {
            const r = byId.get(g.id);
            return (
              <tr key={g.id}>
                <td>{r && <span className={`pill ${r.passed ? "status-ok" : "status-error"}`}>{r.passed ? "pass" : "fail"}</span>}</td>
                <td>{g.label || <span className="muted">(no label)</span>}</td>
                <td className="muted small">{g.expected.tool_path.length} tool calls</td>
                <td><button className="link" onClick={() => onOpen(g.run_id)}>source run</button>{r?.new_run_id && <> · <button className="link" onClick={() => onOpen(r.new_run_id!)}>replay</button></>}</td>
                <td className="right"><button className="link" onClick={() => remove(g.id)}>Delete</button></td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {results && <p className="muted small">{results.filter((r) => r.passed).length}/{results.length} passed. A fail means the agent took a different path of tool calls than when the golden was saved.</p>}
    </div>
  );
}

type Step =
  | { kind: "model"; event: TraceEventRow; start: number; end: number }
  | { kind: "tool"; call: TraceEventRow; result?: TraceEventRow; gateway?: AuditRow | null; start: number; end: number }
  | { kind: "error"; event: TraceEventRow; start: number; end: number };

function buildSteps(events: TraceEventRow[]): { steps: Step[]; total: number } {
  const t0 = events.length ? Date.parse(events[0].ts) : 0;
  const at = (e: TraceEventRow) => Date.parse(e.ts) - t0;
  const results = new Map(events.filter((e) => e.type === "tool_result").map((e) => [e.parent_step, e]));
  const steps: Step[] = [];
  for (const e of events) {
    // llm_call and tool_result are written when they finish, so their bar ends at ts.
    if (e.type === "llm_call") steps.push({ kind: "model", event: e, start: at(e) - (e.latency_ms ?? 0), end: at(e) });
    else if (e.type === "tool_call") {
      const result = results.get(e.step);
      const end = result ? at(result) : at(e);
      steps.push({ kind: "tool", call: e, result, gateway: e.gateway, start: result ? end - (result.latency_ms ?? 0) : at(e), end });
    } else if (e.type === "error") steps.push({ kind: "error", event: e, start: at(e), end: at(e) });
  }
  const total = Math.max(1, ...events.map(at), ...steps.map((s) => s.end));
  return { steps, total };
}

function RunView({ runId, runs, onBack, onOpen, onCompare }: {
  runId: string; runs: RunSummary[]; onBack: () => void; onOpen: (id: string) => void; onCompare: (a: string, b: string) => void;
}) {
  const [run, setRun] = useState<RunDetail | null>(null);
  const [error, setError] = useState("");
  const [fork, setFork] = useState(false);
  useEffect(() => {
    setRun(null);
    const load = () => api<RunDetail>(`/api/runs/${runId}`).then(setRun).catch((e) => setError(e.message));
    load();
    const timer = window.setInterval(() => run?.status === "running" && load(), 2000);
    return () => window.clearInterval(timer);
  }, [runId]);
  const { steps, total } = useMemo(() => buildSteps(run?.events ?? []), [run]);

  if (error) return <><button className="link" onClick={onBack}>← All runs</button><p className="error">{error}</p></>;
  if (!run) return <p className="muted">Loading…</p>;
  const origin = run.events[0]?.payload?.replay_of || run.events[0]?.payload?.forked_from;
  return (
    <>
      <header>
        <button className="link" onClick={onBack}>← All runs</button>
        <h2>{run.message}</h2>
        <p className="muted small">
          run {run.run_id} · {run.model} · <span className={`pill status-${run.status}`}>{run.status}</span> · {secs(run.duration_ms)} ·{" "}
          {run.llm_calls} model calls · {run.tool_calls} tool calls · {(run.tokens_in).toLocaleString()} in / {(run.tokens_out).toLocaleString()} out tokens
          {origin && <> · {run.events[0].payload.forked_from ? "forked from" : "replay of"} <button className="link" onClick={() => onOpen(origin)}>{origin}</button></>}
        </p>
      </header>
      <RunActions run={run} runs={runs} onOpen={onOpen} onCompare={onCompare} onFork={() => setFork(!fork)} forkOpen={fork} />
      {fork && <ForkPanel run={run} onDone={onOpen} />}
      <div className="card">
        <div className="row small legend">
          <span><i className="swatch model" /> Model call</span>
          <span><i className="swatch tool" /> Tool call through the Gateway</span>
          <span className="grow" />
          <span className="muted">0 s — {secs(total)}</span>
        </div>
        <div className="timeline">
          {steps.map((s, i) => <StepRow key={i} step={s} total={total} />)}
        </div>
      </div>
      {run.answer && (
        <div className="card">
          <h4>Answer</h4>
          <div className="answer">{run.answer}</div>
        </div>
      )}
    </>
  );
}

function RunActions({ run, runs, onOpen, onCompare, onFork, forkOpen }: {
  run: RunDetail; runs: RunSummary[]; onOpen: (id: string) => void; onCompare: (a: string, b: string) => void; onFork: () => void; forkOpen: boolean;
}) {
  const [busy, setBusy] = useState("");
  const replay = async () => {
    setBusy("replay");
    try {
      const r = await post<{ run_id: string }>(`/api/runs/${run.run_id}/replay`, {});
      onOpen(r.run_id);
    } catch (e) {
      alert(String((e as Error).message));
    }
    setBusy("");
  };
  const saveGolden = async () => {
    const label = window.prompt("Label for this golden run?", run.message ?? "");
    if (label === null) return;
    await post("/api/goldens", { run_id: run.run_id, label });
    alert("Saved as a golden. Find it at the top of the Runs list.");
  };
  return (
    <div className="card row">
      <button disabled={busy !== ""} onClick={replay} title="Re-run with the recorded tool results, so only the model can change">
        {busy === "replay" ? "Replaying…" : "Replay"}
      </button>
      <button className={forkOpen ? "active" : ""} onClick={onFork}>Fork with edits</button>
      <button onClick={saveGolden}>Save as golden</button>
      <span className="grow" />
      <label className="muted small">Compare with&nbsp;
        <select defaultValue="" onChange={(e) => e.target.value && onCompare(e.target.value, run.run_id)}>
          <option value="">choose a run…</option>
          {runs.filter((r) => r.run_id !== run.run_id).map((r) => (
            <option key={r.run_id} value={r.run_id}>{r.run_id} — {(r.message ?? "").slice(0, 40)}</option>
          ))}
        </select>
      </label>
    </div>
  );
}

function ForkPanel({ run, onDone }: { run: RunDetail; onDone: (id: string) => void }) {
  const toolCalls = run.events.filter((e) => e.type === "tool_call");
  const [message, setMessage] = useState(run.message ?? "");
  const [overrides, setOverrides] = useState<Record<number, string>>({});
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    const result_overrides: Record<string, { content: string }> = {};
    for (const [step, content] of Object.entries(overrides)) result_overrides[step] = { content };
    try {
      const body = { message: message !== run.message ? message : undefined, result_overrides: Object.keys(result_overrides).length ? result_overrides : undefined };
      const r = await post<{ run_id: string; unmatched: string[] }>(`/api/runs/${run.run_id}/replay`, body);
      if (r.unmatched.length) alert(`The fork diverged: no recorded result for ${r.unmatched.join(", ")}. Those calls returned a "not in recording" note.`);
      onDone(r.run_id);
    } catch (e) {
      alert(String((e as Error).message));
    }
    setBusy(false);
  };

  return (
    <div className="card">
      <h4>Fork this run</h4>
      <p className="muted small">Change the task or a tool's result, then replay from the top. Everything else is held to the recording.</p>
      <label className="small">Task message</label>
      <textarea value={message} rows={2} onChange={(e) => setMessage(e.target.value)} />
      {toolCalls.length > 0 && <label className="small">Override a tool result (leave blank to keep the recorded one)</label>}
      {toolCalls.map((e) => (
        <div key={e.step} className="override">
          <code>{e.payload.tool}</code>
          <textarea rows={2} placeholder="recorded result kept" value={overrides[e.step] ?? ""}
            onChange={(ev) => setOverrides({ ...overrides, [e.step]: ev.target.value })} />
        </div>
      ))}
      <div className="row"><button className="primary" disabled={busy} onClick={submit}>{busy ? "Replaying…" : "Replay fork"}</button></div>
    </div>
  );
}

function DiffView({ a, b, onBack, onOpen }: { a: string; b: string; onBack: () => void; onOpen: (id: string) => void }) {
  const [diff, setDiff] = useState<DiffResult | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    api<DiffResult>(`/api/runs/${a}/diff/${b}`).then(setDiff).catch((e) => setError(e.message));
  }, [a, b]);
  if (error) return <><button className="link" onClick={onBack}>← Back</button><p className="error">{error}</p></>;
  if (!diff) return <p className="muted">Loading…</p>;
  const label = { same: "=", changed: "~", only_a: "−", only_b: "+" } as const;
  return (
    <>
      <header>
        <button className="link" onClick={onBack}>← Back</button>
        <h2>Compare runs</h2>
        <p className="muted small">
          {diff.same_tool_path
            ? "Both runs made the same sequence of tool calls."
            : "The two runs took different paths of tool calls."}
        </p>
      </header>
      <div className="row diff-head">
        <div className="grow"><button className="link" onClick={() => onOpen(a)}>{a}</button> <span className={`pill status-${diff.a.status}`}>{diff.a.status}</span> · {diff.a.steps} steps · {diff.a.tokens.toLocaleString()} tok</div>
        <div className="grow"><button className="link" onClick={() => onOpen(b)}>{b}</button> <span className={`pill status-${diff.b.status}`}>{diff.b.status}</span> · {diff.b.steps} steps · {diff.b.tokens.toLocaleString()} tok</div>
      </div>
      <div className="card flush">
        {diff.rows.map((row, i) => (
          <div key={i} className={`diff-row ${row.status}`}>
            <span className="diff-mark">{label[row.status]}</span>
            <div className="diff-cell">{row.a && <><span className={`step-kind ${row.a.kind}`}>{row.a.kind}</span> {row.a.summary}</>}</div>
            <div className="diff-cell">{row.b && <><span className={`step-kind ${row.b.kind}`}>{row.b.kind}</span> {row.b.summary}</>}</div>
          </div>
        ))}
      </div>
      {(diff.a.answer || diff.b.answer) && (
        <div className="row">
          <div className="card grow"><h4>Answer A</h4><div className="answer small">{diff.a.answer}</div></div>
          <div className="card grow"><h4>Answer B</h4><div className="answer small">{diff.b.answer}</div></div>
        </div>
      )}
    </>
  );
}

function Bar({ start, end, total, kind, label }: { start: number; end: number; total: number; kind: string; label: string }) {
  const left = (Math.max(0, start) / total) * 100;
  const width = Math.max(0.6, ((end - start) / total) * 100);
  return (
    <div className="track" title={label}>
      <div className={`bar ${kind}`} style={{ left: `${left}%`, width: `${Math.min(width, 100 - left)}%` }} />
    </div>
  );
}

function StepRow({ step, total }: { step: Step; total: number }) {
  if (step.kind === "error") {
    return (
      <div className="step-row">
        <div className="step-head"><span className="error">Error</span><span className="muted small">{step.event.payload.error}</span></div>
      </div>
    );
  }
  if (step.kind === "model") {
    const p = step.event.payload;
    const calls: { name: string }[] = p.response?.tool_calls ?? [];
    const summary = calls.length ? `asks for ${calls.map((c) => c.name).join(", ")}` : String(p.response?.content ?? "").slice(0, 120);
    const ms = step.event.latency_ms ?? 0;
    return (
      <details className="step-row">
        <summary>
          <div className="step-head">
            <span className="step-kind model">Model</span>
            <span className="grow ellipsis">{summary}</span>
            <span className="muted small nowrap">{ms} ms · {step.event.tokens_in ?? 0}→{step.event.tokens_out ?? 0} tok</span>
          </div>
          <Bar start={step.start} end={step.end} total={total} kind="model" label={`Model call: ${ms} ms`} />
        </summary>
        {p.reasoning && <details><summary className="muted">Thinking</summary><pre>{p.reasoning}</pre></details>}
        <details><summary className="muted">Prompt ({p.messages?.length ?? 0} messages)</summary>
          {(p.messages ?? []).map((m: any, i: number) => (
            <div key={i} className="msg"><span className="muted small">{m.role}</span><pre>{typeof m.content === "string" ? m.content : JSON.stringify(m.content, null, 2)}{m.tool_calls ? `\n${JSON.stringify(m.tool_calls, null, 2)}` : ""}</pre></div>
          ))}
        </details>
        <h4>Response</h4>
        <pre>{p.response?.content || JSON.stringify(calls, null, 2)}</pre>
      </details>
    );
  }
  const { call, result, gateway } = step;
  const ms = result?.latency_ms ?? 0;
  const failed = result?.payload.is_error;
  return (
    <details className="step-row">
      <summary>
        <div className="step-head">
          <span className="step-kind tool">Tool</span>
          <code>{call.payload.tool}</code>
          {gateway && <Action action={gateway.decision} />}
          {gateway?.approval && <span className="muted small">approval {gateway.approval}</span>}
          <span className="grow" />
          <span className={`small nowrap ${failed ? "error" : "muted"}`}>{result ? (failed ? "failed" : "ok") : "running"} · {ms} ms</span>
        </div>
        <Bar start={step.start} end={step.end} total={total} kind="tool" label={`${call.payload.tool}: ${ms} ms`} />
      </summary>
      <h4>Arguments</h4>
      <pre>{JSON.stringify(call.payload.args, null, 2)}</pre>
      {gateway && (
        <p className="small">
          Gateway: <b>{gateway.decision}</b> by {gateway.rule ?? "default"} ({gateway.reason}), outcome {gateway.outcome}, profile {gateway.profile}
          {gateway.snapshot && <> · snapshot <code>{gateway.snapshot}</code></>}
        </p>
      )}
      {result && (<><h4>Result</h4><pre className={failed ? "error" : ""}>{String(result.payload.content)}</pre></>)}
    </details>
  );
}
