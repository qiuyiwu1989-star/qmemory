from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional

from qmemory.config import Settings
from qmemory.project import canonical_remote, identify_project
from qmemory.service import MemoryService
from qmemory.sources import CodexConversationArchive


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=path,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def _committed_repo(path: Path, remote: Optional[str] = None) -> None:
    path.mkdir()
    _git(path, "init")
    _git(path, "config", "user.email", "qmemory@example.invalid")
    _git(path, "config", "user.name", "QMemory Test")
    (path / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "fixture")
    if remote:
        _git(path, "remote", "add", "origin", remote)


def _rollout(
    path: Path,
    conversation_id: str,
    cwd: Optional[Path],
    *,
    explicit_project: Optional[Path] = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata: dict[str, object] = {"id": conversation_id}
    if cwd is not None:
        metadata["cwd"] = str(cwd)
    if explicit_project is not None:
        metadata["project_path"] = str(explicit_project)
    events = [
        {"type": "session_meta", "payload": metadata},
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "Map this task correctly."},
        },
        {
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "Evidence stays immutable."},
        },
    ]
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )


def test_canonical_remote_normalizes_transport_and_case() -> None:
    expected = "github.com/example/qmemory"

    assert canonical_remote("git@GitHub.com:Example/QMemory.git") == expected
    assert canonical_remote("https://github.com/example/qmemory.git/") == expected


def test_linked_worktree_uses_main_checkout_as_canonical_local_identity(
    tmp_path: Path,
) -> None:
    main = tmp_path / "main"
    worktree = tmp_path / "feature"
    _committed_repo(main)
    _git(main, "worktree", "add", "-b", "feature", str(worktree))

    main_identity = identify_project(main)
    worktree_identity = identify_project(worktree)

    assert worktree_identity.root == str(worktree.resolve())
    assert worktree_identity.canonical_root == str(main.resolve())
    assert worktree_identity.project_id == main_identity.project_id


def test_git_remote_wins_over_checkout_path_and_groups_clones(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    remote = "git@github.com:Example/QMemory.git"
    _committed_repo(first, remote)
    _committed_repo(second, "https://github.com/example/qmemory.git")

    first_identity = identify_project(first)
    second_identity = identify_project(second)

    assert first_identity.project_id == "git:github.com/example/qmemory"
    assert second_identity.project_id == first_identity.project_id
    assert first_identity.root != second_identity.root


def test_explicit_metadata_project_beats_unrelated_git_cwd(tmp_path: Path) -> None:
    cwd = tmp_path / "documents"
    qmemory = tmp_path / "qmemory"
    _committed_repo(cwd, "https://github.com/example/documents.git")
    _committed_repo(qmemory, "https://github.com/example/qmemory.git")
    codex_home = tmp_path / "codex"
    conversation_id = "019ff1b1-27bf-7390-9fe3-536e0ecc0f91"
    _rollout(
        codex_home / "sessions" / ("rollout-2026-08-01T10-00-00-" + conversation_id + ".jsonl"),
        conversation_id,
        cwd,
        explicit_project=qmemory,
    )
    service = MemoryService(tmp_path / "home", device_id="explicit-metadata-test")

    service.codex_sync(codex_home)

    rows = service.conversations(qmemory)
    assert [row["conversation_id"] for row in rows] == [conversation_id]
    assert service.conversations(cwd) == []
    assert rows[0]["identity_source"] == "explicit"


def test_projection_binding_repairs_documents_attribution_and_survives_recompute(
    tmp_path: Path,
) -> None:
    documents = tmp_path / "Documents"
    qmemory = documents / "qmemory"
    qmemory.mkdir(parents=True)
    codex_home = tmp_path / "codex"
    conversation_id = "019ff1b1-27bf-7390-9fe3-536e0ecc0f92"
    source = codex_home / "sessions" / (
        "rollout-2026-08-01T10-00-00-" + conversation_id + ".jsonl"
    )
    _rollout(source, conversation_id, documents)
    source_before = source.read_bytes()
    service = MemoryService(tmp_path / "home", device_id="binding-test")
    service.codex_sync(codex_home)
    settings = Settings(tmp_path / "home")
    archive = CodexConversationArchive(settings)
    mirror_before = Path(
        archive.conversation(conversation_id)["mirror_path"]
    ).read_bytes()

    result = archive.bind_conversation_project(conversation_id, qmemory)
    recomputed = archive.recompute_project_mappings()
    bound = archive.conversation(conversation_id)
    archive.close()

    assert result["source_evidence_changed"] is False
    assert recomputed == {"examined": 1, "changed": 0}
    assert bound["project_id"] == identify_project(qmemory).project_id
    assert bound["project_root"] == str(qmemory.resolve())
    assert bound["identity_source"] == "explicit"
    assert source.read_bytes() == source_before
    archive = CodexConversationArchive(settings)
    assert Path(archive.conversation(conversation_id)["mirror_path"]).read_bytes() == mirror_before
    archive.close()


def test_explicit_binding_survives_incremental_resync(tmp_path: Path) -> None:
    documents = tmp_path / "Documents"
    qmemory = documents / "qmemory"
    qmemory.mkdir(parents=True)
    codex_home = tmp_path / "codex"
    conversation_id = "019ff1b1-27bf-7390-9fe3-536e0ecc0f93"
    source = codex_home / "sessions" / (
        "rollout-2026-08-01T10-00-00-" + conversation_id + ".jsonl"
    )
    _rollout(source, conversation_id, documents)
    service = MemoryService(tmp_path / "home", device_id="binding-resync-test")
    service.codex_sync(codex_home)
    archive = CodexConversationArchive(Settings(tmp_path / "home"))
    archive.bind_conversation_project(conversation_id, qmemory)
    archive.close()
    with source.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "message": "A later update."},
                }
            )
            + "\n"
        )

    service.codex_sync(codex_home)

    rows = service.conversations(qmemory)
    assert len(rows) == 1
    assert rows[0]["conversation_id"] == conversation_id
    assert rows[0]["identity_source"] == "explicit"


def test_manual_canonical_root_stays_locked_when_new_worktree_is_seen(
    tmp_path: Path,
) -> None:
    first = tmp_path / "a"
    second = tmp_path / "much-longer-checkout"
    for path in (first, second):
        _committed_repo(path, "https://github.com/example/shared.git")
    service = MemoryService(tmp_path / "home", device_id="canonical-lock-test")
    project = service.register_project(first)
    service.update_project(project["project_id"], canonical_root=str(first))

    service.register_project(second)

    current = service.projects()[0]
    assert current["canonical_root"] == str(first)
    assert current["canonical_root_locked"] is True
