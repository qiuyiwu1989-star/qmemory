from __future__ import annotations

import json
import os
import platform
import re
import socket
import uuid
from pathlib import Path
from typing import Any, Dict, Optional


DEVICE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")


def default_home() -> Path:
    override = os.environ.get("QMEMORY_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".local" / "share" / "qmemory").resolve()


def _safe_device_label() -> str:
    raw = socket.gethostname() or platform.node() or "device"
    label = re.sub(r"[^a-z0-9._-]+", "-", raw.lower()).strip("-.")
    if len(label) < 3:
        label = "device"
    return label[:40]


class Settings:
    def __init__(self, home: Optional[Path] = None) -> None:
        self.home = (home or default_home()).expanduser().resolve()
        self.config_dir = self.home / "config"
        self.events_dir = self.home / "events"
        self.sources_dir = self.home / "sources"
        self.state_dir = self.home / "state"
        self.conflicts_dir = self.home / "conflicts"
        self.device_file = self.config_dir / "device.json"
        self.database_path = self.state_dir / "qmemory.sqlite3"
        self.source_database_path = self.state_dir / "sources.sqlite3"

    def ensure(self, device_id: Optional[str] = None) -> Dict[str, Any]:
        for directory in (
            self.config_dir,
            self.events_dir,
            self.sources_dir,
            self.state_dir,
            self.conflicts_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        try:
            self.home.chmod(0o700)
            self.sources_dir.chmod(0o700)
        except OSError:
            pass

        if self.device_file.exists():
            with self.device_file.open("r", encoding="utf-8") as handle:
                config = json.load(handle)
            self._validate_device_id(config.get("device_id", ""))
            if device_id and device_id != config["device_id"]:
                raise ValueError(
                    "This QMemory home is already bound to device %s" % config["device_id"]
                )
            return config

        resolved = device_id or "%s-%s" % (_safe_device_label(), uuid.uuid4().hex[:8])
        self._validate_device_id(resolved)
        config = {"device_id": resolved, "schema_version": 1}
        temporary = self.device_file.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(config, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(self.device_file))
        return config

    @staticmethod
    def _validate_device_id(device_id: str) -> None:
        if not DEVICE_ID_PATTERN.fullmatch(device_id):
            raise ValueError(
                "device_id must be 3-64 lowercase letters, digits, dots, dashes, or underscores"
            )

    @property
    def device_id(self) -> str:
        return str(self.ensure()["device_id"])
