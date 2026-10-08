"""Audit log: one row per tool call, whatever the outcome. Stored in `<home>/audit.db`.

Long string arguments are cut to 500 characters, so file contents do not fill the log.

    python -m agentlab.gateway.audit list [--client NAME] [--tool GLOB] [--decision ACTION] [--limit N]
"""

import argparse
import fnmatch
import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agentlab.shared.home import agentlab_home

MAX_ARG_CHARS = 500

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, session_id TEXT, client TEXT, profile TEXT,
    tool TEXT NOT NULL, args_json TEXT, risk TEXT, decision TEXT, reason TEXT, rule TEXT,
    approval TEXT, snapshot TEXT, outcome TEXT, duration_ms INTEGER, run_id TEXT
);
CREATE INDEX IF NOT EXISTS calls_ts ON calls (ts);
"""
MIGRATIONS = [("run_id", "ALTER TABLE calls ADD COLUMN run_id TEXT")]


@dataclass
class AuditRecord:
    tool: str
    args: dict[str, Any]
    session_id: str = ""
    client: str = ""
    profile: str = ""
    risk: str | None = None
    decision: str | None = None  # the policy action, or "unknown_tool"
    reason: str | None = None
    rule: str | None = None
    approval: str | None = None  # approval id and its answer, for example "1a2b3c4d:approved"
    snapshot: str | None = None
    outcome: str | None = None  # ok | tool_error | denied | not_run
    duration_ms: int | None = None
    run_id: str | None = None  # the agent run (trace) this call belongs to, from request _meta
    ts: float = field(default_factory=time.time)


def _shorten(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_ARG_CHARS:
        return value[:MAX_ARG_CHARS] + f"... [{len(value)} chars]"
    if isinstance(value, dict):
        return {k: _shorten(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_shorten(v) for v in value]
    return value


class AuditLog:
    def __init__(self, home: Path | None = None):
        home = home or agentlab_home()
        home.mkdir(parents=True, exist_ok=True)
        self.db_path = home / "audit.db"
        with self._db() as db:
            db.executescript(SCHEMA)
            columns = {r["name"] for r in db.execute("PRAGMA table_info(calls)")}
            for column, sql in MIGRATIONS:
                if column not in columns:
                    db.execute(sql)
            db.execute("CREATE INDEX IF NOT EXISTS calls_run ON calls (run_id)")

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def write(self, r: AuditRecord) -> None:
        row = asdict(r)
        row["args_json"] = json.dumps(_shorten(row.pop("args")), default=str)
        cols = ", ".join(row)
        with self._db() as db:
            db.execute(f"INSERT INTO calls ({cols}) VALUES ({', '.join('?' * len(row))})", list(row.values()))

    def query(self, client: str | None = None, tool: str | None = None, decision: str | None = None,
              limit: int = 50, after_id: int | None = None, run_id: str | None = None) -> list[dict[str, Any]]:
        """Newest first. `after_id` returns only rows added after that id."""
        where, params = [], []
        if run_id:
            where.append("run_id = ?")
            params.append(run_id)
        if after_id is not None:
            where.append("id > ?")
            params.append(after_id)
        if client:
            where.append("client = ?")
            params.append(client)
        if decision:
            where.append("decision = ?")
            params.append(decision)
        sql = "SELECT * FROM calls" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC"
        if not tool:  # a tool glob is filtered in Python, so the limit applies after it
            sql += " LIMIT ?"
            params.append(limit)
        with self._db() as db:
            rows = [dict(r) for r in db.execute(sql, params)]
        if tool:
            rows = [r for r in rows if fnmatch.fnmatchcase(r["tool"], tool)]
        return rows[:limit]


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m agentlab.gateway.audit", description="Show the Gateway audit log.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("list")
    p.add_argument("--client")
    p.add_argument("--tool", help="glob, for example 'fs__*'")
    p.add_argument("--decision", help="allow, deny, ask, dry_run, allow_with_snapshot, unknown_tool")
    p.add_argument("--limit", type=int, default=30)
    a = parser.parse_args()
    for r in reversed(AuditLog().query(a.client, a.tool, a.decision, a.limit)):
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"]))
        extra = "".join(f"  {k}={r[k]}" for k in ("approval", "snapshot") if r[k])
        print(f"{when}  {r['client']:<14} {r['tool']:<24} {r['decision'] or '-':<20} {r['outcome'] or '-':<10}{extra}")
        print(f"    {r['reason']}  args={r['args_json']}")


if __name__ == "__main__":
    main()
