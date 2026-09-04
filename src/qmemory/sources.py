from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import Settings
from .project import ProjectIdentity, identify_project
from .redaction import REDACTION_VERSION, redact_text
from .sync_observability import SyncRunTracker


SOURCE_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL,
    project_id TEXT NOT NULL,
    project_root TEXT NOT NULL,
    title TEXT NOT NULL,
    started_at TEXT,
    updated_at TEXT,
    source_path TEXT NOT NULL,
    mirror_path TEXT NOT NULL,
    mirror_size INTEGER NOT NULL,
    mirror_sha256 TEXT NOT NULL,
    source_mtime_ns INTEGER NOT NULL,
    message_count INTEGER NOT NULL,
    user_message_count INTEGER NOT NULL,
    agent_message_count INTEGER NOT NULL,
    imported_at TEXT NOT NULL,
    insights_extracted_sha256 TEXT,
    duplicate_of TEXT,
    parent_conversation_id TEXT,
    agent_path TEXT,
    agent_nickname TEXT,
    identity_source TEXT NOT NULL DEFAULT 'cwd',
    relationship_scanned INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS conversations_project_updated_idx
ON conversations(project_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS conversation_messages (
    message_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    occurred_at TEXT,
    source_line INTEGER NOT NULL,
    source_offset INTEGER NOT NULL,
    content_sha256 TEXT NOT NULL,
    FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE,
    UNIQUE(conversation_id, sequence)
);

CREATE INDEX IF NOT EXISTS messages_conversation_sequence_idx
ON conversation_messages(conversation_id, sequence);

CREATE TABLE IF NOT EXISTS insight_chunks (
    conversation_id TEXT NOT NULL,
    chunk_sha256 TEXT NOT NULL,
    sequence_start INTEGER NOT NULL,
    sequence_end INTEGER NOT NULL,
    status TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    error TEXT,
    PRIMARY KEY(conversation_id, chunk_sha256),
    FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE,
    CHECK(status IN ('complete', 'failed'))
);

CREATE INDEX IF NOT EXISTS insight_chunks_status_idx
ON insight_chunks(conversation_id, status);

CREATE TABLE IF NOT EXISTS insight_runs (
    run_id TEXT PRIMARY KEY,
    reset_at TEXT NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    display_name TEXT,
    remote TEXT,
    canonical_root TEXT,
    canonical_root_locked INTEGER NOT NULL DEFAULT 0,
    sync_enabled INTEGER NOT NULL DEFAULT 1,
    ignored INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_roots (
    project_id TEXT NOT NULL,
    project_root TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY(project_id, project_root),
    FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS project_roots_project_idx
ON project_roots(project_id, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS conversation_project_bindings (
    conversation_id TEXT PRIMARY KEY,
    project_path TEXT NOT NULL,
    project_id TEXT NOT NULL,
    bound_at TEXT NOT NULL,
    FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS conversation_project_bindings_project_idx
ON conversation_project_bindings(project_id, bound_at DESC);

CREATE TABLE IF NOT EXISTS source_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path, limit: Optional[int] = None) -> str:
    digest = hashlib.sha256()
    remaining = limit
    with path.open("rb") as handle:
        while True:
            size = 1024 * 1024 if remaining is None else min(1024 * 1024, remaining)
            if size <= 0:
                break
            chunk = handle.read(size)
            if not chunk:
                break
            digest.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
    return digest.hexdigest()


def _prefix_matches(source: Path, mirror: Path, size: int) -> bool:
    if size == 0:
        return True
    return _sha256(source, size) == _sha256(mirror, size)


class CodexConversationArchive:
    """Non-lossy local archive of coding-agent transcripts plus a readable projection."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = settings.sources_dir
        self.raw_dirs = {
            "codex": self.root / "codex" / "raw",
            "claude-code": self.root / "claude-code" / "raw",
        }
        self.versions_dirs = {
            "codex": self.root / "codex" / "versions",
            "claude-code": self.root / "claude-code" / "versions",
        }
        for directory in (
            self.root,
            *self.raw_dirs.values(),
            *self.versions_dirs.values(),
        ):
            directory.mkdir(parents=True, exist_ok=True)
            try:
                directory.chmod(0o700)
            except OSError:
                pass
        self.connection = sqlite3.connect(str(settings.source_database_path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(SOURCE_SCHEMA)
        self._migrate_schema()
        self._backfill_conversation_relationships()
        self._backfill_projects()
        self._ensure_fts()
        self._ensure_redacted_projection()

    def close(self) -> None:
        self.connection.close()

    def _ensure_fts(self) -> None:
        try:
            self.connection.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS conversation_fts "
                "USING fts5(message_id UNINDEXED, conversation_id UNINDEXED, text)"
            )
            self.connection.commit()
        except sqlite3.OperationalError:
            pass

    def _ensure_redacted_projection(self) -> None:
        """Upgrade display/search projections without rewriting lossless evidence."""
        row = self.connection.execute(
            "SELECT value FROM source_metadata WHERE key = 'redaction_version'"
        ).fetchone()
        current = int(row["value"]) if row and str(row["value"]).isdigit() else 0
        if current >= REDACTION_VERSION:
            return
        conversations = self.connection.execute(
            "SELECT conversation_id, title FROM conversations"
        ).fetchall()
        with self.connection:
            for item in conversations:
                safe_title = redact_text(str(item["title"]))[0]
                if safe_title != item["title"]:
                    self.connection.execute(
                        "UPDATE conversations SET title = ? WHERE conversation_id = ?",
                        (safe_title, item["conversation_id"]),
                    )
            try:
                self.connection.execute("DELETE FROM conversation_fts")
                self.connection.executemany(
                    "INSERT INTO conversation_fts(message_id, conversation_id, text) "
                    "VALUES (?, ?, ?)",
                    (
                        (
                            item["message_id"],
                            item["conversation_id"],
                            redact_text(str(item["text"]))[0],
                        )
                        for item in self.connection.execute(
                            "SELECT message_id, conversation_id, text "
                            "FROM conversation_messages"
                        )
                    ),
                )
            except sqlite3.OperationalError:
                pass
            self.connection.execute(
                "INSERT INTO source_metadata(key, value) VALUES ('redaction_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(REDACTION_VERSION),),
            )

    def _migrate_schema(self) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(conversations)")
        }
        if "insights_extracted_sha256" not in columns:
            self.connection.execute(
                "ALTER TABLE conversations ADD COLUMN insights_extracted_sha256 TEXT"
            )
            self.connection.commit()
        if "duplicate_of" not in columns:
            self.connection.execute("ALTER TABLE conversations ADD COLUMN duplicate_of TEXT")
        if "parent_conversation_id" not in columns:
            self.connection.execute(
                "ALTER TABLE conversations ADD COLUMN parent_conversation_id TEXT"
            )
        if "agent_path" not in columns:
            self.connection.execute("ALTER TABLE conversations ADD COLUMN agent_path TEXT")
        if "agent_nickname" not in columns:
            self.connection.execute("ALTER TABLE conversations ADD COLUMN agent_nickname TEXT")
        if "identity_source" not in columns:
            self.connection.execute(
                "ALTER TABLE conversations ADD COLUMN identity_source TEXT NOT NULL DEFAULT 'cwd'"
            )
        if "relationship_scanned" not in columns:
            self.connection.execute(
                "ALTER TABLE conversations ADD COLUMN relationship_scanned INTEGER NOT NULL DEFAULT 0"
            )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS conversations_duplicate_idx "
            "ON conversations(project_id, duplicate_of)"
        )
        project_columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(projects)")
        }
        if "canonical_root_locked" not in project_columns:
            self.connection.execute(
                "ALTER TABLE projects ADD COLUMN canonical_root_locked INTEGER NOT NULL DEFAULT 0"
            )
        self.connection.commit()

    @staticmethod
    def _relationship(metadata: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        source = metadata.get("source")
        if not isinstance(source, dict):
            return None, None, None
        subagent = source.get("subagent")
        if not isinstance(subagent, dict):
            return None, None, None
        spawn = subagent.get("thread_spawn")
        if not isinstance(spawn, dict):
            return None, None, None
        return (
            str(spawn.get("parent_thread_id") or "") or None,
            str(spawn.get("agent_path") or "") or None,
            str(spawn.get("agent_nickname") or "") or None,
        )

    @staticmethod
    def _read_codex_metadata(path: Path) -> Dict[str, Any]:
        try:
            with path.open("rb") as handle:
                for raw_line in handle:
                    event = json.loads(raw_line.decode("utf-8"))
                    if event.get("type") == "session_meta":
                        payload = event.get("payload")
                        return payload if isinstance(payload, dict) else {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return {}

    def _backfill_conversation_relationships(self) -> None:
        rows = self.connection.execute(
            """
            SELECT conversation_id, mirror_path FROM conversations
            WHERE source_kind = 'codex' AND relationship_scanned = 0
            """
        ).fetchall()
        if not rows:
            return
        with self.connection:
            for row in rows:
                metadata = self._read_codex_metadata(Path(str(row["mirror_path"])))
                parent_id, agent_path, agent_nickname = self._relationship(metadata)
                self.connection.execute(
                    """
                    UPDATE conversations SET parent_conversation_id = ?, agent_path = ?,
                        agent_nickname = ?, relationship_scanned = 1
                    WHERE conversation_id = ?
                    """,
                    (parent_id, agent_path, agent_nickname, row["conversation_id"]),
                )

    def _backfill_projects(self) -> None:
        rows = self.connection.execute(
            """
            SELECT project_id, project_root, MIN(imported_at) AS first_seen,
                   MAX(imported_at) AS last_seen
            FROM conversations
            GROUP BY project_id, project_root
            """
        ).fetchall()
        if not rows:
            return
        with self.connection:
            for row in rows:
                project_id = str(row["project_id"])
                root = str(row["project_root"])
                first_seen = str(row["first_seen"] or _now())
                last_seen = str(row["last_seen"] or first_seen)
                remote = project_id.removeprefix("git:") if project_id.startswith("git:") else None
                self._register_project(
                    project_id,
                    root,
                    remote,
                    first_seen=first_seen,
                    last_seen=last_seen,
                )

    @staticmethod
    def _default_project_name(project_id: str, root: str) -> str:
        if project_id.startswith("git:"):
            value = project_id.removeprefix("git:").rstrip("/").split("/")[-1]
            return value.removesuffix(".git") or Path(root).name or "Git 项目"
        return Path(root).name or root

    @staticmethod
    def _preferred_root(roots: Iterable[str]) -> str:
        values = sorted(set(str(root) for root in roots if root))
        if not values:
            return ""
        existing = [root for root in values if Path(root).exists()]
        candidates = existing or values
        normal = [root for root in candidates if "/.claude/worktrees/" not in root]
        return min(normal or candidates, key=lambda value: (len(Path(value).parts), len(value)))

    def _register_project(
        self,
        project_id: str,
        root: str,
        remote: Optional[str],
        *,
        canonical_root: Optional[str] = None,
        first_seen: Optional[str] = None,
        last_seen: Optional[str] = None,
    ) -> None:
        first_seen = first_seen or _now()
        last_seen = last_seen or first_seen
        existing_roots = [
            str(row[0])
            for row in self.connection.execute(
                "SELECT project_root FROM project_roots WHERE project_id = ?",
                (project_id,),
            ).fetchall()
        ]
        canonical = canonical_root or self._preferred_root(existing_roots + [root])
        if existing_roots and canonical_root:
            canonical = self._preferred_root(existing_roots + [root, canonical_root])
        self.connection.execute(
            """
            INSERT INTO projects (
                project_id, display_name, remote, canonical_root,
                canonical_root_locked, sync_enabled, ignored, first_seen_at, last_seen_at
            ) VALUES (?, NULL, ?, ?, 0, 1, 0, ?, ?)
            ON CONFLICT(project_id) DO UPDATE SET
                remote=COALESCE(excluded.remote, projects.remote),
                canonical_root=CASE
                    WHEN projects.canonical_root_locked = 1 THEN projects.canonical_root
                    ELSE excluded.canonical_root END,
                last_seen_at=CASE WHEN excluded.last_seen_at > projects.last_seen_at
                                  THEN excluded.last_seen_at ELSE projects.last_seen_at END
            """,
            (project_id, remote, canonical, first_seen, last_seen),
        )
        for project_root in dict.fromkeys([root, canonical]):
            if not project_root:
                continue
            self.connection.execute(
                """
                INSERT INTO project_roots (project_id, project_root, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(project_id, project_root) DO UPDATE SET
                    last_seen_at=CASE WHEN excluded.last_seen_at > project_roots.last_seen_at
                                      THEN excluded.last_seen_at ELSE project_roots.last_seen_at END
                """,
                (project_id, project_root, first_seen, last_seen),
            )

    def register_project(self, project_path: Path) -> Dict[str, Any]:
        identity = identify_project(project_path)
        with self.connection:
            self._register_project(
                identity.project_id,
                identity.root,
                identity.remote,
                canonical_root=identity.canonical_root,
                last_seen=_now(),
            )
        return self.project_detail(identity.project_id)

    @staticmethod
    def _metadata_project_path(metadata: Dict[str, Any]) -> Optional[Path]:
        for key in ("project_path", "projectRoot", "workspace_root", "workspaceRoot"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return Path(value).expanduser()
        return None

    def _conversation_identity(
        self,
        conversation_id: str,
        metadata: Dict[str, Any],
        source: Path,
    ) -> ProjectIdentity:
        binding = self.connection.execute(
            "SELECT project_path FROM conversation_project_bindings WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if binding is not None:
            project_path = Path(str(binding["project_path"]))
            return identify_project(project_path, explicit_root=project_path)

        explicit = self._metadata_project_path(metadata)
        if explicit is not None:
            return identify_project(explicit, explicit_root=explicit)

        cwd_value = metadata.get("cwd")
        if isinstance(cwd_value, str) and cwd_value.strip():
            return identify_project(Path(cwd_value).expanduser())

        fallback = source.parent.expanduser().resolve()
        return identify_project(None, manual_fallback=fallback)

    def bind_conversation_project(
        self, conversation_id: str, project_path: Path
    ) -> Dict[str, Any]:
        """Bind one archived conversation to a project in the derived projection.

        The raw and mirrored transcript remain byte-for-byte unchanged.  Re-sync and
        ``recompute_project_mappings`` both reapply the binding deterministically.
        """

        row = self.connection.execute(
            "SELECT conversation_id FROM conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Unknown conversation: %s" % conversation_id)
        target = project_path.expanduser().resolve()
        identity = identify_project(target, explicit_root=target)
        now = _now()
        with self.connection:
            self._register_project(
                identity.project_id,
                identity.root,
                identity.remote,
                canonical_root=identity.canonical_root,
                last_seen=now,
            )
            self.connection.execute(
                """
                INSERT INTO conversation_project_bindings (
                    conversation_id, project_path, project_id, bound_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    project_path=excluded.project_path,
                    project_id=excluded.project_id,
                    bound_at=excluded.bound_at
                """,
                (conversation_id, str(target), identity.project_id, now),
            )
            self.connection.execute(
                """
                UPDATE conversations
                SET project_id = ?, project_root = ?, identity_source = 'explicit',
                    insights_extracted_sha256 = NULL
                WHERE conversation_id = ?
                """,
                (identity.project_id, identity.root, conversation_id),
            )
        self._refresh_duplicate_links()
        return {
            "conversation_id": conversation_id,
            "project": self.project_detail(identity.project_id),
            "source_evidence_changed": False,
        }

    def recompute_project_mappings(self) -> Dict[str, int]:
        """Rebuild conversation-to-project mappings from raw metadata and bindings."""

        rows = self.connection.execute(
            "SELECT conversation_id, source_kind, source_path, mirror_path, project_id, "
            "project_root, identity_source FROM conversations ORDER BY conversation_id"
        ).fetchall()
        changed = 0
        with self.connection:
            for row in rows:
                mirror = Path(str(row["mirror_path"]))
                metadata = (
                    self._parse_claude(mirror)[0]
                    if row["source_kind"] == "claude-code"
                    else self._parse(mirror)[0]
                )
                identity = self._conversation_identity(
                    str(row["conversation_id"]), metadata, Path(str(row["source_path"]))
                )
                self._register_project(
                    identity.project_id,
                    identity.root,
                    identity.remote,
                    canonical_root=identity.canonical_root,
                    last_seen=_now(),
                )
                current = (
                    str(row["project_id"]),
                    str(row["project_root"]),
                    str(row["identity_source"]),
                )
                resolved = (identity.project_id, identity.root, identity.identity_source)
                if current == resolved:
                    continue
                changed += 1
                self.connection.execute(
                    """
                    UPDATE conversations
                    SET project_id = ?, project_root = ?, identity_source = ?,
                        insights_extracted_sha256 = NULL
                    WHERE conversation_id = ?
                    """,
                    (*resolved, row["conversation_id"]),
                )
        self._refresh_duplicate_links()
        return {"examined": len(rows), "changed": changed}

    @staticmethod
    def codex_home(override: Optional[Path] = None) -> Path:
        if override is not None:
            return override.expanduser().resolve()
        value = os.environ.get("CODEX_HOME")
        return Path(value).expanduser().resolve() if value else (Path.home() / ".codex")

    @classmethod
    def discover(cls, codex_home: Optional[Path] = None) -> List[Path]:
        home = cls.codex_home(codex_home)
        paths: List[Path] = []
        for directory in (home / "sessions", home / "archived_sessions"):
            if directory.exists():
                paths.extend(directory.rglob("rollout-*.jsonl"))
        return sorted(set(path.resolve() for path in paths))

    @staticmethod
    def claude_home(override: Optional[Path] = None) -> Path:
        if override is not None:
            return override.expanduser().resolve()
        value = os.environ.get("CLAUDE_CONFIG_DIR")
        return Path(value).expanduser().resolve() if value else (Path.home() / ".claude")

    @classmethod
    def discover_claude(cls, claude_home: Optional[Path] = None) -> List[Path]:
        projects = cls.claude_home(claude_home) / "projects"
        if not projects.exists():
            return []
        # Only top-level sessions are user conversations. The nested subagents directory
        # contains internal execution branches and would duplicate the parent transcript.
        return sorted(path.resolve() for path in projects.glob("*/*.jsonl") if path.is_file())

    @classmethod
    def discover_all(
        cls,
        codex_home: Optional[Path] = None,
        claude_home: Optional[Path] = None,
    ) -> Dict[str, List[Path]]:
        return {
            "codex": cls.discover(codex_home),
            "claude-code": cls.discover_claude(claude_home),
        }

    def preview(self, codex_home: Optional[Path] = None) -> Dict[str, Any]:
        return self.preview_all(codex_home=codex_home, include_claude=False)

    def preview_all(
        self,
        codex_home: Optional[Path] = None,
        claude_home: Optional[Path] = None,
        include_claude: bool = True,
    ) -> Dict[str, Any]:
        discovered = self.discover_all(codex_home, claude_home)
        if not include_claude:
            discovered = {"codex": discovered["codex"]}
        archived = {
            (str(row["source_kind"]), str(row["source_path"])): row
            for row in self.connection.execute(
                "SELECT source_kind, source_path, mirror_size, source_mtime_ns FROM conversations"
            )
        }
        changed = 0
        total_bytes = 0
        by_source: Dict[str, Dict[str, int]] = {}
        for source_kind, paths in discovered.items():
            source_changed = 0
            source_bytes = 0
            for path in paths:
                stat = path.stat()
                total_bytes += stat.st_size
                source_bytes += stat.st_size
                existing = archived.get((source_kind, str(path)))
                if (
                    existing is None
                    or existing["mirror_size"] != stat.st_size
                    or existing["source_mtime_ns"] != stat.st_mtime_ns
                ):
                    changed += 1
                    source_changed += 1
            by_source[source_kind] = {
                "found": len(paths),
                "changed": source_changed,
                "total_bytes": source_bytes,
            }
        return {
            "source": "Coding agents",
            "found": sum(len(paths) for paths in discovered.values()),
            "changed": changed,
            "total_bytes": total_bytes,
            "archived": len(archived),
            "by_source": by_source,
            "local_only": True,
        }

    def sync(self, codex_home: Optional[Path] = None) -> Dict[str, Any]:
        return self.sync_all(codex_home=codex_home, include_claude=False)

    def sync_all(
        self,
        codex_home: Optional[Path] = None,
        claude_home: Optional[Path] = None,
        include_claude: bool = True,
    ) -> Dict[str, Any]:
        discovered = self.discover_all(codex_home, claude_home)
        if not include_claude:
            discovered = {"codex": discovered["codex"]}
        tracker = SyncRunTracker()
        for source_kind, paths in discovered.items():
            tracker.record_found(source_kind, len(paths))
            for path in paths:
                try:
                    result = self._sync_one(path, source_kind)
                except Exception as exc:
                    tracker.record_failure(source_kind, exc)
                    continue
                if result["changed"]:
                    tracker.record_imported(
                        source_kind,
                        messages_indexed=int(result["messages"]),
                        bytes_copied=int(result["bytes_copied"]),
                    )
                else:
                    tracker.record_unchanged(source_kind)
        self._refresh_duplicate_links()
        return tracker.snapshot(
            source="Coding agents",
            projects=len(self.projects()),
            local_only=True,
        )

    @staticmethod
    def _is_tool_trace(text: str) -> bool:
        stripped = text.lstrip()
        prefixes = (
            "[external_agent_tool_call",
            "[external_agent_tool_result",
            "<EXTERNAL SESSION IMPORTED>",
        )
        return stripped.startswith(prefixes)

    def _conversation_fingerprint(self, conversation_id: str) -> Dict[str, Any]:
        rows = self.connection.execute(
            """
            SELECT role, text, content_sha256 FROM conversation_messages
            WHERE conversation_id = ? ORDER BY sequence
            """,
            (conversation_id,),
        ).fetchall()
        visible = [row for row in rows if not self._is_tool_trace(str(row["text"]))]
        first_user = next(
            (str(row["content_sha256"]) for row in visible if row["role"] == "user"),
            "",
        )
        return {
            "first_user": first_user,
            "hashes": {str(row["content_sha256"]) for row in visible},
            "visible_count": len(visible),
            "has_import_marker": any(
                str(row["text"]).lstrip().startswith("<EXTERNAL SESSION IMPORTED>")
                for row in rows
            ),
        }

    def _refresh_duplicate_links(self) -> None:
        rows = self.connection.execute(
            """
            SELECT conversation_id, project_id, source_kind, started_at, message_count,
                   parent_conversation_id
            FROM conversations ORDER BY project_id, started_at, conversation_id
            """
        ).fetchall()
        by_project: Dict[str, List[sqlite3.Row]] = {}
        for row in rows:
            # Subagent branches are related through parent_thread_id. They are execution
            # evidence, never transcript copies, even when their prompts overlap.
            if row["parent_conversation_id"]:
                continue
            by_project.setdefault(str(row["project_id"]), []).append(row)
        duplicates: Dict[str, str] = {}
        for project_rows in by_project.values():
            fingerprints = {
                str(row["conversation_id"]): self._conversation_fingerprint(
                    str(row["conversation_id"])
                )
                for row in project_rows
            }
            def quality(row: sqlite3.Row) -> Tuple[Any, ...]:
                fingerprint = fingerprints[str(row["conversation_id"])]
                return (
                    len(fingerprint["hashes"]), fingerprint["visible_count"],
                    not fingerprint["has_import_marker"], str(row["started_at"] or ""),
                )

            def matches(canonical: sqlite3.Row, candidate: sqlite3.Row) -> bool:
                canonical_fp = fingerprints[str(canonical["conversation_id"])]
                candidate_fp = fingerprints[str(candidate["conversation_id"])]
                if (
                    not canonical_fp["first_user"]
                    or canonical_fp["first_user"] != candidate_fp["first_user"]
                ):
                    return False
                shared = len(canonical_fp["hashes"] & candidate_fp["hashes"])
                minimum = min(len(canonical_fp["hashes"]), len(candidate_fp["hashes"]))
                threshold = (
                    0.95 if canonical["source_kind"] == candidate["source_kind"] else 0.55
                )
                return shared >= 3 and bool(minimum) and shared / minimum >= threshold

            # Greedy, canonical-anchored grouping intentionally avoids transitive chains:
            # every copy must directly satisfy the threshold against the richest transcript.
            remaining = sorted(project_rows, key=quality, reverse=True)
            while remaining:
                canonical = remaining.pop(0)
                group = [canonical]
                still_unassigned: List[sqlite3.Row] = []
                for row in remaining:
                    (group if matches(canonical, row) else still_unassigned).append(row)
                remaining = still_unassigned
                canonical_id = str(canonical["conversation_id"])
                for row in group[1:]:
                    conversation_id = str(row["conversation_id"])
                    duplicates[conversation_id] = canonical_id
        with self.connection:
            self.connection.execute("UPDATE conversations SET duplicate_of = NULL")
            self.connection.executemany(
                "UPDATE conversations SET duplicate_of = ? WHERE conversation_id = ?",
                [(canonical, duplicate) for duplicate, canonical in duplicates.items()],
            )

    def _sync_one(self, source: Path, source_kind: str = "codex") -> Dict[str, Any]:
        stat = source.stat()
        existing = self.connection.execute(
            "SELECT * FROM conversations WHERE source_kind = ? AND source_path = ?",
            (source_kind, str(source)),
        ).fetchone()
        if (
            existing is not None
            and existing["mirror_size"] == stat.st_size
            and existing["source_mtime_ns"] == stat.st_mtime_ns
        ):
            return {"changed": False, "messages": 0, "bytes_copied": 0}

        native_id = self._id_from_filename(source)
        preliminary_id = native_id if source_kind == "codex" else "claude:" + native_id
        mirror = (
            Path(str(existing["mirror_path"]))
            if existing is not None
            else self.raw_dirs[source_kind] / (preliminary_id.replace(":", "-") + ".jsonl")
        )
        bytes_copied = self._mirror_source(source, mirror, stat.st_size, source_kind)
        metadata, parsed_messages = (
            self._parse_claude(mirror) if source_kind == "claude-code" else self._parse(mirror)
        )
        native_conversation_id = str(metadata.get("id") or source.stem)
        conversation_id = (
            preliminary_id
            if source_kind == "codex"
            else "claude:" + native_conversation_id.removeprefix("claude:")
        )
        if conversation_id != preliminary_id:
            final_mirror = self.raw_dirs[source_kind] / (
                conversation_id.replace(":", "-") + ".jsonl"
            )
            if final_mirror != mirror:
                mirror.replace(final_mirror)
                mirror = final_mirror
        identity = self._conversation_identity(conversation_id, metadata, source)
        title = redact_text(
            str(metadata.get("title") or self._title(parsed_messages, metadata))
        )[0]
        started_at = str(metadata.get("timestamp") or "") or None
        updated_at = parsed_messages[-1]["occurred_at"] if parsed_messages else started_at
        mirror_hash = _sha256(mirror)
        imported_at = _now()
        parent_id, agent_path, agent_nickname = self._relationship(metadata)
        user_count = sum(message["role"] == "user" for message in parsed_messages)
        agent_count = sum(message["role"] == "assistant" for message in parsed_messages)

        with self.connection:
            self._register_project(
                identity.project_id,
                identity.root,
                identity.remote,
                canonical_root=identity.canonical_root,
                last_seen=imported_at,
            )
            self.connection.execute(
                "DELETE FROM conversation_messages WHERE conversation_id = ?",
                (conversation_id,),
            )
            self.connection.execute(
                "DELETE FROM conversation_fts WHERE conversation_id = ?",
                (conversation_id,),
            )
            self.connection.execute(
                """
                INSERT INTO conversations (
                    conversation_id, source_kind, project_id, project_root, title,
                    started_at, updated_at, source_path, mirror_path, mirror_size,
                    mirror_sha256, source_mtime_ns, message_count, user_message_count,
                    agent_message_count, imported_at, parent_conversation_id,
                    agent_path, agent_nickname, identity_source, relationship_scanned
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    project_id=excluded.project_id, project_root=excluded.project_root,
                    title=excluded.title, started_at=excluded.started_at,
                    updated_at=excluded.updated_at, source_path=excluded.source_path,
                    mirror_path=excluded.mirror_path, mirror_size=excluded.mirror_size,
                    mirror_sha256=excluded.mirror_sha256,
                    source_mtime_ns=excluded.source_mtime_ns,
                    message_count=excluded.message_count,
                    user_message_count=excluded.user_message_count,
                    agent_message_count=excluded.agent_message_count,
                    parent_conversation_id=excluded.parent_conversation_id,
                    agent_path=excluded.agent_path,
                    agent_nickname=excluded.agent_nickname,
                    identity_source=excluded.identity_source,
                    relationship_scanned=1,
                    imported_at=excluded.imported_at,
                    insights_extracted_sha256=CASE
                        WHEN conversations.mirror_sha256 = excluded.mirror_sha256
                        THEN conversations.insights_extracted_sha256 ELSE NULL END
                """,
                (
                    conversation_id,
                    source_kind,
                    identity.project_id,
                    identity.root,
                    title,
                    started_at,
                    updated_at,
                    str(source),
                    str(mirror),
                    stat.st_size,
                    mirror_hash,
                    stat.st_mtime_ns,
                    len(parsed_messages),
                    user_count,
                    agent_count,
                    imported_at,
                    parent_id,
                    agent_path,
                    agent_nickname,
                    identity.identity_source,
                ),
            )
            for message in parsed_messages:
                message_id = "%s:%d" % (conversation_id, message["sequence"])
                self.connection.execute(
                    """
                    INSERT INTO conversation_messages (
                        message_id, conversation_id, sequence, role, text, occurred_at,
                        source_line, source_offset, content_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        conversation_id,
                        message["sequence"],
                        message["role"],
                        message["text"],
                        message["occurred_at"],
                        message["source_line"],
                        message["source_offset"],
                        message["content_sha256"],
                    ),
                )
                try:
                    self.connection.execute(
                        "INSERT INTO conversation_fts(message_id, conversation_id, text) VALUES (?, ?, ?)",
                        (message_id, conversation_id, redact_text(message["text"])[0]),
                    )
                except sqlite3.OperationalError:
                    pass
        try:
            self.settings.source_database_path.chmod(0o600)
            mirror.chmod(0o600)
        except OSError:
            pass
        return {
            "changed": True,
            "messages": len(parsed_messages),
            "bytes_copied": bytes_copied,
        }

    def _mirror_source(
        self, source: Path, mirror: Path, source_size: int, source_kind: str = "codex"
    ) -> int:
        mirror.parent.mkdir(parents=True, exist_ok=True)
        if mirror.exists():
            mirror_size = mirror.stat().st_size
            if source_size >= mirror_size and _prefix_matches(source, mirror, mirror_size):
                copied = source_size - mirror_size
                if copied:
                    with source.open("rb") as incoming, mirror.open("ab") as target:
                        incoming.seek(mirror_size)
                        self._copy_exact(incoming, target, copied)
                return copied
            old_hash = _sha256(mirror)[:16]
            version_dir = self.versions_dirs[source_kind] / mirror.stem
            version_dir.mkdir(parents=True, exist_ok=True)
            archived = version_dir / (old_hash + ".jsonl")
            if not archived.exists():
                mirror.replace(archived)
            else:
                mirror.unlink()
        temporary = mirror.with_suffix(".tmp")
        with source.open("rb") as incoming, temporary.open("wb") as target:
            self._copy_exact(incoming, target, source_size)
            target.flush()
            os.fsync(target.fileno())
        temporary.replace(mirror)
        return source_size

    @staticmethod
    def _copy_exact(source: Any, target: Any, size: int) -> None:
        remaining = size
        while remaining:
            chunk = source.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            target.write(chunk)
            remaining -= len(chunk)

    @staticmethod
    def _id_from_filename(path: Path) -> str:
        value = path.stem
        parts = value.split("-")
        if len(parts) >= 6 and value.startswith("rollout-"):
            return "-".join(parts[-5:])
        return hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _parse(path: Path) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        metadata: Dict[str, Any] = {}
        messages: List[Dict[str, Any]] = []
        offset = 0
        line_number = 0
        with path.open("rb") as handle:
            for raw_line in handle:
                line_number += 1
                line_offset = offset
                offset += len(raw_line)
                try:
                    event = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if event.get("type") == "session_meta":
                    metadata.update(event.get("payload") or {})
                    continue
                if event.get("type") != "event_msg":
                    continue
                payload = event.get("payload") or {}
                payload_type = payload.get("type")
                if payload_type not in ("user_message", "agent_message"):
                    continue
                text = payload.get("message")
                if not isinstance(text, str) or not text.strip():
                    continue
                sequence = len(messages) + 1
                messages.append(
                    {
                        "sequence": sequence,
                        "role": "user" if payload_type == "user_message" else "assistant",
                        "text": text,
                        "occurred_at": event.get("timestamp"),
                        "source_line": line_number,
                        "source_offset": line_offset,
                        "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    }
                )
        return metadata, messages

    @staticmethod
    def _parse_claude(path: Path) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        metadata: Dict[str, Any] = {}
        messages: List[Dict[str, Any]] = []
        offset = 0
        line_number = 0
        with path.open("rb") as handle:
            for raw_line in handle:
                line_number += 1
                line_offset = offset
                offset += len(raw_line)
                try:
                    event = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                session_id = event.get("sessionId")
                if session_id and "id" not in metadata:
                    metadata["id"] = str(session_id)
                cwd = event.get("cwd")
                if cwd and "cwd" not in metadata:
                    metadata["cwd"] = str(cwd)
                timestamp = event.get("timestamp")
                if timestamp and "timestamp" not in metadata:
                    metadata["timestamp"] = str(timestamp)
                if event.get("type") in ("custom-title", "ai-title"):
                    title = event.get("customTitle") or event.get("aiTitle")
                    if isinstance(title, str) and title.strip():
                        metadata["title"] = title.strip()[:120]
                    continue
                if event.get("type") not in ("user", "assistant"):
                    continue
                if event.get("isSidechain") is True:
                    continue
                message = event.get("message") or {}
                role = message.get("role") or event.get("type")
                if role not in ("user", "assistant"):
                    continue
                content = message.get("content")
                visible: List[str] = []
                if isinstance(content, str):
                    visible.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict) or block.get("type") != "text":
                            continue
                        text = block.get("text")
                        if isinstance(text, str):
                            visible.append(text)
                text = "\n\n".join(value for value in visible if value.strip()).strip()
                if not text:
                    continue
                if role == "user" and CodexConversationArchive._is_claude_synthetic(text):
                    continue
                sequence = len(messages) + 1
                messages.append(
                    {
                        "sequence": sequence,
                        "role": role,
                        "text": text,
                        "occurred_at": timestamp,
                        "source_line": line_number,
                        "source_offset": line_offset,
                        "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    }
                )
        return metadata, messages

    @staticmethod
    def _is_claude_synthetic(text: str) -> bool:
        stripped = text.lstrip()
        prefixes = (
            "<local-command-caveat>",
            "<local-command-stdout>",
            "<command-name>",
            "<command-message>",
            "<task-notification>",
            "<system-reminder>",
            "This session is being continued from a previous conversation that ran out of context.",
            "A session-scoped Stop hook is now active with condition:",
        )
        return stripped.startswith(prefixes)

    @staticmethod
    def _title(messages: List[Dict[str, Any]], metadata: Dict[str, Any]) -> str:
        first_user = next((message["text"] for message in messages if message["role"] == "user"), "")
        value = " ".join(first_user.split()) or str(metadata.get("id") or "Codex 会话")
        return value[:120]

    def conversations(
        self,
        project_path: Optional[Path] = None,
        query: str = "",
        limit: int = 100,
        include_duplicates: bool = False,
    ) -> List[Dict[str, Any]]:
        parameters: List[Any] = []
        clauses: List[str] = []
        if project_path is not None:
            clauses.append("project_id = ?")
            parameters.append(self.register_project(project_path)["project_id"])
        if not include_duplicates:
            clauses.append("duplicate_of IS NULL")
        if query.strip():
            clauses.append(
                "(title LIKE ? OR EXISTS ("
                "SELECT 1 FROM conversation_messages m "
                "WHERE m.conversation_id = conversations.conversation_id AND m.text LIKE ?))"
            )
            pattern = "%%%s%%" % query.strip()
            parameters.extend((pattern, pattern))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        parameters.append(max(1, min(limit, 500)))
        rows = self.connection.execute(
            "SELECT * FROM conversations%s ORDER BY updated_at DESC LIMIT ?" % where,
            parameters,
        ).fetchall()
        results = [dict(row) for row in rows]
        for result in results:
            result["sources"] = [
                str(source[0])
                for source in self.connection.execute(
                    """
                    SELECT DISTINCT source_kind FROM conversations
                    WHERE conversation_id = ? OR duplicate_of = ?
                    ORDER BY source_kind
                    """,
                    (result["conversation_id"], result["conversation_id"]),
                ).fetchall()
            ]
            result["source_copy_count"] = int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM conversations WHERE duplicate_of = ?",
                    (result["conversation_id"],),
                ).fetchone()[0]
            ) + 1
            result["readable_message_count"] = int(
                self.connection.execute(
                    """
                    SELECT COUNT(*) FROM conversation_messages
                    WHERE conversation_id = ?
                      AND text NOT LIKE '[external_agent_tool_call%'
                      AND text NOT LIKE '[external_agent_tool_result%'
                      AND text != '<EXTERNAL SESSION IMPORTED>'
                    """,
                    (result["conversation_id"],),
                ).fetchone()[0]
            )
        return results

    def _readable_message_count(self, conversation_id: str) -> int:
        return int(
            self.connection.execute(
                """
                SELECT COUNT(*) FROM conversation_messages
                WHERE conversation_id = ?
                  AND text NOT LIKE '[external_agent_tool_call%'
                  AND text NOT LIKE '[external_agent_tool_result%'
                  AND text != '<EXTERNAL SESSION IMPORTED>'
                """,
                (conversation_id,),
            ).fetchone()[0]
        )

    def _readable_hashes(self, conversation_id: str) -> set[str]:
        return {
            str(row["content_sha256"])
            for row in self.connection.execute(
                """
                SELECT content_sha256, text FROM conversation_messages
                WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchall()
            if not self._is_tool_trace(str(row["text"]))
        }

    @staticmethod
    def _task_merge_reason(source_copy_count: int, subagent_count: int) -> str:
        if source_copy_count > 1 and subagent_count:
            return "跨 Agent 内容重合，子 Agent 按父任务归组"
        if source_copy_count > 1:
            return "跨 Agent 首条用户消息一致且可读内容高度重合"
        if subagent_count:
            return "Codex 子 Agent 通过 parent_thread_id 归入根任务"
        return "独立根任务"

    @staticmethod
    def _task_root_id(
        conversation_id: str, rows: Dict[str, Dict[str, Any]]
    ) -> str:
        """Resolve duplicate and parent links without trusting malformed cycles."""
        current = conversation_id
        seen: set[str] = set()
        while current in rows and current not in seen:
            seen.add(current)
            row = rows[current]
            parent_id = str(row.get("parent_conversation_id") or "")
            duplicate_id = str(row.get("duplicate_of") or "")
            target = parent_id if parent_id in rows else duplicate_id
            if not target or target not in rows:
                break
            current = target
        return current if current in rows else conversation_id

    @staticmethod
    def _task_conversation_summary(row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: row.get(key)
            for key in (
                "conversation_id", "source_kind", "title", "started_at", "updated_at",
                "project_root", "message_count", "user_message_count",
                "agent_message_count", "parent_conversation_id", "agent_path",
                "agent_nickname", "duplicate_of", "mirror_path",
            )
        }

    def _task_groups(self, project_id: str) -> Dict[str, Dict[str, Any]]:
        raw_rows = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM conversations WHERE project_id = ?",
                (project_id,),
            ).fetchall()
        ]
        rows = {str(row["conversation_id"]): row for row in raw_rows}
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for conversation_id, row in rows.items():
            root_id = self._task_root_id(conversation_id, rows)
            groups.setdefault(root_id, []).append(row)

        tasks: Dict[str, Dict[str, Any]] = {}
        for root_id, members in groups.items():
            root_candidates = [
                row for row in members
                if not row.get("parent_conversation_id")
                or str(row.get("parent_conversation_id")) not in rows
            ]
            # Source copies describe equivalent top-level transcripts. Execution branches
            # remain separately inspectable and never inflate this count.
            source_copies = root_candidates or [rows.get(root_id) or members[0]]
            subagents = [
                row for row in members
                if row.get("parent_conversation_id")
                and str(row.get("parent_conversation_id")) in rows
            ]
            fingerprints = {
                str(row["conversation_id"]): self._conversation_fingerprint(
                    str(row["conversation_id"])
                )
                for row in source_copies
            }
            # task_id is the stable relationship root; primary is independently selected
            # for best reading coverage and may be a richer source copy.
            canonical = max(
                source_copies,
                key=lambda row: (
                    len(fingerprints[str(row["conversation_id"])]["hashes"]),
                    fingerprints[str(row["conversation_id"])]["visible_count"],
                    not fingerprints[str(row["conversation_id"])]["has_import_marker"],
                    str(row.get("updated_at") or ""),
                ),
            )
            source_kinds = sorted({str(row["source_kind"]) for row in source_copies})
            updated_at = max(
                (str(row.get("updated_at") or row.get("started_at") or "") for row in members),
                default="",
            ) or None
            readable = self._readable_message_count(str(canonical["conversation_id"])) + sum(
                self._readable_message_count(str(row["conversation_id"]))
                for row in subagents
            )
            raw_count = sum(int(row.get("message_count") or 0) for row in members)
            source_union: set[str] = set()
            for fingerprint in fingerprints.values():
                source_union.update(fingerprint["hashes"])
            primary_hashes = fingerprints[str(canonical["conversation_id"])]["hashes"]
            primary_coverage = (
                len(primary_hashes & source_union) / len(source_union)
                if source_union else 1.0
            )
            all_unique_hashes = set(source_union)
            for row in subagents:
                all_unique_hashes.update(self._readable_hashes(str(row["conversation_id"])))
            source_count = len(source_copies)
            subagent_count = len(subagents)
            tasks[root_id] = {
                "task_id": root_id,
                "title": str(canonical.get("title") or "Agent 任务"),
                "updated_at": updated_at,
                "project_root": str(canonical.get("project_root") or ""),
                "sources": source_kinds,
                "source_copy_count": source_count,
                "subagent_count": subagent_count,
                "readable_message_count": readable,
                "raw_message_count": raw_count,
                "primary_coverage": round(primary_coverage, 4),
                "unique_readable_message_count": len(all_unique_hashes),
                "has_content_gap": primary_coverage < 1.0,
                "merge_reason": self._task_merge_reason(source_count, subagent_count),
                "primary_conversation_id": str(canonical["conversation_id"]),
                "_members": members,
                "_source_copies": source_copies,
                "_subagents": subagents,
            }
        return tasks

    def tasks(
        self, project_path: Path, query: str = "", limit: int = 200
    ) -> List[Dict[str, Any]]:
        project_id = self.register_project(project_path)["project_id"]
        groups = self._task_groups(project_id)
        needle = query.strip().casefold()
        if needle:
            matching_ids = {
                str(row["conversation_id"])
                for row in self.connection.execute(
                    """
                    SELECT DISTINCT c.conversation_id FROM conversations c
                    LEFT JOIN conversation_messages m
                      ON m.conversation_id = c.conversation_id
                    WHERE c.project_id = ? AND (LOWER(c.title) LIKE ? OR LOWER(m.text) LIKE ?)
                    """,
                    (project_id, f"%{needle}%", f"%{needle}%"),
                ).fetchall()
            }
            values = [
                task for task in groups.values()
                if any(str(row["conversation_id"]) in matching_ids for row in task["_members"])
            ]
        else:
            values = list(groups.values())
        values.sort(key=lambda task: str(task["updated_at"] or ""), reverse=True)
        public_keys = (
            "task_id", "title", "updated_at", "project_root", "sources",
            "source_copy_count", "subagent_count", "readable_message_count",
            "raw_message_count", "primary_coverage", "unique_readable_message_count",
            "has_content_gap", "merge_reason", "primary_conversation_id",
        )
        return [
            {key: task[key] for key in public_keys}
            for task in values[: max(1, min(limit, 500))]
        ]

    def task(self, project_path: Path, task_id: str, redact: bool = True) -> Dict[str, Any]:
        project_id = self.register_project(project_path)["project_id"]
        group = self._task_groups(project_id).get(task_id)
        if group is None:
            raise ValueError("Unknown task or cross-project task access rejected")
        primary_id = str(group["primary_conversation_id"])
        result = {
            key: group[key]
            for key in (
                "task_id", "title", "updated_at", "project_root", "sources",
                "source_copy_count", "subagent_count", "readable_message_count",
                "raw_message_count", "primary_coverage", "unique_readable_message_count",
                "has_content_gap", "merge_reason", "primary_conversation_id",
            )
        }
        result["primary_conversation"] = self.conversation(primary_id, redact=redact)
        result["source_copies"] = []
        for row in sorted(
            group["_source_copies"], key=lambda item: str(item.get("started_at") or "")
        ):
            summary = self._task_conversation_summary(row)
            summary["readable_message_count"] = self._readable_message_count(
                str(row["conversation_id"])
            )
            summary["merge_reason"] = (
                "主会话" if row["conversation_id"] == primary_id
                else "跨 Agent 内容与主会话重合"
            )
            result["source_copies"].append(summary)
        result["subagents"] = []
        for row in sorted(
            group["_subagents"], key=lambda item: str(item.get("started_at") or "")
        ):
            summary = self._task_conversation_summary(row)
            summary["readable_message_count"] = self._readable_message_count(
                str(row["conversation_id"])
            )
            summary["merge_reason"] = "parent_thread_id 指向此根任务"
            result["subagents"].append(summary)
        return result

    def totals(self) -> Dict[str, Any]:
        memory_connection = sqlite3.connect(str(self.settings.database_path))
        memory_connection.row_factory = sqlite3.Row
        try:
            memory_rows = {
                str(row["status"]): int(row["count"])
                for row in memory_connection.execute(
                    "SELECT status, COUNT(*) AS count FROM memories GROUP BY status"
                ).fetchall()
            }
        finally:
            memory_connection.close()
        source_rows = {
            str(row["source_kind"]): {
                "conversations": int(row["conversations"]),
                "messages": int(row["messages"]),
            }
            for row in self.connection.execute(
                """
                SELECT source_kind, COUNT(*) AS conversations,
                       COALESCE(SUM(message_count), 0) AS messages
                FROM conversations GROUP BY source_kind
                """
            ).fetchall()
        }
        project_ids = [
            str(row[0])
            for row in self.connection.execute(
                "SELECT project_id FROM projects WHERE ignored = 0"
            ).fetchall()
        ]
        task_count = sum(len(self._task_groups(project_id)) for project_id in project_ids)
        return {
            "projects": int(
                self.connection.execute("SELECT COUNT(*) FROM projects WHERE ignored = 0").fetchone()[0]
            ),
            "tasks": task_count,
            "conversation_files": int(
                self.connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
            ),
            "duplicate_files": int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM conversations WHERE duplicate_of IS NOT NULL"
                ).fetchone()[0]
            ),
            "messages": int(
                self.connection.execute(
                    "SELECT COALESCE(SUM(message_count), 0) FROM conversations"
                ).fetchone()[0]
            ),
            "memories": memory_rows,
            "sources": source_rows,
        }

    def projects(self, include_ignored: bool = False) -> List[Dict[str, Any]]:
        where = "" if include_ignored else "WHERE p.ignored = 0"
        rows = self.connection.execute(
            """
            SELECT p.*,
                   (SELECT COUNT(*) FROM conversations c
                    WHERE c.project_id = p.project_id AND c.duplicate_of IS NULL
                      AND c.parent_conversation_id IS NULL) AS conversation_count,
                   (SELECT COALESCE(SUM(c.message_count), 0) FROM conversations c
                    WHERE c.project_id = p.project_id) AS message_count,
                   (SELECT COUNT(*) FROM conversations c
                    WHERE c.project_id = p.project_id AND c.source_kind = 'codex') AS codex_count,
                   (SELECT COUNT(*) FROM conversations c
                    WHERE c.project_id = p.project_id AND c.source_kind = 'claude-code') AS claude_count,
                   (SELECT COUNT(*) FROM conversations c
                    WHERE c.project_id = p.project_id
                      AND c.duplicate_of IS NULL AND c.parent_conversation_id IS NULL
                      AND (c.insights_extracted_sha256 IS NULL
                           OR c.insights_extracted_sha256 != c.mirror_sha256)) AS pending_count,
                   (SELECT COUNT(*) FROM project_roots r
                    WHERE r.project_id = p.project_id) AS root_count,
                   (SELECT MAX(c.updated_at) FROM conversations c
                    WHERE c.project_id = p.project_id) AS latest_conversation_at
            FROM projects p
            %s
            ORDER BY COALESCE(latest_conversation_at, p.last_seen_at) DESC
            """ % where
        ).fetchall()
        return [self._project_row(dict(row)) for row in rows]

    def project_detail(self, project_id: str) -> Dict[str, Any]:
        row = self.connection.execute(
            """
            SELECT p.*,
                   (SELECT COUNT(*) FROM conversations c
                    WHERE c.project_id = p.project_id AND c.duplicate_of IS NULL
                      AND c.parent_conversation_id IS NULL) AS conversation_count,
                   (SELECT COALESCE(SUM(c.message_count), 0) FROM conversations c
                    WHERE c.project_id = p.project_id) AS message_count,
                   (SELECT COUNT(*) FROM conversations c
                    WHERE c.project_id = p.project_id AND c.source_kind = 'codex') AS codex_count,
                   (SELECT COUNT(*) FROM conversations c
                    WHERE c.project_id = p.project_id AND c.source_kind = 'claude-code') AS claude_count,
                   (SELECT COUNT(*) FROM conversations c
                    WHERE c.project_id = p.project_id
                      AND c.duplicate_of IS NULL AND c.parent_conversation_id IS NULL
                      AND (c.insights_extracted_sha256 IS NULL
                           OR c.insights_extracted_sha256 != c.mirror_sha256)) AS pending_count,
                   (SELECT COUNT(*) FROM project_roots r
                    WHERE r.project_id = p.project_id) AS root_count,
                   (SELECT MAX(c.updated_at) FROM conversations c
                    WHERE c.project_id = p.project_id) AS latest_conversation_at
            FROM projects p
            WHERE p.project_id = ?
            """,
            (project_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Unknown project: %s" % project_id)
        return self._project_row(dict(row))

    def _project_row(self, project: Dict[str, Any]) -> Dict[str, Any]:
        roots = [
            str(row["project_root"])
            for row in self.connection.execute(
                """
                SELECT project_root FROM project_roots
                WHERE project_id = ? ORDER BY last_seen_at DESC, project_root
                """,
                (project["project_id"],),
            ).fetchall()
        ]
        canonical = str(project.get("canonical_root") or "")
        if not canonical or (not Path(canonical).exists() and roots):
            canonical = self._preferred_root(roots)
        project["canonical_root"] = canonical
        project["roots"] = roots
        project["display_name"] = str(project.get("display_name") or "") or self._default_project_name(
            str(project["project_id"]), canonical or (roots[0] if roots else "")
        )
        project["sync_enabled"] = bool(project.get("sync_enabled"))
        project["ignored"] = bool(project.get("ignored"))
        project["canonical_root_locked"] = bool(project.get("canonical_root_locked"))
        # Keep project cards consistent with tasks(): one cross-Agent task and its
        # subagents count as one item, including orphaned legacy relationship rows.
        project["conversation_count"] = len(
            self._task_groups(str(project["project_id"]))
        )
        for key in (
            "conversation_count", "message_count", "codex_count", "claude_count",
            "pending_count", "root_count",
        ):
            project[key] = int(project.get(key) or 0)
        return project

    def update_project(
        self,
        project_id: str,
        *,
        display_name: Optional[str] = None,
        sync_enabled: Optional[bool] = None,
        ignored: Optional[bool] = None,
        canonical_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        current = self.project_detail(project_id)
        if canonical_root is not None and canonical_root not in current["roots"]:
            raise ValueError("Canonical root must belong to this project")
        values = {
            "display_name": current["display_name"] if display_name is None else display_name.strip(),
            "sync_enabled": current["sync_enabled"] if sync_enabled is None else bool(sync_enabled),
            "ignored": current["ignored"] if ignored is None else bool(ignored),
            "canonical_root": current["canonical_root"] if canonical_root is None else canonical_root,
        }
        with self.connection:
            self.connection.execute(
                """
                UPDATE projects
                SET display_name = ?, sync_enabled = ?, ignored = ?,
                    canonical_root = ?,
                    canonical_root_locked = CASE WHEN ? THEN 1 ELSE canonical_root_locked END,
                    last_seen_at = ?
                WHERE project_id = ?
                """,
                (
                    values["display_name"] or None,
                    int(values["sync_enabled"]),
                    int(values["ignored"]),
                    values["canonical_root"] or None,
                    canonical_root is not None,
                    _now(),
                    project_id,
                ),
            )
        return self.project_detail(project_id)

    def conversation(self, conversation_id: str, redact: bool = False) -> Dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM conversations WHERE conversation_id = ?", (conversation_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Unknown conversation: %s" % conversation_id)
        messages = [
            dict(message)
            for message in self.connection.execute(
                "SELECT * FROM conversation_messages WHERE conversation_id = ? ORDER BY sequence",
                (conversation_id,),
            )
        ]
        if redact:
            for message in messages:
                message["text"] = redact_text(message["text"])[0]
        result = dict(row)
        result["messages"] = messages
        return result

    def pending_insight_conversations(
        self, project_path: Path, limit: int = 20
    ) -> List[Dict[str, Any]]:
        project_id = self.register_project(project_path)["project_id"]
        rows = self.connection.execute(
            """
            SELECT * FROM conversations
            WHERE project_id = ?
              AND duplicate_of IS NULL
              AND parent_conversation_id IS NULL
              AND (insights_extracted_sha256 IS NULL
                   OR insights_extracted_sha256 != mirror_sha256)
            ORDER BY updated_at ASC LIMIT ?
            """,
            (project_id, max(1, min(limit, 100))),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_insights_extracted(self, conversation_id: str, mirror_sha256: str) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE conversations SET insights_extracted_sha256 = ?
                WHERE conversation_id = ? AND mirror_sha256 = ?
                """,
                (mirror_sha256, conversation_id, mirror_sha256),
            )

    def reset_insight_pipeline(self, reason: str = "candidate-reset") -> Dict[str, int]:
        canonical = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM conversations WHERE duplicate_of IS NULL "
                "AND parent_conversation_id IS NULL"
            ).fetchone()[0]
        )
        with self.connection:
            self.connection.execute(
                "INSERT INTO insight_runs(run_id, reset_at, reason) VALUES (?, ?, ?)",
                (hashlib.sha256((reason + _now()).encode("utf-8")).hexdigest(), _now(), reason),
            )
            self.connection.execute("DELETE FROM insight_chunks")
            self.connection.execute(
                "UPDATE conversations SET insights_extracted_sha256 = NULL "
                "WHERE duplicate_of IS NULL AND parent_conversation_id IS NULL"
            )
            self.connection.execute(
                "UPDATE conversations SET insights_extracted_sha256 = mirror_sha256 "
                "WHERE duplicate_of IS NOT NULL OR parent_conversation_id IS NOT NULL"
            )
        return {"queued": canonical, "duplicates_skipped": int(
            self.connection.execute(
                "SELECT COUNT(*) FROM conversations WHERE duplicate_of IS NOT NULL"
            ).fetchone()[0]
        ), "subagents_skipped": int(
            self.connection.execute(
                "SELECT COUNT(*) FROM conversations WHERE parent_conversation_id IS NOT NULL"
            ).fetchone()[0]
        )}

    def insight_chunk_status(
        self, conversation_id: str, chunk_sha256: str
    ) -> Optional[str]:
        row = self.connection.execute(
            "SELECT status FROM insight_chunks WHERE conversation_id = ? AND chunk_sha256 = ?",
            (conversation_id, chunk_sha256),
        ).fetchone()
        return str(row["status"]) if row else None

    def mark_insight_chunk(
        self,
        conversation_id: str,
        chunk_sha256: str,
        sequence_start: int,
        sequence_end: int,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        if status not in ("complete", "failed"):
            raise ValueError("Unsupported insight chunk status")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO insight_chunks (
                    conversation_id, chunk_sha256, sequence_start, sequence_end,
                    status, attempted_at, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id, chunk_sha256) DO UPDATE SET
                    sequence_start=excluded.sequence_start,
                    sequence_end=excluded.sequence_end,
                    status=excluded.status,
                    attempted_at=excluded.attempted_at,
                    error=excluded.error
                """,
                (
                    conversation_id,
                    chunk_sha256,
                    sequence_start,
                    sequence_end,
                    status,
                    _now(),
                    (error or "")[:1000] or None,
                ),
            )

    def insight_stats(self, project_path: Path) -> Dict[str, int]:
        project_id = self.register_project(project_path)["project_id"]
        row = self.connection.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN insights_extracted_sha256 = mirror_sha256 THEN 1 ELSE 0 END)
                    AS extracted,
                SUM(CASE WHEN insights_extracted_sha256 IS NULL
                              OR insights_extracted_sha256 != mirror_sha256 THEN 1 ELSE 0 END)
                    AS pending,
                COALESCE(SUM(message_count), 0) AS messages
            FROM conversations WHERE project_id = ? AND duplicate_of IS NULL
              AND parent_conversation_id IS NULL
            """,
            (project_id,),
        ).fetchone()
        failed = self.connection.execute(
            """
            SELECT COUNT(DISTINCT c.conversation_id)
            FROM conversations c
            JOIN insight_chunks i ON i.conversation_id = c.conversation_id
            WHERE c.project_id = ? AND c.duplicate_of IS NULL
              AND c.parent_conversation_id IS NULL AND i.status = 'failed'
              AND (c.insights_extracted_sha256 IS NULL
                   OR c.insights_extracted_sha256 != c.mirror_sha256)
            """,
            (project_id,),
        ).fetchone()[0]
        return {
            "total": int(row["total"] or 0),
            "extracted": int(row["extracted"] or 0),
            "pending": int(row["pending"] or 0),
            "failed": int(failed or 0),
            "messages": int(row["messages"] or 0),
        }

    def search_messages(
        self, project_path: Path, query: str, limit: int = 20, redact: bool = True
    ) -> List[Dict[str, Any]]:
        project_id = self.register_project(project_path)["project_id"]
        limit = max(1, min(limit, 100))
        terms = [term for term in query.replace('"', " ").split() if term]
        rows: Iterable[sqlite3.Row]
        if terms:
            expression = " AND ".join('"%s"' % term for term in terms)
            try:
                rows = self.connection.execute(
                    """
                    SELECT m.*, c.title, c.project_root
                    FROM conversation_fts f
                    JOIN conversation_messages m ON m.message_id = f.message_id
                    JOIN conversations c ON c.conversation_id = m.conversation_id
                    WHERE conversation_fts MATCH ? AND c.project_id = ?
                    ORDER BY bm25(conversation_fts), c.updated_at DESC
                    LIMIT ?
                    """,
                    (expression, project_id, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        else:
            rows = self.connection.execute(
                """
                SELECT m.*, c.title, c.project_root
                FROM conversation_messages m
                JOIN conversations c ON c.conversation_id = m.conversation_id
                WHERE c.project_id = ?
                ORDER BY c.updated_at DESC, m.sequence DESC LIMIT ?
                """,
                (project_id, limit),
            ).fetchall()
        results = [dict(row) for row in rows]
        if redact:
            for result in results:
                result["text"] = redact_text(result["text"])[0]
                result["title"] = redact_text(result["title"])[0]
        return results
