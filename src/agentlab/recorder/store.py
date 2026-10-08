"""Recorder store: agent runs and their trace events in `<home>/recorder.db`.

Events arrive two ways:
- `ingest_folder()` reads new lines from `<home>/traces/*.jsonl` (remembers each file's offset),
- `add_events()` takes events sent over HTTP.
Both are idempotent: an event is keyed by (run_id, step).

Each run gets a summary row (status, counts, tokens, duration) rebuilt from its events.
"""

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from agentlab.shared.home import agentlab_home
from agentlab.shared.trace import TraceEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    run_id TEXT NOT NULL, step INTEGER NOT NULL, parent_step INTEGER, ts TEXT NOT NULL, type TEXT NOT NULL,
    payload_json TEXT NOT NULL, latency_ms INTEGER, tokens_in INTEGER, tokens_out INTEGER,
    PRIMARY KEY (run_id, step)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, thread_id TEXT, started_at TEXT, ended_at TEXT, model TEXT, message TEXT,
    answer TEXT, status TEXT, steps INTEGER, llm_calls INTEGER, tool_calls INTEGER, tool_errors INTEGER,
    errors INTEGER, tokens_in INTEGER, tokens_out INTEGER, duration_ms INTEGER, meta_json TEXT
);
CREATE TABLE IF NOT EXISTS ingest_offsets (file TEXT PRIMARY KEY, offset INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS goldens (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, label TEXT, created TEXT NOT NULL,
    message TEXT, expected_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_started ON runs (started_at);
"""
# run_start keys copied into the run summary as tags, so runs can be filtered by where they came from
RUN_TAGS = ("arena", "replay_of", "forked_from")
KINDS = ("chat", "arena", "replay")


class RecorderStore:
    def __init__(self, home: Path | None = None):
        home = home or agentlab_home()
        home.mkdir(parents=True, exist_ok=True)
        self.db_path = home / "recorder.db"
        self.traces = home / "traces"
        with self._db() as db:
            db.executescript(SCHEMA)
            if "meta_json" not in {r["name"] for r in db.execute("PRAGMA table_info(runs)")}:
                db.execute("ALTER TABLE runs ADD COLUMN meta_json TEXT")  # recorder.db from before run tags
                for (run_id,) in db.execute("SELECT run_id FROM runs").fetchall():
                    self._summarize(db, run_id)  # fill in tags for runs recorded before the column existed

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    # Ingest

    def add_events(self, events: Iterable[TraceEvent]) -> int:
        events = list(events)
        if not events:
            return 0
        with self._db() as db:
            for e in events:
                tokens = e.tokens
                db.execute(
                    "INSERT OR REPLACE INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (e.run_id, e.step, e.parent_step, e.ts, e.type, json.dumps(e.payload, default=str),
                     e.latency_ms, tokens.input if tokens else None, tokens.output if tokens else None),
                )
            for run_id in {e.run_id for e in events}:
                self._summarize(db, run_id)
        return len(events)

    def ingest_folder(self) -> int:
        """Read lines added to trace files since the last call. A partial last line waits for next time."""
        if not self.traces.is_dir():
            return 0
        total = 0
        with self._db() as db:
            offsets = {r["file"]: r["offset"] for r in db.execute("SELECT * FROM ingest_offsets")}
        for path in sorted(self.traces.glob("*.jsonl")):
            start = offsets.get(path.name, 0)
            if path.stat().st_size <= start:
                continue
            with path.open("rb") as f:
                f.seek(start)
                data = f.read()
            complete = data[: data.rfind(b"\n") + 1]
            events = [TraceEvent.model_validate_json(line) for line in complete.decode("utf-8").splitlines() if line.strip()]
            total += self.add_events(events)
            with self._db() as db:
                db.execute("INSERT OR REPLACE INTO ingest_offsets VALUES (?, ?)", (path.name, start + len(complete)))
        return total

    def _summarize(self, db: sqlite3.Connection, run_id: str) -> None:
        rows = db.execute("SELECT * FROM events WHERE run_id = ? ORDER BY step", (run_id,)).fetchall()
        first, last = rows[0], rows[-1]
        start = next((json.loads(r["payload_json"]) for r in rows if r["type"] == "run_start"), {})
        end = next((json.loads(r["payload_json"]) for r in rows if r["type"] == "run_end"), None)
        errors = sum(r["type"] == "error" for r in rows)
        tool_errors = sum(r["type"] == "tool_result" and json.loads(r["payload_json"]).get("is_error", False) for r in rows)
        status = "running" if end is None else ("error" if errors else "ok")
        duration = round((datetime.fromisoformat(last["ts"]) - datetime.fromisoformat(first["ts"])).total_seconds() * 1000)
        db.execute(
            "INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, start.get("thread_id"), first["ts"], last["ts"] if end is not None else None, start.get("model"),
             start.get("message"), (end or {}).get("answer"), status, len(rows),
             sum(r["type"] == "llm_call" for r in rows), sum(r["type"] == "tool_call" for r in rows), tool_errors,
             errors, sum(r["tokens_in"] or 0 for r in rows), sum(r["tokens_out"] or 0 for r in rows), duration,
             json.dumps({k: start[k] for k in RUN_TAGS if k in start}, default=str)),
        )

    # Read

    def runs(self, limit: int = 100, status: str | None = None, search: str | None = None,
             kind: str | None = None) -> list[dict[str, Any]]:
        """Newest first. `kind`: "arena" (Arena matrix cells), "replay" (replays and forks), "chat" (neither)."""
        where, params = [], []
        if kind == "arena":
            where.append("json_extract(meta_json, '$.arena') IS NOT NULL")
        elif kind == "replay":
            where.append("(json_extract(meta_json, '$.replay_of') IS NOT NULL"
                         " OR json_extract(meta_json, '$.forked_from') IS NOT NULL)")
        elif kind == "chat":
            where.append("(meta_json IS NULL OR meta_json = '{}')")
        if status:
            where.append("status = ?")
            params.append(status)
        if search:
            where.append("(message LIKE ? OR answer LIKE ?)")
            params += [f"%{search}%"] * 2
        sql = "SELECT * FROM runs" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY started_at DESC LIMIT ?"
        with self._db() as db:
            return [_with_tags(dict(r)) for r in db.execute(sql, (*params, limit))]

    def run(self, run_id: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            events = [dict(r) for r in db.execute("SELECT * FROM events WHERE run_id = ? ORDER BY step", (run_id,))]
        for e in events:
            e["payload"] = json.loads(e.pop("payload_json"))
        return {**_with_tags(dict(row)), "events": events}


    # Golden runs (regression baselines)

    def save_golden(self, run_id: str, label: str, expected: dict[str, Any]) -> str:
        import uuid

        run = self.run(run_id)
        if run is None:
            raise ValueError(f"no run {run_id}")
        golden_id = uuid.uuid4().hex[:8]
        with self._db() as db:
            db.execute("INSERT INTO goldens VALUES (?, ?, ?, ?, ?, ?)",
                       (golden_id, run_id, label, datetime.now().astimezone().isoformat(),
                        run.get("message"), json.dumps(expected, default=str)))
        return golden_id

    def goldens(self) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM goldens ORDER BY created DESC")]
        for r in rows:
            r["expected"] = json.loads(r.pop("expected_json"))
        return rows

    def get_golden(self, golden_id: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM goldens WHERE id = ?", (golden_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["expected"] = json.loads(out.pop("expected_json"))
        return out

    def delete_golden(self, golden_id: str) -> bool:
        with self._db() as db:
            return db.execute("DELETE FROM goldens WHERE id = ?", (golden_id,)).rowcount == 1


def _with_tags(row: dict[str, Any]) -> dict[str, Any]:
    row["tags"] = json.loads(row.pop("meta_json", None) or "{}")
    return row


def attach_audit(run: dict[str, Any], audit_rows: list[dict[str, Any]]) -> None:
    """Pair each tool_call event with the Gateway audit row for it (same run, same tool, in order)."""
    queue: dict[str, list[dict[str, Any]]] = {}
    for row in sorted(audit_rows, key=lambda r: r["id"]):
        queue.setdefault(row["tool"], []).append(row)
    for e in run["events"]:
        if e["type"] == "tool_call":
            rows = queue.get(e["payload"].get("tool"), [])
            e["gateway"] = rows.pop(0) if rows else None
