import { Fragment, useMemo, useState } from "react";
import { time } from "../api";
import type { AuditRow } from "../api";
import { Action, Risk } from "./common";

export function AuditPage({ rows }: { rows: AuditRow[] }) {
  const [filter, setFilter] = useState("");
  const [decision, setDecision] = useState("");
  const [open, setOpen] = useState<number | null>(null);

  const decisions = useMemo(() => [...new Set(rows.map((r) => r.decision).filter(Boolean))] as string[], [rows]);
  const shown = rows.filter(
    (r) =>
      (!decision || r.decision === decision) &&
      (!filter || `${r.client} ${r.tool} ${r.args_json} ${r.reason}`.toLowerCase().includes(filter.toLowerCase())),
  );

  return (
    <>
      <header>
        <h2>Audit log</h2>
        <p className="muted">Every tool call through any Gateway on this machine, newest first.</p>
      </header>
      <div className="row toolbar">
        <input className="grow" placeholder="Filter by client, tool, arguments, reason" value={filter} onChange={(e) => setFilter(e.target.value)} />
        <select value={decision} onChange={(e) => setDecision(e.target.value)}>
          <option value="">All decisions</option>
          {decisions.map((d) => <option key={d}>{d}</option>)}
        </select>
      </div>
      <div className="card flush">
        <table>
          <thead>
            <tr><th>Time</th><th>Client</th><th>Tool</th><th>Risk</th><th>Decision</th><th>Outcome</th><th>ms</th></tr>
          </thead>
          <tbody>
            {shown.map((r) => (
              <Fragment key={r.id}>
                <tr className="clickable" onClick={() => setOpen(open === r.id ? null : r.id)}>
                  <td className="nowrap muted">{time(r.ts)}</td>
                  <td>{r.client}</td>
                  <td><code>{r.tool}</code></td>
                  <td><Risk level={r.risk} /></td>
                  <td><Action action={r.decision} /></td>
                  <td className={r.outcome === "ok" ? "ok" : "muted"}>{r.outcome}</td>
                  <td className="muted">{r.duration_ms}</td>
                </tr>
                {open === r.id && (
                  <tr className="detail">
                    <td colSpan={7}>
                      <dl>
                        <dt>Reason</dt><dd>{r.reason}</dd>
                        <dt>Rule</dt><dd>{r.rule ?? "-"}</dd>
                        <dt>Profile</dt><dd>{r.profile}</dd>
                        <dt>Session</dt><dd>{r.session_id}</dd>
                        {r.approval && <><dt>Approval</dt><dd>{r.approval}</dd></>}
                        {r.snapshot && <><dt>Snapshot</dt><dd><code>{r.snapshot}</code></dd></>}
                        <dt>Arguments</dt><dd><pre>{JSON.stringify(JSON.parse(r.args_json), null, 2)}</pre></dd>
                      </dl>
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
        {shown.length === 0 && <div className="empty">No calls yet.</div>}
      </div>
    </>
  );
}
