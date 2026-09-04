from __future__ import annotations

from pathlib import Path

from qmemory.insights import CodexInsightExtractor
from qmemory.service import MemoryService

from test_sources import _line, _rollout


class StubExtractor(CodexInsightExtractor):
    def _extract_one(self, project_path, conversation):
        return [
            {
                "memory_type": "decision",
                "holder": "user",
                "subject": "archive architecture",
                "statement": "Archive original conversations before deriving insights.",
                "evidence_sequence": 1,
            }
        ]


def test_insights_are_proposed_and_traceable_to_source_message(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    codex_home = tmp_path / "codex"
    source = codex_home / "sessions" / "rollout-test.jsonl"
    _rollout(source, "conversation-insight", project)
    service = MemoryService(tmp_path / "qmemory", device_id="insight-test")
    service.codex_sync(codex_home)

    result = StubExtractor(service, codex=Path("/bin/true")).extract_pending(project)

    assert result == {
        "conversations": 1,
        "proposals": 1,
        "chunks": 1,
        "chunks_skipped": 0,
        "failures": 0,
    }
    memories = service.browse(project, status="proposed")
    assert len(memories) == 1
    assert memories[0]["holder"] == "user"
    conversation_id = service.conversations(project)[0]["conversation_id"]
    assert memories[0]["source_ref"] == "codex://%s#message-1" % conversation_id
    assert StubExtractor(service, codex=Path("/bin/true")).extract_pending(project) == {
        "conversations": 0,
        "proposals": 0,
        "chunks": 0,
        "chunks_skipped": 0,
        "failures": 0,
    }


def test_invalid_evidence_reference_is_not_saved(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    codex_home = tmp_path / "codex"
    source = codex_home / "sessions" / "rollout-test.jsonl"
    _rollout(source, "conversation-invalid", project)
    service = MemoryService(tmp_path / "qmemory", device_id="invalid-test")
    service.codex_sync(codex_home)

    class InvalidExtractor(StubExtractor):
        def _extract_one(self, project_path, conversation):
            result = super()._extract_one(project_path, conversation)
            result[0]["evidence_sequence"] = 999
            return result

    result = InvalidExtractor(service, codex=Path("/bin/true")).extract_pending(project)
    assert result == {
        "conversations": 1,
        "proposals": 0,
        "chunks": 1,
        "chunks_skipped": 0,
        "failures": 0,
    }
    assert service.browse(project, status="proposed") == []


def test_failed_chunk_is_recorded_and_retried(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    codex_home = tmp_path / "codex"
    source = codex_home / "sessions" / "rollout-test.jsonl"
    _rollout(source, "conversation-retry", project)
    service = MemoryService(tmp_path / "qmemory", device_id="retry-test")
    service.codex_sync(codex_home)

    class FailingExtractor(StubExtractor):
        def _extract_one(self, project_path, conversation):
            raise RuntimeError("temporary model failure")

    failed = FailingExtractor(service, codex=Path("/bin/true")).extract_pending(project)
    assert failed["failures"] == 1 and failed["conversations"] == 0
    assert service.insight_stats(project)["failed"] == 1

    retried = StubExtractor(service, codex=Path("/bin/true")).extract_pending(project)
    assert retried["conversations"] == 1 and retried["proposals"] == 1
    assert service.insight_stats(project)["failed"] == 0


def test_long_conversations_are_chunked_without_losing_message_provenance() -> None:
    conversation = {
        "conversation_id": "long",
        "messages": [
            {"sequence": 1, "role": "user", "text": "a" * 70},
            {"sequence": 2, "role": "assistant", "text": "b" * 70},
        ],
    }
    chunks = CodexInsightExtractor._chunks(conversation, max_characters=100)
    assert len(chunks) >= 2
    assert {message["sequence"] for chunk in chunks for message in chunk["messages"]} == {
        1,
        2,
    }


def test_global_insight_queue_processes_enabled_projects_only(tmp_path: Path) -> None:
    first = tmp_path / "first"
    paused = tmp_path / "paused"
    first.mkdir()
    paused.mkdir()
    codex_home = tmp_path / "codex"
    _rollout(codex_home / "sessions" / "rollout-first.jsonl", "first-global", first)
    _rollout(codex_home / "sessions" / "rollout-paused.jsonl", "paused-global", paused)
    service = MemoryService(tmp_path / "qmemory", device_id="global-insight-test")
    service.codex_sync(codex_home)
    paused_project = next(
        project for project in service.projects() if project["canonical_root"] == str(paused)
    )
    service.update_project(paused_project["project_id"], sync_enabled=False)

    result = StubExtractor(service, codex=Path("/bin/true")).extract_pending_all(10)

    assert result["projects"] == 1 and result["conversations"] == 1
    assert len(service.browse(first, status="proposed")) == 1
    assert service.browse(paused, status="proposed") == []
    assert service.insight_stats(paused)["pending"] == 1


def test_reset_insight_pipeline_requeues_only_canonical_conversations(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    codex_home = tmp_path / "codex"
    source = codex_home / "sessions" / "rollout-reset.jsonl"
    _rollout(source, "conversation-reset", project)
    service = MemoryService(tmp_path / "qmemory", device_id="reset-pipeline-test")
    service.codex_sync(codex_home)
    StubExtractor(service, codex=Path("/bin/true")).extract_pending(project)
    assert service.insight_stats(project)["pending"] == 0

    reset = service.reset_insight_pipeline()

    assert reset["queued"] == 1
    assert service.insight_stats(project)["pending"] == 1


def test_codex_subagents_are_archived_but_never_queued_as_independent_insights(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    codex_home = tmp_path / "codex"
    parent_id = "019ff1b1-27bf-7390-9fe3-536e0ecc0f82"
    child_id = "019ff1b1-27bf-7390-9fe3-536e0ecc0f83"
    parent = codex_home / "sessions" / (
        "rollout-2026-08-01T10-00-00-" + parent_id + ".jsonl"
    )
    child = codex_home / "sessions" / (
        "rollout-2026-08-01T10-03-00-" + child_id + ".jsonl"
    )
    _rollout(parent, parent_id, project)
    child.parent.mkdir(parents=True, exist_ok=True)
    child.write_text(
        _line(
            "session_meta",
            {
                "id": child_id,
                "cwd": str(project),
                "source": {
                    "subagent": {
                        "thread_spawn": {
                                "parent_thread_id": parent_id,
                            "agent_path": "design-review",
                            "agent_nickname": "reviewer",
                        }
                    }
                },
            },
            "2026-08-01T10:03:00Z",
        )
        + _line(
            "event_msg",
            {"type": "user_message", "message": "Review the parent implementation."},
            "2026-08-01T10:04:00Z",
        )
        + _line(
            "event_msg",
            {"type": "agent_message", "message": "The review is complete."},
            "2026-08-01T10:05:00Z",
        ),
        encoding="utf-8",
    )
    service = MemoryService(tmp_path / "qmemory", device_id="subagent-queue-test")

    service.codex_sync(codex_home)
    rows = service.conversations(project, include_duplicates=True)
    child_row = next(row for row in rows if row["conversation_id"] == child_id)
    reset = service.reset_insight_pipeline()

    assert child_row["parent_conversation_id"] == parent_id
    assert child_row["agent_path"] == "design-review"
    assert reset == {"queued": 1, "duplicates_skipped": 0, "subagents_skipped": 1}
    assert service.insight_stats(project)["total"] == 1
