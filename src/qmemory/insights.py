from __future__ import annotations

import json
import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from .service import MEMORY_TYPES, MemoryService
from .sources import CodexConversationArchive
from .textio import run_text


OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "insights": {
            "type": "array",
            "maxItems": 4,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "memory_type": {"type": "string", "enum": sorted(MEMORY_TYPES)},
                    "holder": {"type": "string", "enum": ["user", "agent"]},
                    "subject": {"type": "string", "minLength": 1},
                    "statement": {"type": "string", "minLength": 1},
                    "evidence_sequence": {"type": "integer", "minimum": 1},
                },
                "required": [
                    "memory_type",
                    "holder",
                    "subject",
                    "statement",
                    "evidence_sequence",
                ],
            },
        }
    },
    "required": ["insights"],
}


MAX_INSIGHTS_PER_CONVERSATION = 4


PROMPT = """You extract a very small set of durable project judgments from one archived
coding-agent conversation. Precision is much more important than recall.
Return JSON that exactly matches the provided schema.

Extract only explicit user decisions, constraints, verified environment facts, incident root
causes/fixes, or final project state that would materially prevent a future agent from making a
mistake. Do not extract routine implementation steps, progress updates, tool output, plans that
were never executed, generic best practices, praise, questions, speculation, raw logs, or secrets.
Never extract localhost URLs, preview-server status, temporary paths, one-off file copy operations,
test pass counts, build progress, or facts that are useful only during the completed task.
Do not restate the task request as a memory. Prefer returning zero items over a weak item. Return
at most four insights for the entire conversation. Each statement must be concise and standalone.
Use holder=user only when the user stated or explicitly approved it; otherwise holder=agent.
evidence_sequence must point to the single strongest supporting message. It is valid and often
correct to return an empty insights array.
"""


