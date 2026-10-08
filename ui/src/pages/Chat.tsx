import { useEffect, useRef, useState } from "react";
import { api, getToken } from "../api";

type ChatStatus = { available: boolean; reason?: string; model?: string; num_ctx?: number };

type ToolStep = { id: string; name: string; args: unknown; result?: string; is_error?: boolean };
type Turn = { role: "user" | "agent"; text: string; thinking: string; tools: ToolStep[]; error?: string; runId?: string; busy?: boolean };

const newThread = () => crypto.randomUUID().replace(/-/g, "");

export function ChatPage({ pendingApprovals, onOpenApprovals }: { pendingApprovals: number; onOpenApprovals: () => void }) {
  const [status, setStatus] = useState<ChatStatus | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [thread, setThread] = useState(newThread);
  const busy = turns.some((t) => t.busy);
  const end = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api<ChatStatus>("/api/chat/status").then(setStatus).catch((e) => setStatus({ available: false, reason: e.message }));
  }, []);
  useEffect(() => end.current?.scrollIntoView({ behavior: "smooth" }), [turns]);

  const update = (fn: (t: Turn) => Turn) => setTurns((all) => [...all.slice(0, -1), fn(all[all.length - 1])]);

  const send = async (e: React.FormEvent) => {
    e.preventDefault();
    const message = input.trim();
    if (!message || busy) return;
    setInput("");
    setTurns((all) => [...all, { role: "user", text: message, thinking: "", tools: [] },
      { role: "agent", text: "", thinking: "", tools: [], busy: true }]);
    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${getToken() ?? ""}` },
        body: JSON.stringify({ message, thread_id: thread }),
      });
      if (!res.ok || !res.body) throw new Error((await res.json().catch(() => ({}))).detail ?? res.statusText);
      const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += value;
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";
        for (const line of lines) if (line.trim()) apply(JSON.parse(line));
      }
    } catch (err) {
      update((t) => ({ ...t, error: String((err as Error).message) }));
    }
    update((t) => ({ ...t, busy: false }));
  };

  const apply = (ev: Record<string, any>) => {
    switch (ev.type) {
      case "run_start": return update((t) => ({ ...t, runId: ev.run_id }));
      case "thinking": return update((t) => ({ ...t, thinking: t.thinking + ev.text }));
      case "token": return update((t) => ({ ...t, text: t.text + ev.text }));
      case "tool_call": return update((t) => ({ ...t, tools: [...t.tools, { id: ev.id, name: ev.name, args: ev.args }] }));
      case "tool_result":
        return update((t) => ({ ...t, tools: t.tools.map((s) => (s.id === ev.id ? { ...s, result: ev.content, is_error: ev.is_error } : s)) }));
      case "error": return update((t) => ({ ...t, error: ev.message }));
    }
  };

  return (
    <div className="chat">
      <header className="row">
        <div className="grow">
          <h2>Chat</h2>
          <p className="muted">
            The AgentLab agent on {status?.model ?? "a local model"}. It uses tools only through this Gateway, so policy, approvals and snapshots apply.
          </p>
        </div>
        <button onClick={() => { setTurns([]); setThread(newThread()); }} disabled={busy}>New chat</button>
      </header>
      {status && !status.available && <p className="notice error">Chat unavailable: {status.reason}</p>}
      <div className="messages">
        {turns.length === 0 && (
          <div className="empty">Try: “What is in my agentlab-sandbox folder?” or “Show git status of ~/repositories/agentlab”.</div>
        )}
        {turns.map((t, i) => t.role === "user" ? (
          <div key={i} className="bubble user">{t.text}</div>
        ) : (
          <div key={i} className="bubble agent">
            {t.thinking && (
              <details className="thinking"><summary>Thinking</summary><pre>{t.thinking}</pre></details>
            )}
            {t.tools.map((s) => (
              <details key={s.id} className={`step ${s.result === undefined ? "running" : s.is_error ? "failed" : "done"}`}>
                <summary>
                  <code>{s.name}</code>
                  <span className="muted small"> {s.result === undefined ? "running…" : s.is_error ? "error" : "done"}</span>
                </summary>
                <pre>{JSON.stringify(s.args, null, 2)}</pre>
                {s.result !== undefined && <pre className={s.is_error ? "error" : ""}>{s.result}</pre>}
              </details>
            ))}
            {t.busy && pendingApprovals > 0 && t.tools.some((s) => s.result === undefined) && (
              <p className="notice">A call is waiting for approval. <button className="link" onClick={onOpenApprovals}>Open Approvals</button></p>
            )}
            {t.text && <div className="answer">{t.text}</div>}
            {t.busy && !t.text && <span className="muted small">working…</span>}
            {t.error && <p className="error">{t.error}</p>}
            {t.runId && !t.busy && <p className="muted small">run {t.runId}</p>}
          </div>
        ))}
        <div ref={end} />
      </div>
      <form className="row composer" onSubmit={send}>
        <input className="grow" value={input} onChange={(e) => setInput(e.target.value)} placeholder="Ask the agent to do something on this computer" disabled={!status?.available} />
        <button className="primary" disabled={busy || !input.trim() || !status?.available}>Send</button>
      </form>
    </div>
  );
}
