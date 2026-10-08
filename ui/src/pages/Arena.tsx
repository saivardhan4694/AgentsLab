import { useCallback, useEffect, useRef, useState } from "react";
import { api, post } from "../api";
import type { ArenaAttack, ArenaStatus, MatrixResult } from "../api";

export function ArenaPage() {
  const [attacks, setAttacks] = useState<ArenaAttack[]>([]);
  const [defenses, setDefenses] = useState<string[]>([]);
  const [scenario, setScenario] = useState<string>("");
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [trials, setTrials] = useState(5);
  const [status, setStatus] = useState<ArenaStatus | null>(null);
  const [error, setError] = useState("");
  const poll = useRef<number | undefined>(undefined);

  useEffect(() => {
    api<ArenaAttack[]>("/api/arena/attacks").then(setAttacks);
    api<string[]>("/api/arena/defenses").then((d) => {
      setDefenses(d);
      setPicked(new Set(d));
    });
    loadStatus();
  }, []);

  const loadStatus = useCallback(() => {
    api<ArenaStatus>("/api/arena/status").then((s) => {
      setStatus(s);
      if (s.running) poll.current = window.setTimeout(loadStatus, 1000);
    });
  }, []);
  useEffect(() => () => window.clearTimeout(poll.current), []);

  const scenarios = [...new Set(attacks.map((a) => a.scenario))];

  const run = async () => {
    setError("");
    try {
      await post("/api/arena/run", { scenario: scenario || null, defenses: [...picked], trials });
      loadStatus();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const toggle = (d: string) => {
    const next = new Set(picked);
    next.has(d) ? next.delete(d) : next.add(d);
    setPicked(next);
  };

  return (
    <>
      <header>
        <h2>Arena</h2>
        <p className="muted">
          Runs the AgentLab agent against fake scenario servers with planted prompt-injection attacks, through a
          Gateway where the defense under test is wired in as a plugin. Default policy is wide open: the variable
          is the defense, not the rule engine. Each cell runs several trials; a no-attack baseline row per
          scenario separates a plain model mistake from an attack actually succeeding.
        </p>
      </header>

      <div className="card">
        <div className="row">
          <label className="grow">
            Scenario
            <select value={scenario} onChange={(e) => setScenario(e.target.value)} style={{ marginLeft: 8 }}>
              <option value="">all ({attacks.length} attacks)</option>
              {scenarios.map((s) => (
                <option key={s} value={s}>{s} ({attacks.filter((a) => a.scenario === s).length})</option>
              ))}
            </select>
          </label>
          <label>
            Trials
            <input type="number" min={1} max={20} value={trials} style={{ width: 56, marginLeft: 8 }}
                  onChange={(e) => setTrials(Math.max(1, Number(e.target.value) || 1))} />
          </label>
        </div>
        <div className="row" style={{ flexWrap: "wrap", gap: 12 }}>
          {defenses.map((d) => (
            <label key={d} className="small">
              <input type="checkbox" checked={picked.has(d)} onChange={() => toggle(d)} /> {d}
            </label>
          ))}
        </div>
        <div className="row">
          <button className="primary" disabled={!!status?.running || picked.size === 0} onClick={run}>
            {status?.running ? `Running… ${status.done}/${status.total}` : "Run matrix"}
          </button>
          {status && status.results.length > 0 && (
            <a className="link small" href={`/api/arena/report`} target="_blank" rel="noreferrer">
              Markdown report
            </a>
          )}
        </div>
        {error && <p className="error">{error}</p>}
        {status?.error && <p className="error">{status.error}</p>}
      </div>

      {status && status.results.length > 0 && <Matrix results={status.results} defenses={defenses} />}
      {!status?.running && attacks.length > 0 && (status?.results.length ?? 0) === 0 && (
        <div className="empty">No results yet. Pick defenses above and run the matrix.</div>
      )}
    </>
  );
}

function Matrix({ results, defenses }: { results: MatrixResult[]; defenses: string[] }) {
  const order = defenses.filter((d) => results.some((r) => r.defense === d));
  return (
    <>
      {order.map((defense) => {
        const rows = [...results.filter((r) => r.defense === defense)].sort(
          (a, b) => a.scenario.localeCompare(b.scenario) || Number(a.attack_id !== "baseline") - Number(b.attack_id !== "baseline"),
        );
        const attacked = rows.filter((r) => r.attack_id !== "baseline");
        const attackTrials = attacked.reduce((n, r) => n + r.trials, 0) || 1;
        const blocked = attacked.reduce((n, r) => n + (r.trials - r.attack_successes), 0);
        const taskTrials = rows.reduce((n, r) => n + r.trials, 0) || 1;
        const useful = rows.reduce((n, r) => n + r.task_successes, 0);
        return (
          <div className="card" key={defense}>
            <div className="row">
              <h3 className="grow">{defense}</h3>
              <span className="muted small">
                blocked {blocked}/{attackTrials} · task completion overall {useful}/{taskTrials}
              </span>
            </div>
            {defense === "sanitizer" && (
              <p className="muted small">
                Note: sanitizer's redaction patterns are broad by design and also match ordinary phrasing (for
                example "you must reply by Friday"). A lower task-success rate here can be this cost, not a bug.
              </p>
            )}
            <table>
              <thead>
                <tr>
                  <th>Scenario</th><th>Attack</th><th>Category</th><th>Attack success rate</th><th>Task success rate</th><th>Errors</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.attack_id} className={r.attack_id === "baseline" ? "muted" : ""}>
                    <td>{r.scenario}</td>
                    <td><code>{r.attack_id}</code></td>
                    <td className="muted small">{r.category}</td>
                    <td className={r.attack_successes > 0 ? "error" : "ok"}>{r.attack_successes}/{r.trials}</td>
                    <td className={r.task_successes === r.trials ? "ok" : r.task_successes === 0 ? "error" : ""}>
                      {r.task_successes}/{r.trials}
                    </td>
                    <td className="muted small">{r.errors.length || ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        );
      })}
    </>
  );
}
