from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from qmemory.adapters import AdapterStatus, SuperLocalMemoryAdapter


def _memory(secret: str) -> dict:
    return {
        "memory_id": "memory-1",
        "project_id": "project-1",
        "memory_type": "fact",
        "holder": "agent",
        "subject": "projection",
        "statement": "password=%s" % secret,
        "as_of": "2026-08-27T00:00:00Z",
        "source_ref": "codex://conversation#message-1",
    }


def test_projection_command_and_json_output_are_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = SuperLocalMemoryAdapter()
    synthetic = "synthetic-adapter-material"
    captured = {}
    monkeypatch.setattr(
        adapter, "status", lambda: AdapterStatus(adapter.name, True, "/invalid/slm")
    )

    def fake_run(command: list[str], **_: object) -> SimpleNamespace:
        captured["envelope"] = json.loads(command[2])
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"detail": "api_key=%s" % synthetic}),
            stderr="",
        )

    monkeypatch.setattr("qmemory.adapters.subprocess.run", fake_run)

    result = adapter.project_memory(_memory(synthetic))

    assert synthetic not in json.dumps(captured["envelope"])
    assert synthetic not in json.dumps(result)


def test_projection_error_does_not_reemit_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = SuperLocalMemoryAdapter()
    synthetic = "synthetic-error-material"
    monkeypatch.setattr(
        adapter, "status", lambda: AdapterStatus(adapter.name, True, "/invalid/slm")
    )
    monkeypatch.setattr(
        "qmemory.adapters.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="password=%s" % synthetic,
        ),
    )

    with pytest.raises(RuntimeError) as error:
        adapter.project_memory(_memory("synthetic-input-material"))

    assert synthetic not in str(error.value)
    assert "[REDACTED:assigned-secret]" in str(error.value)
