from __future__ import annotations

import json
import fcntl
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .events import EventStore


SCHEMA = """
PRAGMA journal_mode=DELETE;
PRAGMA foreign_keys=ON;

CREATE TABLE events (
    event_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    occurred_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    project_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    event_hash TEXT NOT NULL,
    UNIQUE(device_id, sequence)
);

CREATE TABLE memories (
    memory_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    holder TEXT NOT NULL,
    subject TEXT NOT NULL,
    statement TEXT NOT NULL,
    status TEXT NOT NULL,
    as_of TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_ref TEXT,
    source_anchor TEXT,
    commit_sha TEXT,
    created_at TEXT NOT NULL,
    created_by_device TEXT NOT NULL,
    confirmed_at TEXT,
    superseded_by TEXT,
    archived_at TEXT,
    redactions_json TEXT NOT NULL DEFAULT '[]',
    CHECK(status IN ('proposed', 'active', 'superseded', 'archived'))
);

CREATE INDEX memories_project_status_idx
ON memories(project_id, status, created_at DESC);

CREATE TABLE impressions (
    impression_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    query_text TEXT,
    shown_at TEXT NOT NULL
);

CREATE TABLE feedback (
    feedback_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    signal TEXT NOT NULL,
    noted_at TEXT NOT NULL,
    CHECK(signal IN ('helpful', 'ignored', 'rejected'))
);

CREATE TABLE projection_issues (
    event_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""


class Projection:
    def __init__(self, event_store: EventStore) -> None:
        self.event_store = event_store
        self.settings = event_store.settings

    def rebuild(self) -> Dict[str, Any]:
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.settings.state_dir / "projection.lock"
        with lock_path.open("a+") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                return self._rebuild_locked()
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def _rebuild_locked(self) -> Dict[str, Any]:
        events = self.event_store.all_events()
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix="qmemory.", suffix=".sqlite3", dir=str(self.settings.state_dir)
        )
        os.close(file_descriptor)
        temporary_path = Path(temporary_name)
        issues = 0
        try:
            connection = sqlite3.connect(str(temporary_path))
            connection.row_factory = sqlite3.Row
            connection.executescript(SCHEMA)
            for event in events:
                connection.execute(
                    """
                    INSERT INTO events
                    (event_id, device_id, sequence, occurred_at, kind, project_id, payload_json, event_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event["event_id"],
                        event["device_id"],
                        event["sequence"],
                        event["occurred_at"],
                        event["kind"],
                        event["project_id"],
                        json.dumps(event["payload"], ensure_ascii=False, sort_keys=True),
                        event["event_hash"],
                    ),
                )

            # Pass 1 creates immutable memory records. Supersede also carries the
            # replacement record so clock skew cannot make it disappear.
            for event in events:
                if event["kind"] == "memory.created":
                    self._insert_memory(connection, event, event["payload"]["memory"])
                elif event["kind"] == "memory.superseded":
                    self._insert_memory(connection, event, event["payload"]["replacement"])

            # Pass 2 applies lifecycle and ranking signals deterministically.
            for event in events:
                try:
                    self._apply_state_event(connection, event)
                except (KeyError, ValueError, sqlite3.IntegrityError) as exc:
                    issues += 1
                    connection.execute(
                        "INSERT OR REPLACE INTO projection_issues(event_id, kind, detail) VALUES (?, ?, ?)",
                        (event["event_id"], event["kind"], str(exc)),
                    )

            fts_enabled = self._build_fts(connection)
            connection.commit()
            connection.close()
            os.replace(str(temporary_path), str(self.settings.database_path))
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
        return {
            "events": len(events),
            "issues": issues,
            "database": str(self.settings.database_path),
            "fts_enabled": fts_enabled,
        }

    @staticmethod
    def _insert_memory(
        connection: sqlite3.Connection,
        event: Dict[str, Any],
        memory: Dict[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO memories (
                memory_id, project_id, memory_type, holder, subject, statement,
                status, as_of, source_kind, source_ref, source_anchor, commit_sha,
                created_at, created_by_device, confirmed_at, redactions_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory["memory_id"],
                event["project_id"],
                memory["memory_type"],
                memory["holder"],
                memory["subject"],
                memory["statement"],
                memory["status"],
                memory["as_of"],
                memory["source_kind"],
                memory.get("source_ref"),
                memory.get("source_anchor"),
                memory.get("commit_sha"),
                event["occurred_at"],
                event["device_id"],
                event["occurred_at"] if memory["status"] == "active" else None,
                json.dumps(memory.get("redactions", []), ensure_ascii=False),
            ),
        )

    @staticmethod
    def _require_memory(
        connection: sqlite3.Connection, memory_id: str, project_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM memories WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Referenced memory does not exist: %s" % memory_id)
        if row["project_id"] != project_id:
            raise ValueError("Cross-project lifecycle event rejected")
        return row

    def _apply_state_event(
        self, connection: sqlite3.Connection, event: Dict[str, Any]
    ) -> None:
        kind = event["kind"]
        payload = event["payload"]
        if kind in ("memory.created",):
            return
        if kind == "memory.confirmed":
            row = self._require_memory(connection, payload["memory_id"], event["project_id"])
            if row["status"] == "proposed":
                connection.execute(
                    "UPDATE memories SET status = 'active', confirmed_at = ? WHERE memory_id = ?",
                    (event["occurred_at"], payload["memory_id"]),
                )
            elif row["status"] != "active":
                raise ValueError("Only proposed memories can be confirmed")
        elif kind == "memory.superseded":
            old_id = payload["memory_id"]
            replacement_id = payload["replacement"]["memory_id"]
            old = self._require_memory(connection, old_id, event["project_id"])
            self._require_memory(connection, replacement_id, event["project_id"])
            if old["status"] == "superseded":
                # Concurrent devices may supersede the same fact while offline.
                # Global replay order selects one winner; the other replacement
                # remains visible as archived history and the issue is surfaced.
                connection.execute(
                    "UPDATE memories SET status = 'archived', archived_at = ? WHERE memory_id = ?",
                    (event["occurred_at"], replacement_id),
                )
                raise ValueError(
                    "Concurrent supersede lost to replacement %s" % old["superseded_by"]
                )
            if old["status"] not in ("active", "proposed"):
                connection.execute(
                    "UPDATE memories SET status = 'archived', archived_at = ? WHERE memory_id = ?",
                    (event["occurred_at"], replacement_id),
                )
                raise ValueError("Only current memories can be superseded")
            connection.execute(
                "UPDATE memories SET status = 'superseded', superseded_by = ? WHERE memory_id = ?",
                (replacement_id, old_id),
            )
        elif kind == "memory.archived":
            row = self._require_memory(connection, payload["memory_id"], event["project_id"])
            if row["status"] == "superseded":
                raise ValueError("Superseded history cannot be archived")
            connection.execute(
                "UPDATE memories SET status = 'archived', archived_at = ? WHERE memory_id = ?",
                (event["occurred_at"], payload["memory_id"]),
            )
        elif kind == "memory.impression":
            self._require_memory(connection, payload["memory_id"], event["project_id"])
            connection.execute(
                """
                INSERT INTO impressions(event_id, memory_id, project_id, query_text, shown_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event["event_id"],
                    payload["memory_id"],
                    event["project_id"],
                    payload.get("query"),
                    event["occurred_at"],
                ),
            )
        elif kind == "memory.feedback":
            self._require_memory(connection, payload["memory_id"], event["project_id"])
            connection.execute(
                """
                INSERT INTO feedback(event_id, memory_id, project_id, signal, noted_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event["event_id"],
                    payload["memory_id"],
                    event["project_id"],
                    payload["signal"],
                    event["occurred_at"],
                ),
            )
        elif kind not in ("memory.created",):
            raise ValueError("Unknown event kind: %s" % kind)

    @staticmethod
    def _build_fts(connection: sqlite3.Connection) -> bool:
        try:
            connection.execute(
                "CREATE VIRTUAL TABLE memory_fts USING fts5(memory_id UNINDEXED, statement, subject, memory_type)"
            )
            connection.execute(
                """
                INSERT INTO memory_fts(memory_id, statement, subject, memory_type)
                SELECT memory_id, statement, subject, memory_type FROM memories
                """
            )
            return True
        except sqlite3.OperationalError:
            return False

    def connect(self) -> sqlite3.Connection:
        if not self.settings.database_path.exists():
            self.rebuild()
        connection = sqlite3.connect(str(self.settings.database_path))
        connection.row_factory = sqlite3.Row
        return connection

    def get(self, memory_id: str) -> Optional[Dict[str, Any]]:
        connection = self.connect()
        try:
            row = connection.execute(
                "SELECT * FROM memories WHERE memory_id = ?", (memory_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    def search(
        self,
        project_id: str,
        query: str = "",
        statuses: Sequence[str] = ("active",),
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        placeholders = ",".join("?" for _ in statuses)
        base_parameters: List[Any] = [project_id] + list(statuses)
        connection = self.connect()
        try:
            rows: Iterable[sqlite3.Row]
            if query.strip() and self._fts_exists(connection):
                tokens = [token for token in query.replace('"', " ").split() if token]
                expression = " AND ".join('"%s"' % token for token in tokens)
                try:
                    rows = connection.execute(
                        """
                        SELECT m.*,
                               bm25(memory_fts) AS text_rank,
                               COALESCE(SUM(CASE f.signal WHEN 'helpful' THEN 2 WHEN 'rejected' THEN -3 ELSE 0 END), 0) AS feedback_rank
                        FROM memory_fts
                        JOIN memories m ON m.memory_id = memory_fts.memory_id
                        LEFT JOIN feedback f ON f.memory_id = m.memory_id
                        WHERE memory_fts MATCH ?
                          AND m.project_id = ?
                          AND m.status IN (%s)
                        GROUP BY m.memory_id
                        ORDER BY text_rank ASC, feedback_rank DESC, m.created_at DESC
                        LIMIT ?
                        """ % placeholders,
                        [expression] + base_parameters + [limit],
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = self._like_search(
                        connection, project_id, query, statuses, limit
                    )
            elif query.strip():
                rows = self._like_search(connection, project_id, query, statuses, limit)
            else:
                rows = connection.execute(
                    """
                    SELECT m.*,
                           COALESCE(SUM(CASE f.signal WHEN 'helpful' THEN 2 WHEN 'rejected' THEN -3 ELSE 0 END), 0) AS feedback_rank
                    FROM memories m
                    LEFT JOIN feedback f ON f.memory_id = m.memory_id
                    WHERE m.project_id = ? AND m.status IN (%s)
                    GROUP BY m.memory_id
                    ORDER BY feedback_rank DESC, m.created_at DESC
                    LIMIT ?
                    """ % placeholders,
                    base_parameters + [limit],
                ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    @staticmethod
    def _fts_exists(connection: sqlite3.Connection) -> bool:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'memory_fts'"
        ).fetchone() is not None

    @staticmethod
    def _like_search(
        connection: sqlite3.Connection,
        project_id: str,
        query: str,
        statuses: Sequence[str],
        limit: int,
    ) -> List[sqlite3.Row]:
        placeholders = ",".join("?" for _ in statuses)
        terms = [term.casefold() for term in query.split() if term]
        where_terms = " AND ".join(
            "lower(m.statement || ' ' || m.subject || ' ' || m.memory_type) LIKE ?"
            for _ in terms
        )
        parameters: List[Any] = [project_id] + list(statuses)
        parameters.extend("%%%s%%" % term for term in terms)
        parameters.append(limit)
        return connection.execute(
            """
            SELECT m.*,
                   COALESCE(SUM(CASE f.signal WHEN 'helpful' THEN 2 WHEN 'rejected' THEN -3 ELSE 0 END), 0) AS feedback_rank
            FROM memories m
            LEFT JOIN feedback f ON f.memory_id = m.memory_id
            WHERE m.project_id = ? AND m.status IN (%s) AND %s
            GROUP BY m.memory_id
            ORDER BY feedback_rank DESC, m.created_at DESC
            LIMIT ?
            """ % (placeholders, where_terms or "1=1"),
            parameters,
        ).fetchall()

    def status(self) -> Dict[str, Any]:
        connection = self.connect()
        try:
            counts = {
                row["status"]: row["count"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM memories GROUP BY status"
                )
            }
            events = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            issues = connection.execute(
                "SELECT COUNT(*) FROM projection_issues"
            ).fetchone()[0]
            return {"events": events, "memories": counts, "projection_issues": issues}
        finally:
            connection.close()

    def retrieval_stats(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        """Return aggregate recall exposure and feedback without leaking query text.

        Impressions and feedback are append-only events.  The quality dashboard only needs
        aggregate counts; keeping raw queries out of this response avoids turning diagnostics
        into another source of potentially sensitive conversation content.
        """
        connection = self.connect()
        try:
            where = "WHERE project_id = ?" if project_id else ""
            parameters: Sequence[Any] = (project_id,) if project_id else ()
            impressions = int(
                connection.execute(
                    "SELECT COUNT(*) FROM impressions %s" % where, parameters
                ).fetchone()[0]
            )
            rows = connection.execute(
                "SELECT signal, COUNT(*) AS count FROM feedback %s GROUP BY signal" % where,
                parameters,
            ).fetchall()
            feedback = {"helpful": 0, "ignored": 0, "rejected": 0}
            feedback.update({str(row["signal"]): int(row["count"]) for row in rows})
            rated = sum(feedback.values())
            return {
                "impressions": impressions,
                "feedback": feedback,
                "rated": rated,
                "helpful_rate": round(feedback["helpful"] / rated, 4) if rated else None,
                "feedback_coverage": round(rated / impressions, 4) if impressions else None,
            }
        finally:
            connection.close()

    def issues(self) -> List[Dict[str, Any]]:
        connection = self.connect()
        try:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT event_id, kind, detail FROM projection_issues ORDER BY event_id"
                ).fetchall()
            ]
        finally:
            connection.close()
