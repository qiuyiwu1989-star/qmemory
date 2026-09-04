from __future__ import annotations

import json
from pathlib import Path

import pytest

from qmemory.tencent_memorycore import (
    MemoryCoreBridge,
    MemoryCoreConfig,
    MemoryCoreIdentity,
    TencentMemoryCoreLab,
    validate_memorycore_url,
)


class FakeService:
    def tasks(self, project: Path, limit: int = 10000):
        return [{"task_id": "task-1", "title": "跨 Agent 任务"}]

    def task(self, project: Path, task_id: str):
        return {
            "task_id": task_id,
            "primary_conversation_id": "codex:primary",
            "primary_conversation": {
                "messages": [
                    {"role": "user", "sequence": 1, "text": "保留原始对话"},
                    {"role": "assistant", "sequence": 2, "text": "已归档"},
                ]
            },
            "source_copies": [
                {"conversation_id": "codex:primary", "source_kind": "codex"},
                {"conversation_id": "claude:copy", "source_kind": "claude-code"},
            ],
            "subagents": [],
        }

    def conversation(self, project: Path, conversation_id: str, redact: bool = True):
        return {
            "messages": [
                {"role": "user", "sequence": 4, "text": "第二来源"},
                {"role": "tool", "sequence": 5, "text": "不导出工具轨迹"},
                {"role": "assistant", "sequence": 6, "text": "已合并"},
            ]
        }


def test_export_is_deterministic_and_preserves_provenance(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    lab = TencentMemoryCoreLab(FakeService())  # type: ignore[arg-type]
    first = lab.export_project(project, tmp_path / "one", max_messages=1)
    second = lab.export_project(project, tmp_path / "two", max_messages=1)

    assert first["conversation_count"] == 2
    assert first["batch_count"] == 4
    assert first["message_count"] == 4
    assert first["manifest_sha256"] == second["manifest_sha256"]
    manifest = json.loads((tmp_path / "one" / "manifest.json").read_text())
    assert [batch["sequences"] for batch in manifest["batches"]] == [[1], [2], [4], [6]]
    assert all(batch["message_count"] <= 100 for batch in manifest["batches"])


def test_import_rejects_non_local_endpoint(tmp_path: Path) -> None:
    lab = TencentMemoryCoreLab(FakeService())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="localhost"):
        lab.import_export(
            tmp_path,
            MemoryCoreIdentity("team", "user", "agent"),
            base_url="https://memory.example.com",
        )


def test_production_url_policy_allows_remote_https_only() -> None:
    assert validate_memorycore_url("http://127.0.0.1:8420") == "http://127.0.0.1:8420"
    assert validate_memorycore_url("https://memory.example.com/") == "https://memory.example.com"
    with pytest.raises(ValueError, match="HTTPS"):
        validate_memorycore_url("http://memory.example.com")
    with pytest.raises(ValueError, match="credentials"):
        validate_memorycore_url("https://user:secret@memory.example.com")


def test_config_persists_no_secret(tmp_path: Path) -> None:
    path = tmp_path / "memorycore.json"
    config = MemoryCoreConfig(
        enabled=True,
        base_url="https://memory.example.com",
        api_key_env="MEMORYCORE_GATEWAY_KEY",
    )
    config.save(path)
    saved = path.read_text()
    assert "MEMORYCORE_GATEWAY_KEY" in saved
    assert "secret" not in saved
    assert '"operation_mode":"shadow"' in saved
    assert '"fail_open":true' in saved
    assert '"recall_top_k":5' in saved
    assert MemoryCoreConfig.load(path) == config


def test_config_rejects_active_mode_until_an_explicit_safety_gate_exists(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="only shadow"):
        MemoryCoreConfig(enabled=True, operation_mode="active").save(tmp_path / "memorycore.json")
    with pytest.raises(ValueError, match="between 1 and 100"):
        MemoryCoreConfig(enabled=True, recall_top_k=0).save(tmp_path / "memorycore.json")


