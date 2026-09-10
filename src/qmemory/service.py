from __future__ import annotations

import uuid
import fcntl
import hashlib
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .config import Settings
from .events import EventStore, utc_now
from .events import validate_shard
from .project import ProjectIdentity, identify_project
from .redaction import redact_text
from .store import Projection
from .sources import CodexConversationArchive


MEMORY_TYPES = {
    "decision",
    "constraint",
    "environment",
    "incident",
    "status",
    "handoff",
    "fact",
}
FEEDBACK_SIGNALS = {"helpful", "ignored", "rejected"}


class MemoryService:
    def __init__(self, home: Optional[Path] = None, device_id: Optional[str] = None) -> None:
        self.settings = Settings(home)
        self.settings.ensure(device_id=device_id)
        self.events = EventStore(self.settings)
        self.projection = Projection(self.events)

    def codex_preview(self, codex_home: Optional[Path] = None) -> Dict[str, Any]:
        archive = CodexConversationArchive(self.settings)
        try:
            return archive.preview(codex_home)
        finally:
            archive.close()

    def conversation_sources_preview(
        self,
        codex_home: Optional[Path] = None,
        claude_home: Optional[Path] = None,
    ) -> Dict[str, Any]:
        archive = CodexConversationArchive(self.settings)
        try:
            return archive.preview_all(codex_home, claude_home)
        finally:
            archive.close()

    def codex_sync(self, codex_home: Optional[Path] = None) -> Dict[str, Any]:
        archive = CodexConversationArchive(self.settings)
        try:
            return archive.sync(codex_home)
        finally:
            archive.close()

    def conversation_sources_sync(
        self,
        codex_home: Optional[Path] = None,
        claude_home: Optional[Path] = None,
    ) -> Dict[str, Any]:
        from .bootstrap import fingerprint, source_key
        from .usage import Usage
        with (self.settings.state_dir / 'bootstrap-sync.lock').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                stamp, _ = fingerprint(codex_home, claude_home)
            except OSError:
                stamp = None
            archive = CodexConversationArchive(self.settings)
            try:
                result = archive.sync_all(codex_home, claude_home)
            finally:
                archive.close()
            if stamp is not None and not result.get('failures'):
                try:
                    with Usage(self.settings).connect() as db:
                        now = time.time()
                        db.execute('INSERT OR REPLACE INTO freshness VALUES(?,?,?,?)',
                                   (source_key(codex_home, claude_home), stamp, now, now))
                except Exception:
                    pass  # Operational cache loss must not fail a completed archive.
            return result

    def conversations(
        self,
        project_path: Optional[Path] = None,
        query: str = "",
        limit: int = 100,
        include_duplicates: bool = False,
    ) -> List[Dict[str, Any]]:
        archive = CodexConversationArchive(self.settings)
        try:
            return archive.conversations(project_path, query, limit, include_duplicates)
        finally:
            archive.close()

    def tasks(
        self, project_path: Path, query: str = "", limit: int = 200
    ) -> List[Dict[str, Any]]:
        archive = CodexConversationArchive(self.settings)
        try:
            return archive.tasks(project_path, query=query, limit=limit)
        finally:
            archive.close()

    def task(self, project_path: Path, task_id: str) -> Dict[str, Any]:
        archive = CodexConversationArchive(self.settings)
        try:
            result = archive.task(project_path, task_id, redact=True)
            result["primary_conversation"]["messages"] = [
                message
                for message in result["primary_conversation"]["messages"]
                if not archive._is_tool_trace(str(message["text"]))
            ]
            result["primary_conversation"]["raw_message_count"] = result[
                "primary_conversation"
            ]["message_count"]
            result["primary_conversation"]["message_count"] = len(
                result["primary_conversation"]["messages"]
            )
            return result
        finally:
            archive.close()

    def totals(self) -> Dict[str, Any]:
        archive = CodexConversationArchive(self.settings)
        try:
            return archive.totals()
        finally:
            archive.close()

    def retrieval_stats(self, project_path: Optional[Path] = None) -> Dict[str, Any]:
        project_id = self.project(project_path).project_id if project_path is not None else None
        result = self.projection.retrieval_stats(project_id)
        usage = self.usage_stats(project_path)
        result['ledger_impressions'] = result['impressions']
        result['local_context_impressions'] = usage.get('context_impressions', 0)
        result['impressions'] += result['local_context_impressions']
        result['feedback_coverage'] = round(result['rated'] / result['impressions'], 4) if result['impressions'] else None
        return result

    def usage_stats(self, project_path: Optional[Path] = None) -> Dict[str, Any]:
        from .usage import Usage
        project_id = self.project(project_path).project_id if project_path is not None else None
        return Usage(self.settings).stats(project_id)

    def quality_report(self, project_path: Optional[Path] = None) -> Dict[str, Any]:
        from .quality import build_quality_report

        return build_quality_report(self, project_path=project_path)

    def projects(self, include_ignored: bool = False) -> List[Dict[str, Any]]:
        archive = CodexConversationArchive(self.settings)
        try:
            return archive.projects(include_ignored=include_ignored)
        finally:
            archive.close()

    def register_project(self, project_path: Path) -> Dict[str, Any]:
        archive = CodexConversationArchive(self.settings)
        try:
            return archive.register_project(project_path)
        finally:
            archive.close()

    def bind_conversation_project(
        self, conversation_id: str, project_path: Path
    ) -> Dict[str, Any]:
        archive = CodexConversationArchive(self.settings)
        try:
            return archive.bind_conversation_project(conversation_id, project_path)
        finally:
            archive.close()

    def recompute_project_mappings(self) -> Dict[str, int]:
        archive = CodexConversationArchive(self.settings)
        try:
            return archive.recompute_project_mappings()
        finally:
            archive.close()

    def update_project(
        self,
        project_id: str,
        *,
        display_name: Optional[str] = None,
        sync_enabled: Optional[bool] = None,
        ignored: Optional[bool] = None,
        canonical_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        archive = CodexConversationArchive(self.settings)
        try:
            return archive.update_project(
                project_id,
                display_name=display_name,
                sync_enabled=sync_enabled,
                ignored=ignored,
                canonical_root=canonical_root,
            )
        finally:
            archive.close()

    def conversation(
        self, project_path: Path, conversation_id: str, redact: bool = True
    ) -> Dict[str, Any]:
        archive = CodexConversationArchive(self.settings)
        try:
            result = archive.conversation(conversation_id, redact=redact)
            expected = self.project(project_path).project_id
            if result["project_id"] != expected:
                raise ValueError("Cross-project conversation access rejected")
            result["messages"] = [
                message
                for message in result["messages"]
                if not archive._is_tool_trace(str(message["text"]))
            ]
            result["raw_message_count"] = result["message_count"]
            result["message_count"] = len(result["messages"])
            return result
        finally:
            archive.close()

    def conversation_search(
        self, project_path: Path, query: str = "", limit: int = 20
    ) -> List[Dict[str, Any]]:
        archive = CodexConversationArchive(self.settings)
        try:
            return archive.search_messages(project_path, query, limit, redact=True)
        finally:
            archive.close()

    def insight_stats(self, project_path: Path) -> Dict[str, int]:
        archive = CodexConversationArchive(self.settings)
        try:
            return archive.insight_stats(project_path)
        finally:
            archive.close()

    def reset_insight_pipeline(self, reason: str = "candidate-reset") -> Dict[str, int]:
        archive = CodexConversationArchive(self.settings)
        try:
            return archive.reset_insight_pipeline(reason)
        finally:
            archive.close()

    def initialize(self) -> Dict[str, Any]:
        result = self.projection.rebuild()
        result["device_id"] = self.settings.device_id
        result["home"] = str(self.settings.home)
        return result

    @staticmethod
    def project(path: Path) -> ProjectIdentity:
        return identify_project(path)

    def remember(
        self,
        project_path: Path,
        memory_type: str,
        statement: str,
        subject: str = "project",
        holder: str = "user",
        confirmed: bool = False,
        as_of: Optional[str] = None,
        source_kind: str = "agent",
        source_ref: Optional[str] = None,
        source_anchor: Optional[str] = None,
    ) -> Dict[str, Any]:
        if memory_type not in MEMORY_TYPES:
            raise ValueError("Unsupported memory type: %s" % memory_type)
        statement = statement.strip()
        subject = subject.strip()
        holder = holder.strip()
        if not statement or not subject or not holder:
            raise ValueError("holder, subject, and statement are required")
        project = self.project(project_path)
        safe_statement, statement_findings = redact_text(statement)
        safe_subject, subject_findings = redact_text(subject)
        safe_holder, holder_findings = redact_text(holder)
        safe_ref, ref_findings = redact_text(source_ref or "")
        safe_anchor, anchor_findings = redact_text(source_anchor or "")
        redactions = sorted(
            set(
                statement_findings
                + subject_findings
                + holder_findings
                + ref_findings
                + anchor_findings
            )
        )
        current = self.projection.search(
            project.project_id,
            query="",
            statuses=("active", "proposed"),
            limit=100,
        )
        duplicate = next(
            (
                item
                for item in current
                if item["memory_type"] == memory_type
                and item["statement"] == safe_statement
                and item["subject"] == safe_subject
                and item["holder"] == safe_holder
            ),
            None,
        )
        if duplicate is not None:
            duplicate["deduplicated"] = True
            return duplicate
        memory = self._memory_payload(
            memory_type=memory_type,
            statement=safe_statement,
            subject=safe_subject,
            holder=safe_holder,
            status="active" if confirmed else "proposed",
            as_of=as_of or utc_now(),
            source_kind=source_kind,
            source_ref=safe_ref or None,
            source_anchor=safe_anchor or None,
            commit_sha=project.commit_sha,
            redactions=redactions,
        )
        event = self.events.append("memory.created", project.project_id, {"memory": memory})
        self.projection.rebuild()
        result = self.projection.get(memory["memory_id"])
        assert result is not None
        result["event_id"] = event["event_id"]
        result["redactions"] = redactions
        return result

    @staticmethod
    def _memory_payload(
        memory_type: str,
        statement: str,
        subject: str,
        holder: str,
        status: str,
        as_of: str,
        source_kind: str,
        source_ref: Optional[str],
        source_anchor: Optional[str],
        commit_sha: Optional[str],
        redactions: List[str],
    ) -> Dict[str, Any]:
        return {
            "memory_id": str(uuid.uuid4()),
            "memory_type": memory_type,
            "holder": holder,
            "subject": subject,
            "statement": statement,
            "status": status,
            "as_of": as_of,
            "source_kind": source_kind,
            "source_ref": source_ref,
            "source_anchor": source_anchor,
            "commit_sha": commit_sha,
            "redactions": redactions,
        }

    def confirm(self, project_path: Path, memory_id: str) -> Dict[str, Any]:
        project = self.project(project_path)
        current = self._require_in_project(memory_id, project.project_id)
        if current["status"] == "active":
            return current
        if current["status"] != "proposed":
            raise ValueError("Only proposed memories can be confirmed")
        self.events.append(
            "memory.confirmed", project.project_id, {"memory_id": memory_id}
        )
        self.projection.rebuild()
        result = self.projection.get(memory_id)
        assert result is not None
        return result

    def supersede(
        self,
        project_path: Path,
        memory_id: str,
        statement: str,
        confirmed: bool = True,
        source_ref: Optional[str] = None,
    ) -> Dict[str, Any]:
        project = self.project(project_path)
        current = self._require_in_project(memory_id, project.project_id)
        if current["status"] not in ("active", "proposed"):
            raise ValueError("Only current memories can be superseded")
        safe_statement, findings = redact_text(statement.strip())
        safe_ref, ref_findings = redact_text(source_ref or "")
        if not safe_statement:
            raise ValueError("Replacement statement is required")
        replacement = self._memory_payload(
            memory_type=current["memory_type"],
            statement=safe_statement,
            subject=current["subject"],
            holder=current["holder"],
            status="active" if confirmed else "proposed",
            as_of=utc_now(),
            source_kind="supersede",
            source_ref=safe_ref or None,
            source_anchor=None,
            commit_sha=project.commit_sha,
            redactions=sorted(set(findings + ref_findings)),
        )
        self.events.append(
            "memory.superseded",
            project.project_id,
            {"memory_id": memory_id, "replacement": replacement},
        )
        self.projection.rebuild()
        result = self.projection.get(replacement["memory_id"])
        assert result is not None
        result["supersedes"] = memory_id
        return result

    def archive(self, project_path: Path, memory_id: str) -> Dict[str, Any]:
        project = self.project(project_path)
        self._require_in_project(memory_id, project.project_id)
        self.events.append(
            "memory.archived", project.project_id, {"memory_id": memory_id}
        )
        self.projection.rebuild()
        result = self.projection.get(memory_id)
        assert result is not None
        return result

    def archive_all_proposed(self) -> Dict[str, Any]:
        """Archive every proposed memory with reversible lifecycle events."""
        connection = self.projection.connect()
        try:
            rows = connection.execute(
                """
                SELECT memory_id, project_id FROM memories
                WHERE status = 'proposed' ORDER BY created_at, memory_id
                """
            ).fetchall()
        finally:
            connection.close()
        by_project: Dict[str, int] = {}
        for row in rows:
            project_id = str(row["project_id"])
            self.events.append(
                "memory.archived",
                project_id,
                {"memory_id": str(row["memory_id"]), "reason": "candidate-reset"},
            )
            by_project[project_id] = by_project.get(project_id, 0) + 1
        result = self.projection.rebuild()
        return {
            "archived": len(rows),
            "projects": len(by_project),
            "by_project": by_project,
            "projection_issues": int(result.get("issues", 0)),
        }

    def search(
        self,
        project_path: Path,
        query: str = "",
        include_proposed: bool = False,
        limit: int = 10,
        record_impressions: bool = False,
    ) -> List[Dict[str, Any]]:
        from .usage import Usage
        started = time.perf_counter()
        project = self.project(project_path)
        statuses: Sequence[str] = ("active", "proposed") if include_proposed else ("active",)
        results = self.projection.search(project.project_id, query, statuses, limit)
        if record_impressions and results:
            for result in results:
                self.events.append(
                    "memory.impression",
                    project.project_id,
                    {"memory_id": result["memory_id"], "query": query},
                )
            self.projection.rebuild()
        Usage(self.settings).record(project.project_id, 'memory_search', returned=len(results), empty=int(not results),
                                    duration_ms=(time.perf_counter() - started) * 1000)
        return results

    def browse(
        self,
        project_path: Path,
        query: str = "",
        status: str = "current",
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        project = self.project(project_path)
        status_map = {
            "current": ("active", "proposed"),
            "active": ("active",),
            "proposed": ("proposed",),
            "history": ("superseded", "archived"),
            "all": ("active", "proposed", "superseded", "archived"),
        }
        if status not in status_map:
            raise ValueError("Unsupported browse status: %s" % status)
        return self.projection.search(
            project.project_id,
            query=query,
            statuses=status_map[status],
            limit=limit,
        )

    def context_pack(
        self, project_path: Path, query: str = "", limit: int = 12,
        *, _record: bool = True, _budget: int = 1400,
    ) -> Dict[str, Any]:
        from .context import build_context, payload_tokens
        from .usage import Usage
        started = time.perf_counter()
        project = self.project(project_path)
        try:
            result = build_context(self.projection, project, query, limit, _budget)
            result['available'] = True
        except Exception as exc:
            result = {'project': {'project_id': project.project_id, 'root': project.root},
                      'memories': [], 'available': False, 'error': type(exc).__name__,
                      'instruction': 'Continue local work without memory; one recoverable notice, no retry loop.'}
        elapsed = (time.perf_counter() - started) * 1000
        result['estimated_payload_tokens'] = payload_tokens(result) + 4
        if _record:
            Usage(self.settings).record(project.project_id, 'project_context', returned=len(result['memories']),
                empty=int(not result['memories']), errors=int(not result['available']),
                duration_ms=elapsed, context_ms=elapsed, tokens=result['estimated_payload_tokens'])
        return result

    def project_bootstrap(self, project_path: Path, query: str = '', limit: int = 12,
                          ttl_seconds: int = 300, *, codex_home=None, claude_home=None) -> Dict[str, Any]:
        from .bootstrap import sync_if_needed
        from .context import payload_tokens
        from .usage import Usage
        started = time.perf_counter()
        sync = sync_if_needed(self, codex_home=codex_home, claude_home=claude_home, ttl_seconds=ttl_seconds)
        context_started = time.perf_counter()
        result = self.context_pack(project_path, query, limit, _record=False, _budget=1100)
        elapsed = (time.perf_counter() - context_started) * 1000
        result.update(sync)
        result['fail_open'] = not result['available'] or sync['sync_reason'] in ('busy', 'unavailable', 'partial_failure')
        result['estimated_payload_tokens'] = payload_tokens(result) + 4
        Usage(self.settings).record(result['project']['project_id'], 'project_bootstrap',
            returned=len(result['memories']), empty=int(not result['memories']),
            noop=int(sync['sync_reason'] in ('fresh', 'unchanged')),
            errors=int(result['fail_open']), duration_ms=(time.perf_counter() - started) * 1000,
            context_ms=elapsed, tokens=result['estimated_payload_tokens'],
            bytes_copied=sync['bytes_copied'], files_scanned=sync['files_scanned'])
        return result

    def feedback(
        self, project_path: Path, memory_id: str, signal: str
    ) -> Dict[str, Any]:
        if signal not in FEEDBACK_SIGNALS:
            raise ValueError("Unsupported feedback signal")
        project = self.project(project_path)
        self._require_in_project(memory_id, project.project_id)
        self.events.append(
            "memory.feedback",
            project.project_id,
            {"memory_id": memory_id, "signal": signal},
        )
        self.projection.rebuild()
        return {"memory_id": memory_id, "signal": signal}

    def session_handoff(
        self, project_path: Path, statement: str, source_ref: Optional[str] = None
    ) -> Dict[str, Any]:
        from .usage import Usage
        if not statement.strip() or len(statement) > 4000:
            raise ValueError('Handoff requires a concise summary of changed state, remaining work and next step (1-4000 characters)')
        project = self.project(project_path)
        normalize = lambda s: re.sub(r'\s+', ' ', unicodedata.normalize('NFC', redact_text(s)[0])).strip()
        lock_name = hashlib.sha256(project.project_id.encode()).hexdigest()
        with (self.settings.state_dir / ('handoff-' + lock_name + '.lock')).open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            connection = self.projection.connect()
            try:
                row = connection.execute("SELECT * FROM memories WHERE project_id=? AND status='active' AND memory_type='handoff' AND subject='current work' AND holder='agent' ORDER BY created_at DESC,memory_id DESC LIMIT 1", (project.project_id,)).fetchone()
                current = dict(row) if row else None
            finally:
                connection.close()
            if current is not None and normalize(current['statement']) == normalize(statement):
                result = {**current, 'noop': True, 'changed': False, 'reason': 'normalized_content_unchanged'}
            elif current is not None:
                result = self.supersede(project_path, current['memory_id'], statement, confirmed=True, source_ref=source_ref)
                result.update(noop=False, changed=True)
            else:
                result = self.remember(project_path, 'handoff', statement, subject='current work', holder='agent',
                                       confirmed=True, source_kind='mcp', source_ref=source_ref)
                result.update(noop=False, changed=True)
        Usage(self.settings).record(project.project_id, 'session_handoff', noop=int(result['noop']))
        return result

    def sync_push(self, directory: Path) -> Dict[str, Any]:
        return self.events.publish(directory)

    def sync_pull(self, directory: Path) -> Dict[str, Any]:
        result = self.events.pull(directory)
        result["projection"] = self.projection.rebuild()
        return result

    def status(self) -> Dict[str, Any]:
        status = self.projection.status()
        status.update(
            {
                "device_id": self.settings.device_id,
                "home": str(self.settings.home),
                "shards": len(list(self.settings.events_dir.glob("*.jsonl"))),
            }
        )
        return status

    def doctor(self) -> Dict[str, Any]:
        checks: List[Dict[str, Any]] = []
        for shard in sorted(self.settings.events_dir.glob("*.jsonl")):
            try:
                event_count = len(validate_shard(shard))
                checks.append(
                    {"check": "shard:%s" % shard.name, "ok": True, "events": event_count}
                )
            except ValueError as exc:
                checks.append(
                    {"check": "shard:%s" % shard.name, "ok": False, "detail": str(exc)}
                )
        projection = self.projection.rebuild()
        checks.append(
            {
                "check": "projection",
                "ok": projection["issues"] == 0,
                "detail": projection,
            }
        )
        checks.append(
            {
                "check": "conflict-quarantine",
                "ok": not any(self.settings.conflicts_dir.iterdir()),
                "files": len(list(self.settings.conflicts_dir.iterdir())),
            }
        )
        return {
            "ok": all(check["ok"] for check in checks),
            "device_id": self.settings.device_id,
            "checks": checks,
        }

    def _require_in_project(self, memory_id: str, project_id: str) -> Dict[str, Any]:
        current = self.projection.get(memory_id)
        if current is None:
            raise ValueError("Unknown memory: %s" % memory_id)
        if current["project_id"] != project_id:
            raise ValueError("Cross-project memory access rejected")
        return current
