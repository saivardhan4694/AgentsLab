import { useEffect, useState } from "react";
import { api } from "../api";
import type { ServerInfo } from "../api";
import { Risk } from "./common";

export function ServersPage() {
  const [servers, setServers] = useState<ServerInfo[] | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    api<ServerInfo[]>("/api/servers").then(setServers).catch((e) => setError(e.message));
  }, []);

  return (
    <>
      <header>
        <h2>Servers and tools</h2>
        <p className="muted">Downstream MCP servers of this Gateway, from <code>config/servers.yaml</code>.</p>
      </header>
      {error && <p className="error">{error}</p>}
      {servers?.map((s) => (
        <div className="card" key={s.name}>
          <div className="row">
            <span className={s.healthy ? "dot on" : "dot off"} />
            <h3 className="grow">{s.name}</h3>
            {s.trust_meta && <span className="pill">trusted metadata</span>}
            <span className="muted small">{s.tools.length} tools</span>
          </div>
          {s.error && <p className="error">{s.error}</p>}
          <table>
            <tbody>
              {s.tools.map((t) => (
                <tr key={t.name}>
                  <td><code>{t.name}</code></td>
                  <td><Risk level={t.risk} /></td>
                  <td className="muted">{t.description.split("\n")[0]}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </>
  );
}
