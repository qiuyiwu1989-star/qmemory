from __future__ import annotations

import json

from qmemory.sync_observability import SyncRunTracker, new_sync_run_id


def test_sync_run_ids_are_unique_and_non_semantic() -> None:
    first = new_sync_run_id()
    second = new_sync_run_id()

    assert first.startswith("sync_")
    assert first != second
    assert len(first) == len("sync_") + 32


def test_sync_snapshot_reports_idempotent_counts_by_source() -> None:
    tracker = SyncRunTracker(sync_run_id="sync_" + "1" * 32)
    tracker.record_found("codex", 3)
    tracker.record_imported("codex", messages_indexed=8, bytes_copied=120)
    tracker.record_unchanged("codex", 2)
    tracker.record_found("claude-code", 2)
    tracker.record_unchanged("claude-code", 2)

    result = tracker.snapshot(projects=4, local_only=True)

    assert result["sync_run_id"] == "sync_" + "1" * 32
    assert result["found"] == 5
    assert result["imported"] == 1
    assert result["unchanged"] == 4
    assert result["failures"] == 0
    assert result["messages_indexed"] == 8
    assert result["bytes_copied"] == 120
    assert result["by_source"]["codex"] == {
        "found": 3,
        "imported": 1,
        "unchanged": 2,
        "failures": 0,
        "messages_indexed": 8,
        "bytes_copied": 120,
    }
    assert result["projects"] == 4
    assert result["local_only"] is True


def test_sync_failure_status_is_bounded_and_redacted() -> None:
    tracker = SyncRunTracker(max_error_samples=1)
    tracker.record_found("codex", 2)
    tracker.record_failure("codex", RuntimeError("password=synthetic-sync-failure"))
    tracker.record_failure("codex", RuntimeError("password=second-hidden-failure"))

    result = tracker.snapshot(status_detail={"token": "ghp_" + "Q" * 30})
    serialized = json.dumps(result)

    assert result["failures"] == 2
    assert result["by_source"]["codex"]["failures"] == 2
    assert len(result["errors"]) == 1
    assert "synthetic-sync-failure" not in serialized
    assert "second-hidden-failure" not in serialized
    assert "ghp_" not in serialized
