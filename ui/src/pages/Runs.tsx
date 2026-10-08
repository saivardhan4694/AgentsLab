import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type { AuditRow, RunDetail, RunSummary, TraceEventRow } from "../api";
import { Action } from "./common";

const secs = (ms: number) => (ms >= 10_000 ? `${(ms / 1000).toFixed(0)} s` : `${(ms / 1000).toFixed(1)} s`);
const when = (iso: string) => new Date(iso).toLocaleString(undefined, { hour12: false, month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });

export function RunsPage() {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<string | null>(null);

  useEffect(() => {
    const load = () => api<RunSummary[]>(`/api/runs?limit=200${q ? `&q=${encodeURIComponent(q)}` : ""}`).then(setRuns).catch(() => {});
    load();
    const timer = window.setInterval(load, 3000); // trace files grow while a run is going
    return () => window.clearInterval(timer);
  }, [q]);

  if (open) return <RunView runId={open} onBack={() => setOpen(null)} />;

  return (
    <>
      <header>
        <h2>Runs</h2>
        <p className="muted">Every agent run, recorded step by step. Open one to see its timeline, prompts and the Gateway's decision for each tool call.</p>
      </header>
      <div className="row toolbar">
        <input className="grow" placeholder="Search task or answer" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      <div className="card flush">
        <table>
          <thead>
            <tr><th>Started</th><th>Task</th><th>Status</th><th className="right">Model calls</th><th className="right">Tool calls</th><th className="right">Tokens</th><th className="right">Time</th></tr>
          </thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.run_id} className="clickable" onClick={() => setOpen(r.run_id)}>
                <td className="nowrap muted">{when(r.started_at)}</td>
                <td>{r.message}</td>
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

function RunView({ runId, onBack }: { runId: string; onBack: () => void }) {
  const [run, setRun] = useState<RunDetail | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const load = () => api<RunDetail>(`/api/runs/${runId}`).then(setRun).catch((e) => setError(e.message));
    load();
    const timer = window.setInterval(() => run?.status === "running" && load(), 2000);
    return () => window.clearInterval(timer);
  }, [runId, run?.status]);
  const { steps, total } = useMemo(() => buildSteps(run?.events ?? []), [run]);

  if (error) return <p className="error">{error}</p>;
  if (!run) return <p className="muted">Loading…</p>;
  return (
    <>
      <header>
        <button className="link" onClick={onBack}>← All runs</button>
        <h2>{run.message}</h2>
        <p className="muted small">
          run {run.run_id} · {run.model} · <span className={`pill status-${run.status}`}>{run.status}</span> · {secs(run.duration_ms)} ·{" "}
          {run.llm_calls} model calls · {run.tool_calls} tool calls · {(run.tokens_in).toLocaleString()} in / {(run.tokens_out).toLocaleString()} out tokens
        </p>
      </header>
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
