"""File snapshots and rollback.

Before a tool call that writes files runs, the Gateway records the current state of every path
the call will change: a file (content stored by SHA-256 hash), a directory tree, or "absent".
Rollback restores each path to exactly that state. Anything created at that path later is removed.

Blobs live in `<home>/snapshots/blobs/`, the index in `<home>/snapshots.db`.

    python -m agentlab.gateway.snapshots list
    python -m agentlab.gateway.snapshots rollback <snapshot_id>
    python -m agentlab.gateway.snapshots rollback --session <session_id>
"""

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentlab.shared.home import agentlab_home

MAX_BYTES = 500_000_000
MAX_FILES = 20_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, ts REAL NOT NULL,
    tool TEXT NOT NULL, args_json TEXT NOT NULL, rolled_back_at REAL
);
CREATE TABLE IF NOT EXISTS entries (
    snapshot_id TEXT NOT NULL, root TEXT NOT NULL, rel TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('file', 'dir', 'absent')), blob TEXT
);
"""


class SnapshotError(Exception):
    pass


@dataclass
class Snapshot:
    id: str
    session_id: str
    ts: float
    tool: str
    args: dict[str, Any]
    rolled_back_at: float | None
    paths: list[str]


class SnapshotStore:
    def __init__(self, home: Path | None = None, max_bytes: int = MAX_BYTES, max_files: int = MAX_FILES):
        home = home or agentlab_home()
        self.blobs = home / "snapshots" / "blobs"
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.db_path = home / "snapshots.db"
        self.max_bytes, self.max_files = max_bytes, max_files
        with self._db() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.db_path, timeout=10)
        try:
            with db:  # commit on success, roll back on error
                yield db
        finally:
            db.close()

    # Taking

    def take(self, session_id: str, tool: str, args: dict[str, Any], paths: list[Path]) -> str:
        entries: list[tuple[str, str, str, str | None]] = []
        budget = {"bytes": 0, "files": 0}
        for root in dict.fromkeys(p.resolve() for p in paths):
            entries.extend(self._capture(root, budget))
        snap_id = uuid.uuid4().hex[:12]
        with self._db() as db:
            db.execute(
                "INSERT INTO snapshots VALUES (?, ?, ?, ?, ?, NULL)",
                (snap_id, session_id, time.time(), tool, json.dumps(args, default=str)),
            )
            db.executemany("INSERT INTO entries VALUES (?, ?, ?, ?, ?)", [(snap_id, *e) for e in entries])
        return snap_id

    def _capture(self, root: Path, budget: dict[str, int]) -> list[tuple[str, str, str, str | None]]:
        if root.is_symlink():
            raise SnapshotError(f"Cannot snapshot a symlink: {root}")
        if not root.exists():
            return [(str(root), "", "absent", None)]
        if root.is_file():
            return [(str(root), "", "file", self._store(root, budget))]
        out = [(str(root), "", "dir", None)]
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root).as_posix()
            if p.is_symlink():
                raise SnapshotError(f"Cannot snapshot a symlink: {p}")
            if p.is_dir():
                out.append((str(root), rel, "dir", None))
            else:
                out.append((str(root), rel, "file", self._store(p, budget)))
        return out

    def _store(self, path: Path, budget: dict[str, int]) -> str:
        budget["files"] += 1
        budget["bytes"] += path.stat().st_size
        if budget["files"] > self.max_files or budget["bytes"] > self.max_bytes:
            raise SnapshotError(f"Too much to snapshot (limit {self.max_files} files, {self.max_bytes} bytes)")
        h = hashlib.sha256()
        with path.open("rb") as f:
            while chunk := f.read(1 << 20):
                h.update(chunk)
        digest = h.hexdigest()
        blob = self.blobs / digest[:2] / digest
        if not blob.exists():
            blob.parent.mkdir(exist_ok=True)
            tmp = blob.with_suffix(".tmp")
            shutil.copyfile(path, tmp)
            tmp.replace(blob)
        return digest

    # Reading

    def get(self, snap_id: str) -> Snapshot:
        with self._db() as db:
            row = db.execute("SELECT * FROM snapshots WHERE id = ?", (snap_id,)).fetchone()
            if row is None:
                raise SnapshotError(f"No snapshot {snap_id}")
            roots = [r[0] for r in db.execute("SELECT DISTINCT root FROM entries WHERE snapshot_id = ?", (snap_id,))]
        return Snapshot(row[0], row[1], row[2], row[3], json.loads(row[4]), row[5], roots)

    def recent(self, session_id: str | None = None, limit: int = 50) -> list[Snapshot]:
        with self._db() as db:
            query = "SELECT id FROM snapshots" + (" WHERE session_id = ?" if session_id else "") + " ORDER BY ts DESC, rowid DESC LIMIT ?"
            ids = [r[0] for r in db.execute(query, (*([session_id] if session_id else []), limit))]
        return [self.get(i) for i in ids]

    # Rollback

    def rollback(self, snap_id: str) -> list[str]:
        snap = self.get(snap_id)
        if snap.rolled_back_at is not None:
            raise SnapshotError(f"Snapshot {snap_id} was already rolled back")
        with self._db() as db:
            rows = db.execute(
                "SELECT root, rel, kind, blob FROM entries WHERE snapshot_id = ? ORDER BY root, rel", (snap_id,)
            ).fetchall()
        for root in snap.paths:
            self._remove(Path(root))
        for root, rel, kind, blob in rows:  # sorted by rel, so parents come before children
            target = Path(root) / rel if rel else Path(root)
            if kind == "dir":
                target.mkdir(parents=True, exist_ok=True)
            elif kind == "file":
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self.blobs / blob[:2] / blob, target)
        with self._db() as db:
            db.execute("UPDATE snapshots SET rolled_back_at = ? WHERE id = ?", (time.time(), snap_id))
        return snap.paths

    def rollback_session(self, session_id: str) -> list[str]:
        # Newest first, so each call is undone on top of the state the next call left behind.
        done: list[str] = []
        for snap in self.recent(session_id, limit=1_000_000):
            if snap.rolled_back_at is None:
                self.rollback(snap.id)
                done.append(snap.id)
        return done

    @staticmethod
    def _remove(path: Path) -> None:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m agentlab.gateway.snapshots", description="List snapshots and roll back.")
    sub = parser.add_subparsers(dest="command", required=True)
    p_list = sub.add_parser("list")
    p_list.add_argument("--session")
    p_list.add_argument("--limit", type=int, default=20)
    p_rb = sub.add_parser("rollback")
    p_rb.add_argument("snapshot_id", nargs="?")
    p_rb.add_argument("--session", help="roll back every snapshot of a session, newest first")
    a = parser.parse_args()

    store = SnapshotStore()
    try:
        if a.command == "list":
            for s in store.recent(a.session, a.limit):
                state = "rolled back" if s.rolled_back_at else "active"
                when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s.ts))
                print(f"{s.id}  {when}  session={s.session_id}  {s.tool}  [{state}]")
                for p in s.paths:
                    print(f"    {p}")
        elif a.session:
            print("rolled back:", ", ".join(store.rollback_session(a.session)) or "nothing")
        elif a.snapshot_id:
            print("restored:", ", ".join(store.rollback(a.snapshot_id)))
        else:
            parser.error("rollback needs a snapshot_id or --session")
    except SnapshotError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
