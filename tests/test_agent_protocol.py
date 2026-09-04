from __future__ import annotations

from pathlib import Path

from qmemory.agent_protocol import (
    END_MARKER,
    START_MARKER,
    inspect_protocol,
    install_protocol,
    global_claude_path,
)


def test_install_protocol_preserves_existing_global_guidance(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_text("# Existing guidance\n\nKeep this.\n", encoding="utf-8")

    result = install_protocol(path)

    text = path.read_text(encoding="utf-8")
    assert result["state"] == "current"
    assert text.startswith("# Existing guidance\n\nKeep this.")
    assert text.count(START_MARKER) == 1
    assert text.count(END_MARKER) == 1
    assert "project_context" in text
    assert "memory_propose" in text
    assert "session_handoff" in text


def test_install_protocol_updates_managed_block_without_duplication(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_text(
        "before\n\n%s\nold instructions\n%s\n\nafter\n" % (START_MARKER, END_MARKER),
        encoding="utf-8",
    )

    install_protocol(path)
    install_protocol(path)

    text = path.read_text(encoding="utf-8")
    assert text.count(START_MARKER) == 1
    assert text.count(END_MARKER) == 1
    assert "old instructions" not in text
    assert "before" in text and "after" in text
    assert inspect_protocol(path)["state"] == "current"


def test_protocol_archives_both_agents_before_context(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    install_protocol(path)
    text = path.read_text(encoding="utf-8")
    assert "agent_conversations_sync" in text
    assert "Codex and Claude Code" in text
    assert "claude-code" in text
    assert global_claude_path(tmp_path) == tmp_path / "CLAUDE.md"
