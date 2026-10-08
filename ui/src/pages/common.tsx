export function Risk({ level }: { level: string | null }) {
  return <span className={`pill risk-${level ?? "unknown"}`}>{level ?? "unknown"}</span>;
}

export function Action({ action }: { action: string | null }) {
  return <span className={`pill action-${action ?? "none"}`}>{action ?? "-"}</span>;
}
