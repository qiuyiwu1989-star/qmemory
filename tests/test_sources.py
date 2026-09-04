from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from qmemory.service import MemoryService


def _line(kind: str, payload: dict, timestamp: str) -> str:
    return json.dumps({"type": kind, "timestamp": timestamp, "payload": payload}) + "\n"


def _rollout(path: Path, conversation_id: str, project: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _line(
            "session_meta",
            {
                "id": conversation_id,
                "cwd": str(project),
                "timestamp": "2026-08-01T10:00:00Z",
            },
            "2026-08-01T10:00:00Z",
        )
        + _line(
            "event_msg",
            {"type": "user_message", "message": "Use SQLite as a local projection"},
            "2026-08-01T10:01:00Z",
        )
        + _line(
            "event_msg",
            {"type": "agent_message", "message": "I will preserve the event log."},
            "2026-08-01T10:02:00Z",
        )
        + _line(
            "response_item",
            {"type": "function_call_output", "output": "secret internal tool output"},
            "2026-08-01T10:02:30Z",
        ),
        encoding="utf-8",
    )


def _claude_session(path: Path, conversation_id: str, project: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "type": "user",
            "sessionId": conversation_id,
            "cwd": str(project),
            "timestamp": "2026-08-13T10:00:00Z",
            "isSidechain": False,
            "message": {"role": "user", "content": "Archive Claude Code conversations too."},
        },
        {
            "type": "ai-title",
            "sessionId": conversation_id,
            "aiTitle": "Claude Code archive design",
        },
        {
            "type": "assistant",
            "sessionId": conversation_id,
            "cwd": str(project),
            "timestamp": "2026-08-13T10:01:00Z",
            "isSidechain": False,
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "private reasoning"},
                    {"type": "text", "text": "The source archive remains local and lossless."},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "secret"}},
                ],
            },
        },
        {
            "type": "user",
            "sessionId": conversation_id,
            "cwd": str(project),
            "timestamp": "2026-08-13T10:02:00Z",
            "isSidechain": False,
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "content": "password=hidden-in-raw"}],
            },
        },
        {
            "type": "user",
            "sessionId": conversation_id,
            "cwd": str(project),
            "timestamp": "2026-08-13T10:03:00Z",
            "isSidechain": False,
            "message": {"role": "user", "content": "<command-name>/model</command-name>"},
        },
    ]
    path.write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=path, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def test_project_library_groups_same_git_remote_and_keeps_local_folders_separate(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    local = tmp_path / "local"
    for path in (first, second, local):
        path.mkdir()
    for path in (first, second):
        _git(path, "init")
        _git(path, "remote", "add", "origin", "https://github.com/example/shared.git")
    codex_home = tmp_path / "codex"
    _rollout(codex_home / "sessions" / "rollout-first.jsonl", "first-session", first)
    _rollout(codex_home / "sessions" / "rollout-second.jsonl", "second-session", second)
    _rollout(codex_home / "sessions" / "rollout-local.jsonl", "local-session", local)
    service = MemoryService(tmp_path / "home", device_id="project-library-test")

    service.codex_sync(codex_home)
    projects = service.projects()

    assert len(projects) == 2
    git_project = next(project for project in projects if project["remote"])
    local_project = next(project for project in projects if not project["remote"])
    assert git_project["conversation_count"] == 2
    assert set(git_project["roots"]) == {str(first), str(second)}
    assert local_project["roots"] == [str(local)]


def test_project_library_preferences_control_visibility_and_canonical_root(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for path in (first, second):
        path.mkdir()
        _git(path, "init")
        _git(path, "remote", "add", "origin", "git@github.com:example/shared.git")
    codex_home = tmp_path / "codex"
    _rollout(codex_home / "sessions" / "rollout-first.jsonl", "first-session", first)
    _rollout(codex_home / "sessions" / "rollout-second.jsonl", "second-session", second)
    service = MemoryService(tmp_path / "home", device_id="project-preferences-test")
    service.codex_sync(codex_home)
    project = service.projects()[0]

    updated = service.update_project(
        project["project_id"], display_name="共同项目", sync_enabled=False,
        canonical_root=str(second), ignored=True,
    )

    assert updated["display_name"] == "共同项目"
    assert updated["sync_enabled"] is False and updated["ignored"] is True
    assert updated["canonical_root"] == str(second)
    assert service.projects() == []
    assert service.projects(include_ignored=True)[0]["display_name"] == "共同项目"


def test_codex_conversations_are_archived_losslessly_and_indexed(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    codex_home = tmp_path / "codex"
    source = codex_home / "sessions" / "2026" / "08" / "01" / (
        "rollout-2026-08-01T10-00-00-019ff1b1-27bf-7390-9fe3-536e0ecc0f82.jsonl"
    )
    _rollout(source, "019ff1b1-27bf-7390-9fe3-536e0ecc0f82", project)
    service = MemoryService(tmp_path / "home", device_id="source-test")

    preview = service.codex_preview(codex_home)
    result = service.codex_sync(codex_home)

    assert preview["found"] == 1 and preview["changed"] == 1
    assert result["imported"] == 1 and result["messages_indexed"] == 2
    conversations = service.conversations(project)
    assert len(conversations) == 1
    archived = Path(conversations[0]["mirror_path"])
    assert archived.read_bytes() == source.read_bytes()
    assert archived.stat().st_mode & 0o077 == 0
    detail = service.conversation(project, conversations[0]["conversation_id"])
    assert [message["role"] for message in detail["messages"]] == ["user", "assistant"]
    assert "secret internal tool output" not in json.dumps(detail)


def test_sync_has_run_id_and_redacts_title_and_fts_without_changing_evidence(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    codex_home = tmp_path / "codex"
    source = codex_home / "sessions" / "rollout-redaction.jsonl"
    token = "ghp_" + "S" * 30
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        _line(
            "session_meta",
            {"id": "redaction-session", "cwd": str(project)},
            "2026-08-01T10:00:00Z",
        )
        + _line(
            "event_msg",
            {"type": "user_message", "message": "credential %s" % token},
            "2026-08-01T10:01:00Z",
        ),
        encoding="utf-8",
    )
    service = MemoryService(tmp_path / "home", device_id="source-redaction-test")

    result = service.codex_sync(codex_home)

    assert result["sync_run_id"].startswith("sync_")
    assert result["failures"] == 0
    assert result["by_source"]["codex"]["imported"] == 1
    conversation = service.conversations(project)[0]
    assert token not in conversation["title"]
    assert "[REDACTED:github-token]" in conversation["title"]
    assert token in Path(conversation["mirror_path"]).read_text(encoding="utf-8")
    with sqlite3.connect(service.settings.source_database_path) as connection:
        assert token in connection.execute(
            "SELECT text FROM conversation_messages LIMIT 1"
        ).fetchone()[0]
        indexed = connection.execute("SELECT text FROM conversation_fts LIMIT 1").fetchone()[0]
        assert token not in indexed
        assert "[REDACTED:github-token]" in indexed


def test_codex_sync_appends_and_does_not_duplicate(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    codex_home = tmp_path / "codex"
    source = codex_home / "sessions" / "rollout-sample.jsonl"
    _rollout(source, "conversation-append", project)
    service = MemoryService(tmp_path / "home", device_id="append-test")
    service.codex_sync(codex_home)
    first_size = source.stat().st_size

    with source.open("a", encoding="utf-8") as handle:
        handle.write(
            _line(
                "event_msg",
                {"type": "user_message", "message": "Now add a searchable archive"},
                "2026-08-01T10:03:00Z",
            )
        )
    result = service.codex_sync(codex_home)
    unchanged = service.codex_sync(codex_home)

    assert result["imported"] == 1
    assert result["bytes_copied"] == source.stat().st_size - first_size
    assert unchanged["imported"] == 0 and unchanged["unchanged"] == 1
    assert len(service.conversation_search(project, "searchable archive")) == 1


def test_codex_rollouts_with_shared_session_metadata_stay_distinct_below_three_messages(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    codex_home = tmp_path / "codex"
    first = codex_home / "sessions" / (
        "rollout-2026-08-01T10-00-00-019ff1b1-27bf-7390-9fe3-536e0ecc0f82.jsonl"
    )
    second = codex_home / "sessions" / (
        "rollout-2026-08-01T11-00-00-019ff1b1-27bf-7390-9fe3-536e0ecc0f83.jsonl"
    )
    _rollout(first, "shared-root-session", project)
    _rollout(second, "shared-root-session", project)
    service = MemoryService(tmp_path / "home", device_id="shared-session-test")

    result = service.codex_sync(codex_home)
    rows = service.conversations(project)
    raw_rows = service.conversations(project, include_duplicates=True)

    assert result["found"] == 2 and result["imported"] == 2
    assert len(rows) == 2
    assert len(raw_rows) == 2
    assert len({row["conversation_id"] for row in raw_rows}) == 2
    assert len({row["mirror_path"] for row in raw_rows}) == 2
    assert all(Path(row["mirror_path"]).exists() for row in raw_rows)


def test_cross_agent_imported_copy_is_merged_in_product_view_but_preserved_raw(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    codex_home = tmp_path / "codex"
    claude_home = tmp_path / "claude"
    codex_source = codex_home / "sessions" / "rollout-imported.jsonl"
    shared_user = "Build the same project page from this presentation."
    shared_agent = "I inspected the slides and created the page."
    codex_source.parent.mkdir(parents=True, exist_ok=True)
    codex_source.write_text(
        _line("session_meta", {"id": "codex-copy", "cwd": str(project)}, "2026-08-02T10:00:00Z")
        + _line("event_msg", {"type": "user_message", "message": shared_user}, "2026-08-02T10:01:00Z")
        + _line("event_msg", {"type": "agent_message", "message": shared_agent}, "2026-08-02T10:02:00Z")
        + _line("event_msg", {"type": "agent_message", "message": "A durable project decision was recorded."}, "2026-08-02T10:03:00Z")
        + _line("event_msg", {"type": "agent_message", "message": "[external_agent_tool_result] noisy listing"}, "2026-08-02T10:04:00Z")
        + _line("event_msg", {"type": "agent_message", "message": "<EXTERNAL SESSION IMPORTED>"}, "2026-08-02T10:05:00Z"),
        encoding="utf-8",
    )
    claude_source = claude_home / "projects" / "-project" / "claude-copy.jsonl"
    claude_source.parent.mkdir(parents=True, exist_ok=True)
    claude_events = [
        {"type": "user", "sessionId": "claude-copy", "cwd": str(project), "timestamp": "2026-08-01T10:00:00Z", "message": {"role": "user", "content": shared_user}},
        {"type": "assistant", "sessionId": "claude-copy", "cwd": str(project), "timestamp": "2026-08-01T10:01:00Z", "message": {"role": "assistant", "content": [{"type": "text", "text": shared_agent}]}},
        {"type": "assistant", "sessionId": "claude-copy", "cwd": str(project), "timestamp": "2026-08-01T10:02:00Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "A durable project decision was recorded."}]}},
    ]
    claude_source.write_text(
        "".join(json.dumps(event) + "\n" for event in claude_events), encoding="utf-8"
    )
    service = MemoryService(tmp_path / "home", device_id="duplicate-view-test")

    service.conversation_sources_sync(codex_home, claude_home)
    product_rows = service.conversations(project)
    raw_rows = service.conversations(project, include_duplicates=True)

    assert len(raw_rows) == 2 and len(product_rows) == 1
    assert set(product_rows[0]["sources"]) == {"codex", "claude-code"}
    assert product_rows[0]["source_copy_count"] == 2
    detail = service.conversation(project, product_rows[0]["conversation_id"])
    assert all("external_agent_tool" not in message["text"] for message in detail["messages"])
    assert detail["raw_message_count"] >= detail["message_count"]

    tasks = service.tasks(project)
    assert len(tasks) == 1
    assert tasks[0]["task_id"] == product_rows[0]["conversation_id"]
    assert set(tasks[0]["sources"]) == {"codex", "claude-code"}
    assert tasks[0]["source_copy_count"] == 2
    assert tasks[0]["subagent_count"] == 0
    assert tasks[0]["primary_coverage"] == 1.0
    assert tasks[0]["unique_readable_message_count"] == 3
    assert tasks[0]["has_content_gap"] is False
    assert "跨 Agent" in tasks[0]["merge_reason"]
    task = service.task(project, tasks[0]["task_id"])
    assert len(task["source_copies"]) == 2
    assert task["primary_conversation"]["conversation_id"] == task["primary_conversation_id"]
    assert all("merge_reason" in copy for copy in task["source_copies"])


def test_task_primary_prefers_richer_copy_and_reports_cross_source_content_gap(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    codex_home = tmp_path / "codex"
    claude_home = tmp_path / "claude"
    shared = ["Build the complete archive.", "Shared answer one.", "Shared answer two."]
    codex = codex_home / "sessions" / "rollout-short.jsonl"
    codex.parent.mkdir(parents=True, exist_ok=True)
    codex.write_text(
        _line("session_meta", {"id": "short", "cwd": str(project)}, "2026-08-01T10:00:00Z")
        + "".join(
            _line(
                "event_msg",
                {"type": "user_message" if index == 0 else "agent_message", "message": text},
                f"2026-08-01T10:0{index + 1}:00Z",
            )
            for index, text in enumerate(shared)
        )
        + _line(
            "event_msg", {"type": "agent_message", "message": "Codex-only evidence."},
            "2026-08-01T10:04:00Z",
        ),
        encoding="utf-8",
    )
    claude = claude_home / "projects" / "-project" / "richer.jsonl"
    claude.parent.mkdir(parents=True, exist_ok=True)
    claude_messages = shared + ["Claude-only evidence A.", "Claude-only evidence B."]
    claude.write_text(
        "".join(
            json.dumps(
                {
                    "type": "user" if index == 0 else "assistant",
                    "sessionId": "richer",
                    "cwd": str(project),
                    "timestamp": f"2026-08-01T11:{index:02d}:00Z",
                    "message": {
                        "role": "user" if index == 0 else "assistant",
                        "content": text,
                    },
                }
            ) + "\n"
            for index, text in enumerate(claude_messages)
        ),
        encoding="utf-8",
    )
    service = MemoryService(tmp_path / "home", device_id="coverage-test")

    service.conversation_sources_sync(codex_home, claude_home)
    task = service.tasks(project)[0]

    assert task["primary_conversation_id"] == "claude:richer"
    assert task["unique_readable_message_count"] == 6
    assert task["primary_coverage"] == pytest.approx(5 / 6, abs=0.0001)
    assert task["has_content_gap"] is True
    assert service.task(project, task["task_id"])["primary_conversation"][
        "conversation_id"
    ] == "claude:richer"


def test_same_source_top_level_copy_requires_directional_95_percent_coverage(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    codex_home = tmp_path / "codex"

    def write_rollout(path: Path, conversation_id: str, values: list[str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            _line(
                "session_meta", {"id": conversation_id, "cwd": str(project)},
                "2026-08-01T10:00:00Z",
            )
            + "".join(
                _line(
                    "event_msg",
                    {
                        "type": "user_message" if index == 0 else "agent_message",
                        "message": text,
                    },
                    f"2026-08-01T10:{index % 60:02d}:00Z",
                )
                for index, text in enumerate(values)
            ),
            encoding="utf-8",
        )

    shared = ["Same root request."] + [f"Shared message {index}" for index in range(1, 100)]
    long_id = "019ff1b1-27bf-7390-9fe3-536e0ecc0f84"
    covered_id = "019ff1b1-27bf-7390-9fe3-536e0ecc0f85"
    low_id = "019ff1b1-27bf-7390-9fe3-536e0ecc0f86"
    write_rollout(
        codex_home / "sessions" / f"rollout-2026-08-01T10-00-00-{long_id}.jsonl",
        long_id, shared,
    )
    write_rollout(
        codex_home / "sessions" / f"rollout-2026-08-01T10-01-00-{covered_id}.jsonl",
        covered_id,
        shared[:-1] + ["Different final message"],
    )
    write_rollout(
        codex_home / "sessions" / f"rollout-2026-08-01T10-02-00-{low_id}.jsonl",
        low_id,
        shared[:80] + [f"Unrelated continuation {index}" for index in range(20)],
    )
    service = MemoryService(tmp_path / "home", device_id="same-source-coverage-test")

    service.codex_sync(codex_home)
    tasks = service.tasks(project)

    assert len(tasks) == 2
    merged = next(task for task in tasks if task["source_copy_count"] == 2)
    separate = next(task for task in tasks if task["source_copy_count"] == 1)
    assert merged["primary_conversation_id"] in {long_id, covered_id}
    assert merged["primary_coverage"] == pytest.approx(100 / 101, abs=0.0001)
    assert merged["has_content_gap"] is True
    assert separate["primary_conversation_id"] == low_id


def test_task_view_folds_codex_subagent_into_root_and_searches_branch(
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
        "rollout-2026-08-01T10-01-00-" + child_id + ".jsonl"
    )
    _rollout(parent, parent_id, project)
    child.parent.mkdir(parents=True, exist_ok=True)
    child.write_text(
        _line(
            "session_meta",
            {
                "id": child_id,
                "cwd": str(project),
                "timestamp": "2026-08-01T10:01:00Z",
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": parent_id,
                            "agent_path": "research",
                            "agent_nickname": "Scout",
                        }
                    }
                },
            },
            "2026-08-01T10:01:00Z",
        )
        + _line(
            "event_msg",
            {"type": "user_message", "message": "Investigate the branch-only keyword nebula."},
            "2026-08-01T10:02:00Z",
        )
        + _line(
            "event_msg",
            {"type": "agent_message", "message": "Branch evidence is ready."},
            "2026-08-01T10:03:00Z",
        ),
        encoding="utf-8",
    )
    service = MemoryService(tmp_path / "home", device_id="task-subagent-test")

    service.codex_sync(codex_home)
    tasks = service.tasks(project)

    assert len(tasks) == 1
    assert tasks[0]["task_id"] == parent_id
    assert tasks[0]["source_copy_count"] == 1
    assert tasks[0]["subagent_count"] == 1
    assert tasks[0]["raw_message_count"] == 4
    assert tasks[0]["readable_message_count"] == 4
    assert "子 Agent" in tasks[0]["merge_reason"]
    assert service.tasks(project, query="nebula")[0]["task_id"] == parent_id
    detail = service.task(project, parent_id)
    assert detail["subagents"][0]["conversation_id"] == child_id
    assert detail["subagents"][0]["agent_nickname"] == "Scout"
    assert "parent_thread_id" in detail["subagents"][0]["merge_reason"]
    assert service.projects()[0]["conversation_count"] == 1
    service.initialize()
    assert service.totals()["tasks"] == 1


def test_task_detail_rejects_cross_project_task_access(tmp_path: Path) -> None:
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    codex_home = tmp_path / "codex"
    _rollout(codex_home / "sessions" / "rollout-task.jsonl", "task", project)
    service = MemoryService(tmp_path / "home", device_id="task-guard-test")
    service.codex_sync(codex_home)
    task_id = service.tasks(project)[0]["task_id"]

    with pytest.raises(ValueError, match="cross-project"):
        service.task(other, task_id)


def test_totals_distinguish_memories_tasks_and_raw_conversation_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    codex_home = tmp_path / "codex"
    _rollout(codex_home / "sessions" / "rollout-totals.jsonl", "totals-session", project)
    service = MemoryService(tmp_path / "home", device_id="totals-test")
    service.codex_sync(codex_home)
    service.remember(project, "decision", "Keep project and memory counts separate.", confirmed=True)

    totals = service.totals()

    assert totals["projects"] == 1
    assert totals["tasks"] == 1 and totals["conversation_files"] == 1
    assert totals["memories"]["active"] == 1


def test_conversation_access_is_project_guarded(tmp_path: Path) -> None:
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    codex_home = tmp_path / "codex"
    source = codex_home / "sessions" / "rollout-guard.jsonl"
    _rollout(source, "conversation-guard", project)
    service = MemoryService(tmp_path / "home", device_id="guard-test")
    service.codex_sync(codex_home)
    conversation_id = service.conversations(project)[0]["conversation_id"]

    with pytest.raises(ValueError, match="Cross-project"):
        service.conversation(other, conversation_id)


def test_claude_code_is_archived_losslessly_without_internal_projection(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    claude_home = tmp_path / "claude"
    source = claude_home / "projects" / "-project" / "claude-session.jsonl"
    _claude_session(source, "claude-session", project)
    subagent = (
        claude_home / "projects" / "-project" / "claude-session" / "subagents" / "agent-1.jsonl"
    )
    _claude_session(subagent, "agent-1", project)
    original = source.read_bytes()
    service = MemoryService(tmp_path / "qmemory", device_id="claude-test")

    preview = service.conversation_sources_preview(tmp_path / "codex", claude_home)
    assert preview["by_source"]["claude-code"]["found"] == 1
    result = service.conversation_sources_sync(tmp_path / "codex", claude_home)
    assert result["by_source"]["claude-code"]["imported"] == 1

    rows = service.conversations(project)
    assert len(rows) == 1
    assert rows[0]["source_kind"] == "claude-code"
    assert Path(rows[0]["mirror_path"]).read_bytes() == original
    detail = service.conversation(project, "claude:claude-session", redact=False)
    assert detail["title"] == "Claude Code archive design"
    assert [message["role"] for message in detail["messages"]] == ["user", "assistant"]
    projected = "\n".join(message["text"] for message in detail["messages"])
    assert "private reasoning" not in projected
    assert "hidden-in-raw" not in projected
    assert "command-name" not in projected
    assert "source archive remains local" in projected
    assert len(service.conversations(project, "local and lossless")) == 1
    assert service.insight_stats(project) == {
        "total": 1,
        "extracted": 0,
        "pending": 1,
        "failed": 0,
        "messages": 2,
    }