class CodexInsightExtractor:
    def __init__(self, service: MemoryService, codex: Optional[Path] = None) -> None:
        self.service = service
        self.codex = codex or self._find_codex()

    @staticmethod
    def _find_codex() -> Optional[Path]:
        candidates = [
            Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
            Path("/opt/homebrew/bin/codex"),
            Path("/usr/local/bin/codex"),
        ]
        discovered = shutil.which("codex")
        if discovered:
            candidates.insert(0, Path(discovered))
        return next((candidate for candidate in candidates if candidate.exists()), None)

    def extract_pending(self, project_path: Path, limit: int = 10) -> Dict[str, Any]:
        if self.codex is None:
            raise RuntimeError("Codex CLI was not found")
        archive = CodexConversationArchive(self.service.settings)
        try:
            pending = archive.pending_insight_conversations(project_path, limit)
            conversations_done = 0
            proposals = 0
            chunks_done = 0
            chunks_skipped = 0
            failures = 0
            for row in pending:
                detail = archive.conversation(row["conversation_id"], redact=True)
                seen = set()
                conversation_failed = False
                accepted = 0
                for chunk in self._chunks(detail):
                    chunk_hash = self._chunk_sha256(chunk)
                    if archive.insight_chunk_status(row["conversation_id"], chunk_hash) == "complete":
                        chunks_skipped += 1
                        continue
                    sequences = [int(message["sequence"]) for message in chunk["messages"]]
                    try:
                        extracted = self._extract_one(project_path, chunk)
                        for insight in extracted:
                            if accepted >= MAX_INSIGHTS_PER_CONVERSATION:
                                break
                            key = (
                                insight.get("memory_type"),
                                insight.get("holder"),
                                insight.get("subject"),
                                insight.get("statement"),
                                insight.get("evidence_sequence"),
                            )
                            if key in seen:
                                continue
                            seen.add(key)
                            sequence = int(insight["evidence_sequence"])
                            if sequence not in sequences:
                                continue
                            result = self.service.remember(
                                project_path,
                                str(insight["memory_type"]),
                                str(insight["statement"]),
                                subject=str(insight["subject"]),
                                holder=str(insight["holder"]),
                                confirmed=False,
                                source_kind="agent-insight",
                                source_ref="%s://%s#message-%d"
                                % (
                                    row["source_kind"],
                                    str(row["conversation_id"]).removeprefix("claude:"),
                                    sequence,
                                ),
                            )
                            if not result.get("deduplicated"):
                                proposals += 1
                                accepted += 1
                        archive.mark_insight_chunk(
                            row["conversation_id"],
                            chunk_hash,
                            min(sequences),
                            max(sequences),
                            "complete",
                        )
                        chunks_done += 1
                    except Exception as exc:
                        archive.mark_insight_chunk(
                            row["conversation_id"],
                            chunk_hash,
                            min(sequences),
                            max(sequences),
                            "failed",
                            str(exc),
                        )
                        failures += 1
                        conversation_failed = True
                        break
                if not conversation_failed:
                    archive.mark_insights_extracted(
                        row["conversation_id"], row["mirror_sha256"]
                    )
                    conversations_done += 1
            return {
                "conversations": conversations_done,
                "proposals": proposals,
                "chunks": chunks_done,
                "chunks_skipped": chunks_skipped,
                "failures": failures,
            }
        finally:
            archive.close()

    def extract_pending_all(self, limit: int = 1) -> Dict[str, Any]:
        """Process a bounded global queue across every enabled discovered project."""
        remaining = max(1, min(limit, 100))
        total = {
            "projects": 0,
            "conversations": 0,
            "proposals": 0,
            "chunks": 0,
            "chunks_skipped": 0,
            "failures": 0,
        }
        projects = [
            project
            for project in self.service.projects()
            if project["sync_enabled"]
            and project["pending_count"]
            and project["canonical_root"]
            and Path(project["canonical_root"]).exists()
        ]
        projects.sort(
            key=lambda project: (
                str(project.get("latest_conversation_at") or ""),
                str(project["project_id"]),
            )
        )
        for project in projects:
            if remaining <= 0:
                break
            result = self.extract_pending(Path(project["canonical_root"]), remaining)
            attempted = int(result["conversations"]) + int(result["failures"])
            if attempted:
                total["projects"] += 1
            for key in ("conversations", "proposals", "chunks", "chunks_skipped", "failures"):
                total[key] += int(result[key])
            remaining -= max(1, attempted)
        return total

    @staticmethod
    def _chunks(
        conversation: Dict[str, Any], max_characters: int = 80_000
    ) -> List[Dict[str, Any]]:
        chunks: List[Dict[str, Any]] = []
        current: List[Dict[str, Any]] = []
        current_size = 0
        for message in conversation["messages"]:
            text = str(message["text"])
            pieces = [
                text[index : index + max_characters]
                for index in range(0, max(1, len(text)), max_characters)
            ] or [""]
            for piece in pieces:
                item = dict(message)
                item["text"] = piece
                size = len(piece) + 200
                if current and current_size + size > max_characters:
                    chunk = dict(conversation)
                    chunk["messages"] = current
                    chunks.append(chunk)
                    current = []
                    current_size = 0
                current.append(item)
                current_size += size
        if current:
            chunk = dict(conversation)
            chunk["messages"] = current
            chunks.append(chunk)
        return chunks

    @staticmethod
    def _chunk_sha256(conversation: Dict[str, Any]) -> str:
        payload = [
            {
                "sequence": message["sequence"],
                "role": message["role"],
                "text": message["text"],
            }
            for message in conversation["messages"]
        ]
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def _extract_one(
        self, project_path: Path, conversation: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        transcript = {
            "conversation_id": conversation["conversation_id"],
            "title": conversation["title"],
            "project_root": conversation["project_root"],
            "messages": [
                {
                    "sequence": message["sequence"],
                    "role": message["role"],
                    "text": message["text"],
                }
                for message in conversation["messages"]
            ],
        }
        with tempfile.TemporaryDirectory(prefix="qmemory-insight-") as temporary_value:
            temporary = Path(temporary_value)
            schema_path = temporary / "schema.json"
            result_path = temporary / "result.json"
            schema_path.write_text(
                json.dumps(OUTPUT_SCHEMA, ensure_ascii=False), encoding="utf-8"
            )
            command = [
                str(self.codex),
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(result_path),
                "-",
            ]
            payload = PROMPT + "\n\nCONVERSATION JSON:\n" + json.dumps(
                transcript, ensure_ascii=False
            )
            result = run_text(
                command,
                input=payload,
                stderr=subprocess.STDOUT,
                timeout=300,
                cwd=str(project_path),
            )
            if result.returncode != 0 or not result_path.exists():
                raise RuntimeError(result.stdout.strip() or "Codex insight extraction failed")
            parsed = json.loads(result_path.read_text(encoding="utf-8"))
            return list(parsed.get("insights") or [])
