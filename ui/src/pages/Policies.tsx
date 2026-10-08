import { useEffect, useState } from "react";
import { api, post } from "../api";
import type { ProfileInfo, RuleInfo, SimulateResult } from "../api";
import { Action } from "./common";

export function PoliciesPage() {
  const [profiles, setProfiles] = useState<ProfileInfo[]>([]);
  const [selected, setSelected] = useState("");
  useEffect(() => {
    api<ProfileInfo[]>("/api/profiles").then((p) => {
      setProfiles(p);
      setSelected(p.find((x) => x.name === "coding")?.name ?? p[0]?.name ?? "");
    });
  }, []);
  const profile = profiles.find((p) => p.name === selected);

  return (
    <>
      <header>
        <h2>Policies</h2>
        <p className="muted">Profiles from <code>config/profiles/</code>. Guards first, then rules; the first match wins; no match means deny.</p>
      </header>
      <div className="tabs">
        {profiles.map((p) => (
          <button key={p.name} className={p.name === selected ? "active" : ""} onClick={() => setSelected(p.name)}>{p.name}</button>
        ))}
      </div>
      {profile && (
        <>
          <Simulator profile={profile.name} />
          <div className="card">
            <p className="small">
              Visible tools: <code>{profile.visible.join(", ")}</code>
              {profile.visible_risk && <> · only risk <code>{profile.visible_risk.join(", ")}</code></>}
            </p>
            <RuleTable title="Guards (cannot be overridden)" rules={profile.guards} />
            <RuleTable title="Rules" rules={profile.rules} />
            <p className="muted small">No match: <Action action="deny" /></p>
            {profile.budgets.length > 0 && (
              <p className="small">
                Budgets:{" "}
                {profile.budgets.map((b) => (
                  <code key={b.pattern} className="spaced">
                    {b.pattern}: {b.max_per_session ? `${b.max_per_session}/session ` : ""}{b.max_per_minute ? `${b.max_per_minute}/min` : ""}
                  </code>
                ))}
              </p>
            )}
          </div>
        </>
      )}
    </>
  );
}

function RuleTable({ title, rules }: { title: string; rules: RuleInfo[] }) {
  if (rules.length === 0) return null;
  return (
    <>
      <h4>{title}</h4>
      <table>
        <tbody>
          {rules.map((r) => (
            <tr key={r.source}>
              <td className="muted nowrap">{r.source}</td>
              <td><Action action={r.action} /></td>
              <td><code className="wrap">{JSON.stringify(r.match)}</code>{r.reason && <div className="muted small">{r.reason}</div>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

function Simulator({ profile }: { profile: string }) {
  const [tool, setTool] = useState("fs__read_file");
  const [args, setArgs] = useState('{"path": "~/.ssh/id_rsa"}');
  const [result, setResult] = useState<SimulateResult | null>(null);
  const [error, setError] = useState("");

  const run = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    try {
      setResult(await post<SimulateResult>("/api/simulate", { profile, tool, args: JSON.parse(args || "{}") }));
    } catch (err) {
      setResult(null);
      setError(err instanceof SyntaxError ? "Arguments are not valid JSON." : String((err as Error).message));
    }
  };

  return (
    <form className="card" onSubmit={run}>
      <h4>Simulator</h4>
      <p className="muted small">Ask what <b>{profile}</b> decides for a call. Nothing runs.</p>
      <div className="row">
        <input value={tool} onChange={(e) => setTool(e.target.value)} placeholder="server__tool" />
        <input className="grow mono" value={args} onChange={(e) => setArgs(e.target.value)} placeholder='{"path": "..."}' />
        <button className="primary">Check</button>
      </div>
      {error && <p className="error">{error}</p>}
      {result && (
        <p className="row">
          <Action action={result.action} />
          <span>{result.reason}</span>
          <span className="muted small">
            rule {result.rule ?? "none"} · risk {result.risk ?? "unknown"}
            {!result.tool_exists && " · tool not found on any server"}
          </span>
        </p>
      )}
    </form>
  );
}
