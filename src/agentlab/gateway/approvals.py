"""Human approvals for tool calls with the `ask` action.

The Gateway writes a pending request to `<home>/approvals.db` and waits. A human answers from
another terminal (the React UI comes later and uses the same store). This works even when
Claude Desktop launches the Gateway and no terminal is attached to it.
No answer before the timeout means deny.

    python -m agentlab.gateway.approvals watch          # prompt for each new request
    python -m agentlab.gateway.approvals list
    python -m agentlab.gateway.approvals approve <id> [--args '{"path": "..."}']
    python -m agentlab.gateway.approvals deny <id> [--reason "..."]
"""

import argparse
import json
import sqlite3
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio

from agentlab.shared.home import agentlab_home

SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, created REAL NOT NULL,
    tool TEXT NOT NULL, args_json TEXT NOT NULL, risk TEXT, reason TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'denied', 'expired')),
    decided_args_json TEXT, decided_reason TEXT, decided_at REAL
);
"""


@dataclass
class Approval:
    id: str
    session_id: str
    created: float
    tool: str
    args: dict[str, Any]
    risk: str | None
    reason: str | None
    status: str
    decided_args: dict[str, Any] | None
    decided_reason: str | None

    @classmethod
    def from_row(cls, r: tuple) -> "Approval":
        return cls(r[0], r[1], r[2], r[3], json.loads(r[4]), r[5], r[6], r[7],
                   json.loads(r[8]) if r[8] else None, r[9])


class ApprovalStore:
    def __init__(self, home: Path | None = None):
        home = home or agentlab_home()
        home.mkdir(parents=True, exist_ok=True)
        self.db_path = home / "approvals.db"
        with self._db() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.db_path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, session_id: str, tool: str, args: dict[str, Any], risk: str | None, reason: str | None) -> str:
        approval_id = uuid.uuid4().hex[:8]
        with self._db() as db:
            db.execute(
                "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, NULL)",
                (approval_id, session_id, time.time(), tool, json.dumps(args, default=str), risk, reason),
            )
        return approval_id

    def get(self, approval_id: str) -> Approval | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        return Approval.from_row(row) if row else None

    def pending(self) -> list[Approval]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM approvals WHERE status = 'pending' ORDER BY created").fetchall()
        return [Approval.from_row(r) for r in rows]

    def recent(self, limit: int = 50) -> list[Approval]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM approvals ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
        return [Approval.from_row(r) for r in rows]

    def decide(self, approval_id: str, status: str, args: dict[str, Any] | None = None, reason: str | None = None) -> bool:
        """Answer a pending request. Returns False if it is not pending (already answered or expired)."""
        if status not in ("approved", "denied", "expired"):
            raise ValueError(f"Bad status {status!r}")
        with self._db() as db:
            cur = db.execute(
                "UPDATE approvals SET status = ?, decided_args_json = ?, decided_reason = ?, decided_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (status, json.dumps(args) if args is not None else None, reason, time.time(), approval_id),
            )
        return cur.rowcount == 1

    async def wait(self, approval_id: str, timeout: float, poll: float = 0.3) -> Approval:
        """Block until the request is answered, or mark it expired after `timeout` seconds."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            approval = self.get(approval_id)
            if approval is not None and approval.status != "pending":
                return approval
            await anyio.sleep(poll)
        self.decide(approval_id, "expired", reason=f"no answer within {timeout:g} s")
        approval = self.get(approval_id)  # an answer may have won the race
        assert approval is not None
        return approval


# CLI


def describe(a: Approval) -> str:
    when = time.strftime("%H:%M:%S", time.localtime(a.created))
    return (f"[{a.id}] {when}  {a.tool}  risk={a.risk}  session={a.session_id}\n"
            f"    why asked: {a.reason}\n"
            f"    args: {json.dumps(a.args, indent=None)}")


def watch(store: ApprovalStore) -> None:
    print("Watching for approval requests. Ctrl+C to stop.")
    seen: set[str] = set()
    while True:
        for a in store.pending():
            if a.id in seen:
                continue
            seen.add(a.id)
            print("\n" + describe(a))
            answer = input("    approve? [y]es / [n]o / [e]dit args: ").strip().lower()
            if answer.startswith("e"):
                edited = json.loads(input("    new args JSON: "))
                ok = store.decide(a.id, "approved", args=edited)
            elif answer.startswith("y"):
                ok = store.decide(a.id, "approved")
            else:
                ok = store.decide(a.id, "denied", reason=input("    reason (optional): ").strip() or None)
            print("    done" if ok else "    too late: the request already expired")
        time.sleep(0.5)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m agentlab.gateway.approvals", description="Answer Gateway approval requests.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("watch")
    sub.add_parser("list")
    p_ok = sub.add_parser("approve")
    p_ok.add_argument("id")
    p_ok.add_argument("--args", help="replacement arguments as JSON")
    p_no = sub.add_parser("deny")
    p_no.add_argument("id")
    p_no.add_argument("--reason")
    a = parser.parse_args()

    store = ApprovalStore()
    if a.command == "watch":
        try:
            watch(store)
        except KeyboardInterrupt:
            pass
    elif a.command == "list":
        pending = store.pending()
        print("\n".join(describe(p) for p in pending) or "no pending requests")
    else:
        status = "approved" if a.command == "approve" else "denied"
        args = json.loads(a.args) if getattr(a, "args", None) else None
        if not store.decide(a.id, status, args=args, reason=getattr(a, "reason", None)):
            sys.exit(f"Request {a.id} is not pending")
        print(f"{a.id}: {status}")


if __name__ == "__main__":
    main()