class BridgeProject:
    project_id = "project-1"


class BridgeSettings:
    def __init__(self, root: Path) -> None:
        self.state_dir = root


class BridgeService(FakeService):
    def __init__(self, root: Path) -> None:
        self.settings = BridgeSettings(root)
        self.extra = False

    def project(self, project: Path):
        return BridgeProject()

    def projects(self):
        return [
            {
                "canonical_root": str(self.settings.state_dir / "project"),
                "sync_enabled": True,
                "ignored": False,
            },
            {
                "canonical_root": str(self.settings.state_dir / "paused"),
                "sync_enabled": False,
                "ignored": False,
            },
        ]

    def task(self, project: Path, task_id: str):
        result = super().task(project, task_id)
        if self.extra:
            result["primary_conversation"]["messages"].append(
                {"role": "assistant", "sequence": 3, "text": "新增进展"}
            )
        return result


class FakeMemoryCoreClient:
    calls = []

    def __init__(self, config, identity):
        self.identity = identity

    def add_conversation(self, session_id, messages, task_id=None):
        self.calls.append((session_id, list(messages), task_id))
        return {"accepted_ids": ["remote-%d" % (len(self.calls) * 10 + i) for i in range(len(messages))]}

    def query_atomic(self, session_id=None, limit=100):
        return {"items": [{"content": "原子事实", "source_message_ids": ["remote-10"]}]}

    def list_scenarios(self, path_prefix=""):
        return {"items": [{"path": "projects/qmemory"}]}

    def read_core(self):
        return {"content": "稳定内核"}


class FailingSyncClient(FakeMemoryCoreClient):
    def add_conversation(self, session_id, messages, task_id=None):
        raise RuntimeError("test gateway unavailable")


class FailingRecallClient(FakeMemoryCoreClient):
    def query_atomic(self, session_id=None, limit=100):
        raise RuntimeError("test recall unavailable")

    def list_scenarios(self, path_prefix=""):
        raise RuntimeError("test scenarios unavailable")

    def read_core(self):
        raise RuntimeError("test core unavailable")


def test_bridge_is_incremental_and_maps_provenance(tmp_path: Path) -> None:
    FakeMemoryCoreClient.calls = []
    service = BridgeService(tmp_path)
    bridge = MemoryCoreBridge(
        service,  # type: ignore[arg-type]
        MemoryCoreConfig(enabled=True),
        state_path=tmp_path / "bridge.json",
        client_factory=FakeMemoryCoreClient,
    )
    project = tmp_path / "project"
    project.mkdir()

    first = bridge.sync_project(project)
    assert first["sent_messages"] == 4
    assert len(FakeMemoryCoreClient.calls) == 2
    second = bridge.sync_project(project)
    assert second["sent_messages"] == 0
    service.extra = True
    third = bridge.sync_project(project)
    assert third["sent_messages"] == 1
    assert FakeMemoryCoreClient.calls[-1][1] == [{"role": "assistant", "content": "新增进展"}]

    layers = bridge.task_layers(project, "task-1")
    assert layers["read_only"] is True
    assert layers["mode"] == "shadow"
    assert layers["candidate_plan"]["writes_to_qmemory"] is False
    assert layers["candidate_plan"]["automatic_confirmation"] is False
    assert layers["recall_plan"] == {
        "top_k": 5,
        "automatic_injection": False,
        "requires_explicit_consumer_action": True,
    }
    assert layers["l1"][0]["qmemory_source_refs"][0]["conversation_id"] == "codex:primary"
    assert layers["l2"]["items"][0]["path"] == "projects/qmemory"
    assert layers["metrics"]["pipeline"] == "recall"
    assert layers["metrics"]["provenance_coverage"] == 1.0

    state = json.loads((tmp_path / "bridge.json").read_text(encoding="utf-8"))
    assert [run["pipeline"] for run in state["runs"]] == ["extraction", "extraction", "extraction", "recall"]
    assert all(run["mode"] == "shadow" for run in state["runs"])


