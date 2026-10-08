import { useState } from "react";
import { post, time } from "../api";
import type { Approval } from "../api";
import { Risk } from "./common";

export function ApprovalsPage({ pending }: { pending: Approval[] }) {
  return (
    <>
      <header>
        <h2>Approvals</h2>
        <p className="muted">Calls waiting for a human. Unanswered requests are denied after the timeout.</p>
      </header>
      {pending.length === 0 ? (
        <div className="empty">Nothing is waiting. New requests appear here live.</div>
      ) : (
        pending.map((a) => <ApprovalCard key={a.id} approval={a} />)
      )}
    </>
  );
}

function ApprovalCard({ approval }: { approval: Approval }) {
  const original = JSON.stringify(approval.args, null, 2);
  const [args, setArgs] = useState(original);
  const [editing, setEditing] = useState(false);
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const answer = async (decision: "approve" | "deny") => {
    setError("");
    let edited: Record<string, unknown> | undefined;
    if (decision === "approve" && args !== original) {
      try {
        edited = JSON.parse(args);
      } catch {
        return setError("Arguments are not valid JSON.");
      }
    }
    setBusy(true);
    try {
      await post(`/api/approvals/${approval.id}`, { decision, args: edited, reason: reason || undefined });
    } catch (e) {
      setError(String((e as Error).message));
      setBusy(false);
    }
  };

  return (
    <div className="card approval">
      <div className="row">
        <code className="tool">{approval.tool}</code>
        <Risk level={approval.risk} />
        <span className="muted small grow">{approval.reason}</span>
        <span className="muted small">{time(approval.created)}</span>
      </div>
      {editing ? (
        <textarea value={args} onChange={(e) => setArgs(e.target.value)} rows={Math.min(14, args.split("\n").length + 1)} spellCheck={false} />
      ) : (
        <pre>{args}</pre>
      )}
      <div className="row">
        <button className="primary" disabled={busy} onClick={() => answer("approve")}>
          {args !== original ? "Approve with edits" : "Approve"}
        </button>
        <button className="danger" disabled={busy} onClick={() => answer("deny")}>Deny</button>
        <input className="grow" placeholder="Reason for deny (optional, shown to the agent)" value={reason} onChange={(e) => setReason(e.target.value)} />
        <button className="link" onClick={() => setEditing(!editing)}>{editing ? "Done editing" : "Edit arguments"}</button>
      </div>
      {args !== original && <p className="muted small">Edited arguments go through the policy again before the call runs.</p>}
      {error && <p className="error">{error}</p>}
      <p className="muted small">id {approval.id} · session {approval.session_id}</p>
    </div>
  );
}
