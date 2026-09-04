from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import DEVICE_ID_PATTERN, Settings


GENESIS_HASH = "0" * 64


class EventValidationError(ValueError):
    pass


class SyncConflictError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_json(value: Dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def calculate_hash(event: Dict[str, Any]) -> str:
    body = dict(event)
    body.pop("event_hash", None)
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def validate_shard(path: Path) -> List[Dict[str, Any]]:
    device_id = path.stem
    if not DEVICE_ID_PATTERN.fullmatch(device_id):
        raise EventValidationError("Invalid shard filename: %s" % path.name)
    events: List[Dict[str, Any]] = []
    expected_sequence = 1
    previous_hash = GENESIS_HASH
    seen_ids = set()
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise EventValidationError(
                    "%s:%d invalid JSON: %s" % (path.name, line_number, exc)
                )
            if event.get("schema_version") != 1:
                raise EventValidationError("%s:%d unsupported schema" % (path.name, line_number))
            if event.get("device_id") != device_id:
                raise EventValidationError("%s:%d device mismatch" % (path.name, line_number))
            if event.get("sequence") != expected_sequence:
                raise EventValidationError("%s:%d non-contiguous sequence" % (path.name, line_number))
            if event.get("previous_hash") != previous_hash:
                raise EventValidationError("%s:%d broken hash chain" % (path.name, line_number))
            if event.get("event_hash") != calculate_hash(event):
                raise EventValidationError("%s:%d invalid event hash" % (path.name, line_number))
            event_id = event.get("event_id")
            if not event_id or event_id in seen_ids:
                raise EventValidationError("%s:%d duplicate/missing event id" % (path.name, line_number))
            seen_ids.add(event_id)
            events.append(event)
            expected_sequence += 1
            previous_hash = event["event_hash"]
    return events


class EventStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.ensure()

    @property
    def local_shard(self) -> Path:
        return self.settings.events_dir / (self.settings.device_id + ".jsonl")

    def append(self, kind: str, project_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.local_shard.parent.mkdir(parents=True, exist_ok=True)
        with self.local_shard.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.seek(0)
            existing = [line for line in handle if line.strip()]
            if existing:
                validated = validate_shard(self.local_shard)
                previous = validated[-1]
                sequence = int(previous["sequence"]) + 1
                previous_hash = str(previous["event_hash"])
            else:
                sequence = 1
                previous_hash = GENESIS_HASH
            event: Dict[str, Any] = {
                "schema_version": 1,
                "event_id": str(uuid.uuid4()),
                "device_id": self.settings.device_id,
                "sequence": sequence,
                "occurred_at": utc_now(),
                "kind": kind,
                "project_id": project_id,
                "payload": payload,
                "previous_hash": previous_hash,
            }
            event["event_hash"] = calculate_hash(event)
            handle.seek(0, os.SEEK_END)
            handle.write(canonical_json(event) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return event

    def all_events(self) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        global_ids: Dict[str, str] = {}
        for shard in sorted(self.settings.events_dir.glob("*.jsonl")):
            for event in validate_shard(shard):
                event_id = event["event_id"]
                if event_id in global_ids and global_ids[event_id] != event["event_hash"]:
                    raise EventValidationError("Conflicting global event id: %s" % event_id)
                global_ids[event_id] = event["event_hash"]
                events.append(event)
        events.sort(
            key=lambda item: (
                item["occurred_at"],
                item["device_id"],
                item["sequence"],
                item["event_id"],
            )
        )
        return events

    def publish(self, sync_directory: Path) -> Dict[str, Any]:
        sync_directory = sync_directory.expanduser().resolve()
        sync_directory.mkdir(parents=True, exist_ok=True)
        if self.local_shard.exists():
            events = validate_shard(self.local_shard)
        else:
            events = []
        destination = sync_directory / self.local_shard.name
        destination_events = validate_shard(destination) if destination.exists() else []
        overlap = min(len(events), len(destination_events))
        divergent = any(
            events[index]["event_hash"] != destination_events[index]["event_hash"]
            for index in range(overlap)
        )
        if divergent:
            raise SyncConflictError(
                "Shared directory contains a divergent local-device shard; pull and inspect conflicts first"
            )
        if len(destination_events) > len(events):
            raise SyncConflictError(
                "Shared directory has a newer local-device shard; refusing to overwrite it"
            )
        if len(destination_events) == len(events):
            return {
                "device_id": self.settings.device_id,
                "events": len(events),
                "destination": str(destination),
                "head": events[-1]["event_hash"] if events else GENESIS_HASH,
                "unchanged": True,
            }
        temporary = destination.with_suffix(".tmp")
        if self.local_shard.exists():
            shutil.copyfile(str(self.local_shard), str(temporary))
        else:
            temporary.write_text("", encoding="utf-8")
        os.replace(str(temporary), str(destination))
        return {
            "device_id": self.settings.device_id,
            "events": len(events),
            "destination": str(destination),
            "head": events[-1]["event_hash"] if events else GENESIS_HASH,
            "unchanged": False,
        }

    def pull(self, sync_directory: Path) -> Dict[str, Any]:
        sync_directory = sync_directory.expanduser().resolve()
        imported = 0
        unchanged = 0
        stale = 0
        conflicts: List[str] = []
        for incoming in sorted(sync_directory.glob("*.jsonl")):
            incoming_events = validate_shard(incoming)
            destination = self.settings.events_dir / incoming.name
            current_events = validate_shard(destination) if destination.exists() else []
            overlap = min(len(incoming_events), len(current_events))
            divergent = any(
                incoming_events[index]["event_hash"] != current_events[index]["event_hash"]
                for index in range(overlap)
            )
            if divergent:
                conflicts.append(incoming.name)
                conflict_copy = self.settings.conflicts_dir / (
                    "%s.%s" % (uuid.uuid4().hex, incoming.name)
                )
                shutil.copyfile(str(incoming), str(conflict_copy))
                continue
            if len(incoming_events) < len(current_events):
                stale += 1
                continue
            if len(incoming_events) == len(current_events):
                unchanged += 1
                continue
            temporary_fd, temporary_name = tempfile.mkstemp(
                prefix=incoming.stem + ".", suffix=".tmp", dir=str(self.settings.events_dir)
            )
            os.close(temporary_fd)
            shutil.copyfile(str(incoming), temporary_name)
            os.replace(temporary_name, str(destination))
            imported += len(incoming_events) - len(current_events)
        if conflicts:
            raise SyncConflictError(
                "Divergent device shard(s) quarantined: %s" % ", ".join(conflicts)
            )
        return {
            "imported_events": imported,
            "unchanged_shards": unchanged,
            "stale_shards": stale,
        }
