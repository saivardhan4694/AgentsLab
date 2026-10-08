// Admin API client. The admin token comes from ?token= on first open, then lives in sessionStorage.

const TOKEN_KEY = "agentlab-admin-token";

export function takeTokenFromUrl(): void {
  const url = new URL(window.location.href);
  const token = url.searchParams.get("token");
  if (token) {
    setToken(token);
    url.searchParams.delete("token");
    window.history.replaceState(null, "", url.toString()); // keep the token out of history and screenshots
  }
}

export function getToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null): void {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage blocked: the token lasts for this page only */
  }
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${getToken() ?? ""}`, ...init.headers },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

export const post = <T>(path: string, body?: unknown) =>
  api<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export function liveSocket(onMessage: (msg: LiveMessage) => void, onState: (open: boolean) => void): () => void {
  let ws: WebSocket | null = null;
  let stopped = false;
  let retry: number | undefined;
  const open = () => {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${window.location.host}/api/ws?token=${encodeURIComponent(getToken() ?? "")}`);
    ws.onopen = () => onState(true);
    ws.onmessage = (e) => onMessage(JSON.parse(e.data));
    ws.onclose = () => {
      onState(false);
      if (!stopped) retry = window.setTimeout(open, 2000);
    };
  };
  open();
  return () => {
    stopped = true;
    window.clearTimeout(retry);
    ws?.close();
  };
}

// Types mirror src/agentlab/gateway/api.py

export type Approval = {
  id: string;
  session_id: string;
  created: number;
  tool: string;
  args: Record<string, unknown>;
  risk: string | null;
  reason: string | null;
  status: "pending" | "approved" | "denied" | "expired";
  decided_args: Record<string, unknown> | null;
  decided_reason: string | null;
};

export type AuditRow = {
  id: number;
  ts: number;
  session_id: string;
  client: string;
  profile: string;
  tool: string;
  args_json: string;
  risk: string | null;
  decision: string | null;
  reason: string | null;
  rule: string | null;
  approval: string | null;
  snapshot: string | null;
  outcome: string | null;
  duration_ms: number | null;
};

export type LiveMessage = { type: "approvals"; items: Approval[] } | { type: "audit"; items: AuditRow[] };

export type Overview = {
  clients: { name: string; profile: string; session_id: string }[];
  servers: { total: number; healthy: number };
  tools: number;
  pending_approvals: number;
  home: string;
};

export type ServerInfo = {
  name: string;
  healthy: boolean;
  error: string | null;
  trust_meta: boolean;
  tools: { name: string; description: string; risk: string; read_only: boolean; destructive: boolean }[];
};

export type RuleInfo = { source: string; action: string; match: Record<string, unknown>; reason: string | null };

export type ProfileInfo = {
  name: string;
  visible: string[];
  visible_risk: string[] | null;
  guards: RuleInfo[];
  rules: RuleInfo[];
  budgets: { pattern: string; max_per_session: number | null; max_per_minute: number | null }[];
};

export type SimulateResult = {
  tool_exists: boolean;
  risk: string | null;
  visible: boolean;
  action: string;
  rule: string | null;
  reason: string;
};

export type Snapshot = {
  id: string;
  session_id: string;
  ts: number;
  tool: string;
  args: Record<string, unknown>;
  rolled_back_at: number | null;
  paths: string[];
};

export const time = (ts: number) =>
  new Date(ts * 1000).toLocaleString(undefined, { hour12: false, month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });

export type RunSummary = {
  run_id: string;
  thread_id: string | null;
  started_at: string;
  ended_at: string | null;
  model: string | null;
  message: string | null;
  answer: string | null;
  status: "ok" | "error" | "running";
  steps: number;
  llm_calls: number;
  tool_calls: number;
  tool_errors: number;
  errors: number;
  tokens_in: number;
  tokens_out: number;
  duration_ms: number;
};

export type TraceEventRow = {
  run_id: string;
  step: number;
  parent_step: number | null;
  ts: string;
  type: "run_start" | "llm_call" | "tool_call" | "tool_result" | "policy_decision" | "approval" | "error" | "run_end";
  payload: Record<string, any>;
  latency_ms: number | null;
  tokens_in: number | null;
  tokens_out: number | null;
  gateway?: AuditRow | null;
};

export type RunDetail = RunSummary & { events: TraceEventRow[] };
