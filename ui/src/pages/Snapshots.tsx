import { useCallback, useEffect, useState } from "react";
import { api, post, time } from "../api";
import type { Snapshot } from "../api";

export function SnapshotsPage() {
  const [snaps, setSnaps] = useState<Snapshot[]>([]);
  const [message, setMessage] = useState("");
  const load = useCallback(() => api<Snapshot[]>("/api/snapshots?limit=200").then(setSnaps), []);
  useEffect(() => {
    load();
  }, [load]);

  const act = async (label: string, path: string) => {
    if (!window.confirm(`${label}?\n\nFiles at these paths return to their earlier state. Later changes at the same paths are lost.`)) return;
    try {
      await post(path);
      setMessage(`${label}: done.`);
    } catch (e) {
      setMessage(`${label}: ${(e as Error).message}`);
    }
    load();
  };

  const sessions = [...new Set(snaps.map((s) => s.session_id))];

  return (
    <>
      <header>
        <h2>Snapshots</h2>
        <p className="muted">File states saved before each change. Roll back one call, or a whole session newest first.</p>
      </header>
      {message && <p className="notice">{message}</p>}
      {sessions.map((session) => {
        const items = snaps.filter((s) => s.session_id === session);
        const active = items.filter((s) => !s.rolled_back_at).length;
        return (
          <div className="card" key={session}>
            <div className="row">
              <h3 className="grow">Session {session}</h3>
              <span className="muted small">{active} active</span>
              <button disabled={!active} onClick={() => act(`Roll back session ${session}`, `/api/sessions/${session}/rollback`)}>Roll back session</button>
            </div>
            <table>
              <tbody>
                {items.map((s) => (
                  <tr key={s.id}>
                    <td className="muted nowrap">{time(s.ts)}</td>
                    <td><code>{s.tool}</code></td>
                    <td className="small">{s.paths.map((p) => <div key={p}><code className="wrap">{p}</code></div>)}</td>
                    <td className="right">
                      {s.rolled_back_at ? (
                        <span className="muted small">rolled back</span>
                      ) : (
                        <button onClick={() => act(`Roll back ${s.id}`, `/api/snapshots/${s.id}/rollback`)}>Roll back</button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        );
      })}
      {snaps.length === 0 && <div className="empty">No snapshots yet.</div>}
    </>
  );
}
