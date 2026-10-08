import { useEffect, useState } from "react";
import { api, ApiError, getToken, liveSocket, setToken, takeTokenFromUrl } from "./api";
import type { Approval, AuditRow, Overview } from "./api";
import { ApprovalsPage } from "./pages/Approvals";
import { AuditPage } from "./pages/Audit";
import { ChatPage } from "./pages/Chat";
import { PoliciesPage } from "./pages/Policies";
import { RunsPage } from "./pages/Runs";
import { ServersPage } from "./pages/Servers";
import { SnapshotsPage } from "./pages/Snapshots";

const PAGES = ["Chat", "Approvals", "Runs", "Audit", "Servers", "Policies", "Snapshots"] as const;
type Page = (typeof PAGES)[number];

takeTokenFromUrl();

export function App() {
  const [authed, setAuthed] = useState<boolean | null>(null);
  const [overview, setOverview] = useState<Overview | null>(null);

  useEffect(() => {
    if (!getToken()) return setAuthed(false);
    api<Overview>("/api/overview")
      .then((o) => {
        setOverview(o);
        setAuthed(true);
      })
      .catch((e) => setAuthed(e instanceof ApiError && e.status === 401 ? false : true));
  }, []);

  if (authed === null) return <div className="center muted">Connecting…</div>;
  if (!authed) return <Login />;
  return <Shell overview={overview} />;
}

function Login() {
  const [value, setValue] = useState("");
  const [error, setError] = useState("");
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setToken(value.trim());
    try {
      await api("/api/overview");
      window.location.reload();
    } catch {
      setToken(null);
      setError("That token was not accepted.");
    }
  };
  return (
    <div className="center">
      <form className="card login" onSubmit={submit}>
        <h1>AgentLab Gateway</h1>
        <p className="muted">
          Paste the admin token. The Gateway prints a link with it at startup, and keeps it in
          <code> ~/.agentlab/admin_token</code>.
        </p>
        <input autoFocus type="password" value={value} onChange={(e) => setValue(e.target.value)} placeholder="Admin token" />
        {error && <p className="error">{error}</p>}
        <button className="primary" disabled={!value.trim()}>Open</button>
      </form>
    </div>
  );
}

function Shell({ overview }: { overview: Overview | null }) {
  const [page, setPage] = useState<Page>("Chat");
  const [pending, setPending] = useState<Approval[]>([]);
  const [audit, setAudit] = useState<AuditRow[]>([]);
  const [live, setLive] = useState(false);

  useEffect(() => {
    api<AuditRow[]>("/api/audit?limit=200").then(setAudit).catch(() => {});
    return liveSocket(
      (msg) => {
        if (msg.type === "approvals") setPending(msg.items);
        else setAudit((rows) => [...msg.items, ...rows].slice(0, 1000));
      },
      setLive,
    );
  }, []);

  useEffect(() => {
    document.title = pending.length ? `(${pending.length}) AgentLab Gateway` : "AgentLab Gateway";
  }, [pending.length]);

  return (
    <div className="shell">
      <aside>
        <div className="brand">AgentLab<span>Gateway</span></div>
        <nav>
          {PAGES.map((p) => (
            <button key={p} className={p === page ? "active" : ""} onClick={() => setPage(p)}>
              {p}
              {p === "Approvals" && pending.length > 0 && <span className="badge">{pending.length}</span>}
            </button>
          ))}
        </nav>
        <div className="status">
          <span className={live ? "dot on" : "dot"} /> {live ? "Live" : "Reconnecting…"}
          {overview && (
            <div className="muted small">
              {overview.servers.healthy}/{overview.servers.total} servers · {overview.tools} tools
              <br />
              {overview.clients.length} HTTP clients
            </div>
          )}
          <button className="link small" onClick={() => { setToken(null); window.location.reload(); }}>Sign out</button>
        </div>
      </aside>
      <main>
        {/* Chat stays mounted, so a running turn keeps streaming while another page is open. */}
        <div hidden={page !== "Chat"}>
          <ChatPage pendingApprovals={pending.length} onOpenApprovals={() => setPage("Approvals")} />
        </div>
        {page === "Approvals" && <ApprovalsPage pending={pending} />}
        {page === "Runs" && <RunsPage />}
        {page === "Audit" && <AuditPage rows={audit} />}
        {page === "Servers" && <ServersPage />}
        {page === "Policies" && <PoliciesPage />}
        {page === "Snapshots" && <SnapshotsPage />}
      </main>
    </div>
  );
}
