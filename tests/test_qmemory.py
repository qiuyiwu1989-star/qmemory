from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from qmemory.events import SyncConflictError, calculate_hash, validate_shard
from qmemory.project import canonical_remote
from qmemory.redaction import redact_text
from qmemory.service import MemoryService


class QMemoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directories = []
        self.project = self.directory("project")

    def tearDown(self) -> None:
        for directory in reversed(self.temporary_directories):
            directory.cleanup()

    def directory(self, prefix: str) -> Path:
        temporary = tempfile.TemporaryDirectory(prefix="qmemory-%s-" % prefix)
        self.temporary_directories.append(temporary)
        return Path(temporary.name)

    def service(self, name: str) -> MemoryService:
        return MemoryService(self.directory(name), device_id=name)

    def test_remote_identity_is_stable_across_protocols(self) -> None:
        self.assertEqual(
            canonical_remote("git@github.com:Qualixar/SuperLocalMemory.git"),
            "github.com/qualixar/superlocalmemory",
        )
        self.assertEqual(
            canonical_remote("https://token@example.com/Owner/Repo.git"),
            "example.com/owner/repo",
        )

    def test_redaction_happens_before_event_persistence(self) -> None:
        service = self.service("redact-device")
        # Assemble the synthetic value at runtime so repository secret scanners do not
        # mistake the test fixture for a committed credential.
        fake_secret = "ghp_" + "abcdefghijklmnopqrstuvwxyz123456"
        created = service.remember(
            self.project,
            "environment",
            "token=%s password=hunter22" % fake_secret,
            confirmed=True,
        )
        self.assertNotIn(fake_secret, created["statement"])
        self.assertIn("[REDACTED:github-token]", created["statement"])
        shard_text = service.events.local_shard.read_text(encoding="utf-8")
        self.assertNotIn(fake_secret, shard_text)
        self.assertNotIn("hunter22", shard_text)

    def test_decision_lifecycle_and_project_guard(self) -> None:
        service = self.service("lifecycle-device")
        created = service.remember(
            self.project, "decision", "Use an append-only device shard"
        )
        self.assertEqual(created["status"], "proposed")
        self.assertEqual(service.search(self.project, "append"), [])
        service.confirm(self.project, created["memory_id"])
        self.assertEqual(len(service.search(self.project, "append")), 1)
        replacement = service.supersede(
            self.project, created["memory_id"], "Use hash-chained device shards"
        )
        old = service.projection.get(created["memory_id"])
        self.assertEqual(old["status"], "superseded")
        self.assertEqual(old["superseded_by"], replacement["memory_id"])
        with self.assertRaises(ValueError):
            service.confirm(self.directory("other-project"), replacement["memory_id"])

    def test_identical_current_memory_is_deduplicated(self) -> None:
        service = self.service("dedupe-device")
        first = service.remember(
            self.project,
            "constraint",
            "Never store transient build output",
            subject="repository",
            holder="agent",
        )
        second = service.remember(
            self.project,
            "constraint",
            "Never store transient build output",
            subject="repository",
            holder="agent",
        )
        self.assertEqual(first["memory_id"], second["memory_id"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(service.status()["events"], 1)

    def test_archive_all_proposed_preserves_active_and_is_rebuildable(self) -> None:
        service = self.service("candidate-reset-device")
        active = service.remember(
            self.project, "decision", "Keep the verified decision", confirmed=True
        )
        first = service.remember(self.project, "fact", "Candidate one")
        second = service.remember(self.project, "constraint", "Candidate two")

        result = service.archive_all_proposed()

        self.assertEqual(result["archived"], 2)
        self.assertEqual(service.projection.get(active["memory_id"])["status"], "active")
        self.assertEqual(service.projection.get(first["memory_id"])["status"], "archived")
        self.assertEqual(service.projection.get(second["memory_id"])["status"], "archived")
        service.projection.rebuild()
        self.assertEqual(service.projection.get(first["memory_id"])["status"], "archived")

    def test_new_handoff_supersedes_previous_current_handoff(self) -> None:
        service = self.service("handoff-device")
        first = service.session_handoff(self.project, "Tests pass; package remains to build")
        second = service.session_handoff(self.project, "Package built; install remains")
        old = service.projection.get(first["memory_id"])
        self.assertEqual(old["status"], "superseded")
        self.assertEqual(old["superseded_by"], second["memory_id"])
        active = [
            memory
            for memory in service.browse(self.project, status="active")
            if memory["memory_type"] == "handoff"
        ]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["statement"], "Package built; install remains")

    def test_two_devices_merge_without_sqlite_copy(self) -> None:
        device_a = self.service("device-a")
        device_b = self.service("device-b")
        sync = self.directory("sync")
        memory_a = device_a.remember(
            self.project, "constraint", "Never sync the SQLite projection", confirmed=True
        )
        memory_b = device_b.remember(
            self.project, "handoff", "Continue from the event merger", confirmed=True
        )
        device_a.sync_push(sync)
        device_b.sync_push(sync)
        device_a.sync_pull(sync)
        device_b.sync_pull(sync)
        for service in (device_a, device_b):
            statements = {item["statement"] for item in service.search(self.project, "", limit=20)}
            self.assertIn(memory_a["statement"], statements)
            self.assertIn(memory_b["statement"], statements)
            self.assertEqual(service.status()["shards"], 2)

    def test_stale_sync_copy_does_not_rollback_local_shard(self) -> None:
        device = self.service("device-stale")
        sync = self.directory("sync-stale")
        device.remember(self.project, "fact", "first", confirmed=True)
        device.sync_push(sync)
        device.remember(self.project, "fact", "second", confirmed=True)
        result = device.sync_pull(sync)
        self.assertEqual(result["stale_shards"], 1)
        self.assertEqual(len(validate_shard(device.events.local_shard)), 2)

    def test_stale_local_push_does_not_overwrite_newer_shared_shard(self) -> None:
        device = self.service("device-push-guard")
        sync = self.directory("sync-push-guard")
        device.remember(self.project, "fact", "first", confirmed=True)
        device.sync_push(sync)
        saved_old = device.events.local_shard.read_text(encoding="utf-8")
        device.remember(self.project, "fact", "second", confirmed=True)
        device.sync_push(sync)
        device.events.local_shard.write_text(saved_old, encoding="utf-8")
        with self.assertRaises(SyncConflictError):
            device.sync_push(sync)
        self.assertEqual(len(validate_shard(sync / "device-push-guard.jsonl")), 2)

    def test_divergent_same_device_shard_is_quarantined(self) -> None:
        device = self.service("device-conflict")
        sync = self.directory("sync-conflict")
        device.remember(self.project, "fact", "canonical", confirmed=True)
        device.sync_push(sync)
        incoming = sync / "device-conflict.jsonl"
        event = json.loads(incoming.read_text(encoding="utf-8").strip())
        event["payload"]["memory"]["statement"] = "tampered-but-rehashed"
        event["event_hash"] = calculate_hash(event)
        incoming.write_text(json.dumps(event, sort_keys=True) + "\n", encoding="utf-8")
        with self.assertRaises(SyncConflictError):
            device.sync_pull(sync)
        self.assertEqual(len(list(device.settings.conflicts_dir.iterdir())), 1)

    def test_concurrent_supersede_has_one_winner_and_a_visible_issue(self) -> None:
        device_a = self.service("device-one")
        device_b = self.service("device-two")
        sync = self.directory("sync-concurrent")
        original = device_a.remember(
            self.project, "decision", "Use transport A", confirmed=True
        )
        device_a.sync_push(sync)
        device_b.sync_pull(sync)
        device_a.supersede(self.project, original["memory_id"], "Use transport B")
        device_b.supersede(self.project, original["memory_id"], "Use transport C")
        device_a.sync_push(sync)
        device_b.sync_push(sync)
        device_a.sync_pull(sync)
        status = device_a.status()
        active = device_a.search(self.project, "transport", limit=10)
        self.assertEqual(len(active), 1)
        self.assertEqual(status["projection_issues"], 1)
        self.assertEqual(status["memories"].get("archived"), 1)

    def test_concurrent_projection_rebuilds_are_serialized(self) -> None:
        device = self.service("projection-lock")
        device.remember(self.project, "fact", "projection lock", confirmed=True)
        errors = []

        def rebuild() -> None:
            try:
                for _ in range(5):
                    device.projection.rebuild()
            except Exception as exc:  # pragma: no cover - assertion captures it
                errors.append(exc)

        workers = [threading.Thread(target=rebuild) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(errors, [])
        self.assertEqual(device.status()["events"], 1)


if __name__ == "__main__":
    unittest.main()