def test_sync_all_skips_paused_projects(tmp_path: Path) -> None:
    FakeMemoryCoreClient.calls = []
    service = BridgeService(tmp_path)
    (tmp_path / "project").mkdir()
    bridge = MemoryCoreBridge(
        service,  # type: ignore[arg-type]
        MemoryCoreConfig(enabled=True),
        state_path=tmp_path / "bridge.json",
        client_factory=FakeMemoryCoreClient,
    )
    result = bridge.sync_all()
    assert result["projects"] == 1
    assert result["failures"] == []


def test_sync_project_fails_open_without_advancing_local_cursor(tmp_path: Path) -> None:
    service = BridgeService(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    bridge = MemoryCoreBridge(
        service,  # type: ignore[arg-type]
        MemoryCoreConfig(enabled=True, fail_open=True),
        state_path=tmp_path / "bridge.json",
        client_factory=FailingSyncClient,
    )

    result = bridge.sync_project(project)

    assert result["status"] == "degraded"
    assert result["fail_open"] is True
    assert result["sent_messages"] == 0
    assert {failure["operation"] for failure in result["failures"]} == {"add_conversation"}
    state = json.loads((tmp_path / "bridge.json").read_text(encoding="utf-8"))
    conversations = state["projects"]["project-1"]["conversations"]
    assert all(record["last_sequence"] == 0 for record in conversations.values())
    assert state["runs"][-1]["pipeline"] == "extraction"
    assert state["runs"][-1]["status"] == "degraded"


def test_sync_all_surfaces_fail_open_project_failures(tmp_path: Path) -> None:
    service = BridgeService(tmp_path)
    (tmp_path / "project").mkdir()
    bridge = MemoryCoreBridge(
        service,  # type: ignore[arg-type]
        MemoryCoreConfig(enabled=True, fail_open=True),
        state_path=tmp_path / "bridge.json",
        client_factory=FailingSyncClient,
    )

    result = bridge.sync_all()

    assert result["projects"] == 1
    assert result["sent_messages"] == 0
    assert {failure["operation"] for failure in result["failures"]} == {"add_conversation"}
    assert all(failure["project"] == str(tmp_path / "project") for failure in result["failures"])


def test_fail_open_can_be_disabled_for_operator_diagnostics(tmp_path: Path) -> None:
    service = BridgeService(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    bridge = MemoryCoreBridge(
        service,  # type: ignore[arg-type]
        MemoryCoreConfig(enabled=True, fail_open=False),
        state_path=tmp_path / "bridge.json",
        client_factory=FailingSyncClient,
    )
    with pytest.raises(RuntimeError, match="gateway unavailable"):
        bridge.sync_project(project)


def test_shadow_recall_plan_fails_open_and_never_requests_injection_or_confirmation(
    tmp_path: Path,
) -> None:
    FakeMemoryCoreClient.calls = []
    service = BridgeService(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    state_path = tmp_path / "bridge.json"
    MemoryCoreBridge(
        service,  # type: ignore[arg-type]
        MemoryCoreConfig(enabled=True),
        state_path=state_path,
        client_factory=FakeMemoryCoreClient,
    ).sync_project(project)
    bridge = MemoryCoreBridge(
        service,  # type: ignore[arg-type]
        MemoryCoreConfig(enabled=True, fail_open=True),
        state_path=state_path,
        client_factory=FailingRecallClient,
    )

    plan = bridge.task_layers(project, "task-1")

    assert plan["status"] == "degraded"
    assert plan["l1"] == []
    assert plan["l2"] == {"items": []}
    assert plan["l3"] == {}
    assert plan["candidate_plan"]["writes_to_qmemory"] is False
    assert plan["candidate_plan"]["automatic_confirmation"] is False
    assert plan["recall_plan"]["automatic_injection"] is False
    assert {failure["operation"] for failure in plan["failures"]} == {
        "query_atomic",
        "list_scenarios",
        "read_core",
    }
    assert plan["metrics"]["status"] == "degraded"
