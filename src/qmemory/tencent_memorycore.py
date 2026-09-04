from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

from .service import MemoryService
from .textio import run_text


MAX_MEMORYCORE_MESSAGES = 100


def _stable_segment(value: str, limit: int = 72) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-.")
    if not cleaned:
        cleaned = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return cleaned[:limit]


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


@dataclass(frozen=True)
class MemoryCoreIdentity:
    team_id: str
    user_id: str
    agent_id: str
    service_id: str = "qmemory-lab"


@dataclass(frozen=True)
class MemoryCoreConfig:
    """Persistable, non-secret connection settings for the production bridge."""

    enabled: bool = False
    base_url: str = "http://127.0.0.1:8420"
    service_id: str = "qmemory"
    team_id: str = "qmemory-personal"
    user_id: str = "qmemory-user"
    api_key_env: str = "QMEMORY_MEMORYCORE_API_KEY"
    operation_mode: str = "shadow"
    fail_open: bool = True
    recall_top_k: int = 5

    @classmethod
    def load(cls, path: Path) -> "MemoryCoreConfig":
        if not path.exists():
            return cls()
        value = json.loads(path.read_text(encoding="utf-8"))
        allowed = {field for field in cls.__dataclass_fields__}
        return cls(**{key: value[key] for key in allowed if key in value})

    def save(self, path: Path) -> None:
        validate_memorycore_url(self.base_url)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.api_key_env):
            raise ValueError("api_key_env must be an environment-variable name")
        if self.operation_mode != "shadow":
            raise ValueError("only shadow MemoryCore operation is currently supported")
        if not 1 <= int(self.recall_top_k) <= 100:
            raise ValueError("recall_top_k must be between 1 and 100")
        path.parent.mkdir(parents=True, exist_ok=True)
        value = {
            "schema_version": 2,
            "enabled": bool(self.enabled),
            "base_url": self.base_url.rstrip("/"),
            "service_id": self.service_id,
            "team_id": self.team_id,
            "user_id": self.user_id,
            "api_key_env": self.api_key_env,
            "operation_mode": self.operation_mode,
            "fail_open": bool(self.fail_open),
            "recall_top_k": int(self.recall_top_k),
        }
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(_json_bytes(value))
        os.replace(str(temporary), str(path))


MEMORYCORE_KEYCHAIN_SERVICE = "com.qiuyiwu.qmemory.memorycore"


def memorycore_keychain_account(config: MemoryCoreConfig) -> str:
    return "%s:%s" % (config.team_id, config.user_id)


def load_memorycore_api_key(config: MemoryCoreConfig) -> Optional[str]:
    from_environment = os.environ.get(config.api_key_env)
    if from_environment:
        return from_environment
    if sys.platform != "darwin":
        return None
    result = run_text(
        [
            "security", "find-generic-password", "-s", MEMORYCORE_KEYCHAIN_SERVICE,
            "-a", memorycore_keychain_account(config), "-w",
        ],
        stderr=subprocess.PIPE,
        timeout=3,
    )
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def store_memorycore_api_key(config: MemoryCoreConfig, secret: str) -> None:
    if sys.platform != "darwin":
        raise RuntimeError("secure desktop key storage currently requires macOS Keychain")
    if not secret:
        return
    result = run_text(
        [
            "security", "add-generic-password", "-U", "-s", MEMORYCORE_KEYCHAIN_SERVICE,
            "-a", memorycore_keychain_account(config), "-w", secret,
        ],
        stderr=subprocess.PIPE,
        timeout=8,
    )
    if result.returncode != 0:
        raise RuntimeError("could not save MemoryCore key in macOS Keychain")


def validate_memorycore_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.username or parsed.password:
        raise ValueError("MemoryCore URL must not contain credentials")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("MemoryCore URL must be an HTTP(S) endpoint")
    local = parsed.hostname in ("127.0.0.1", "localhost", "::1")
    if parsed.scheme == "http" and not local:
        raise ValueError("remote MemoryCore endpoints must use HTTPS")
    return base_url.rstrip("/")


class MemoryCoreClient:
    """Small dependency-free client for TencentDB MemoryCore v3."""

    def __init__(
        self,
        config: MemoryCoreConfig,
        identity: MemoryCoreIdentity,
        *,
        api_key: Optional[str] = None,
        timeout: float = 20.0,
    ) -> None:
        self.config = config
        self.identity = identity
        self.base_url = validate_memorycore_url(config.base_url)
        self._api_key = api_key if api_key is not None else load_memorycore_api_key(config)
        self.timeout = timeout

    def _request(
        self, method: str, path: str, payload: Optional[Dict[str, Any]] = None
    ) -> Any:
        headers = {"Accept": "application/json", "x-tdai-service-id": self.identity.service_id}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = _json_bytes(payload)
        if self._api_key:
            headers["Authorization"] = "Bearer %s" % self._api_key
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError("MemoryCore HTTP %s: %s" % (exc.code, detail)) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError("MemoryCore connection failed: %s" % exc.reason) from exc
        body = json.loads(raw) if raw else {}
        if isinstance(body, dict) and "code" in body:
            if body.get("code") not in (0, "0", None):
                raise RuntimeError("MemoryCore rejected request: %s" % body.get("message", body))
            return body.get("data", {})
        return body

    def health(self) -> Dict[str, Any]:
        result = self._request("GET", "/health")
        return result if isinstance(result, dict) else {"result": result}

    def _identity_payload(self) -> Dict[str, str]:
        return {
            "team_id": self.identity.team_id,
            "agent_id": self.identity.agent_id,
            "user_id": self.identity.user_id,
        }

    def add_conversation(
        self,
        session_id: str,
        messages: List[Dict[str, str]],
        *,
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            **self._identity_payload(),
            "session_id": session_id,
            "messages": messages,
        }
        if task_id:
            payload["task_id"] = task_id
        result = self._request("POST", "/v3/conversation/add", payload)
        return result if isinstance(result, dict) else {"result": result}

    def query_atomic(self, *, session_id: Optional[str] = None, limit: int = 100) -> Any:
        payload: Dict[str, Any] = {**self._identity_payload(), "limit": limit, "offset": 0}
        if session_id:
            payload["session_id"] = session_id
        return self._request("POST", "/v3/atomic/query", payload)

    def list_scenarios(self, path_prefix: str = "") -> Any:
        payload: Dict[str, Any] = self._identity_payload()
        if path_prefix:
            payload["path_prefix"] = path_prefix
        return self._request("POST", "/v3/scenario/ls", payload)

    def read_scenario(self, path: str) -> Any:
        return self._request("POST", "/v3/scenario/read", {**self._identity_payload(), "path": path})

    def read_core(self) -> Any:
        return self._request("POST", "/v3/core/read", self._identity_payload())


class MemoryCoreBridge:
    """Incremental, fail-open shadow processor over QMemory's immutable L0 archive.

    The bridge may send redacted L0 copies to MemoryCore and read derived previews. It never
    injects those previews into an agent context and never confirms them as QMemory memories.
    """

    def __init__(
        self,
        service: MemoryService,
        config: MemoryCoreConfig,
        *,
        state_path: Optional[Path] = None,
        client_factory: Any = MemoryCoreClient,
    ) -> None:
        self.service = service
        self.config = config
        self.state_path = state_path or (service.settings.state_dir / "memorycore-bridge.json")
        self.client_factory = client_factory

    @staticmethod
    def _project_agent_id(project_id: str) -> str:
        return "qmemory-project-%s" % hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:20]

    @staticmethod
    def _remote_task_id(task_id: str) -> str:
        return "qmemory-task-%s" % hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:20]

    def identity(self, project_path: Path) -> MemoryCoreIdentity:
        project_id = self.service.project(project_path.expanduser().resolve()).project_id
        return MemoryCoreIdentity(
            team_id=self.config.team_id,
            user_id=self.config.user_id,
            agent_id=self._project_agent_id(project_id),
            service_id=self.config.service_id,
        )

    def client(self, project_path: Path) -> MemoryCoreClient:
        return self.client_factory(self.config, self.identity(project_path))

    def _load_state(self) -> Dict[str, Any]:
        if not self.state_path.exists():
            return {"schema_version": 2, "projects": {}, "runs": []}
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _save_state(self, state: Dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_bytes(_json_bytes(state))
        os.replace(str(temporary), str(self.state_path))

    @staticmethod
    def _run_metric(
        pipeline: str,
        project_id: str,
        started_at: str,
        started_clock: float,
        *,
        status: str,
        **values: Any,
    ) -> Dict[str, Any]:
        return {
            "schema_version": 1,
            "pipeline": pipeline,
            "mode": "shadow",
            "project_id": project_id,
            "started_at": started_at,
            "duration_ms": round((time.perf_counter() - started_clock) * 1000.0, 3),
            "status": status,
            **values,
        }

    def _record_run(self, state: Dict[str, Any], metric: Dict[str, Any]) -> None:
        runs = state.setdefault("runs", [])
        runs.append(metric)
        del runs[:-200]
        state["schema_version"] = max(2, int(state.get("schema_version", 1)))
        self._save_state(state)

    @staticmethod
    def _failure(operation: str, exc: Exception, **context: Any) -> Dict[str, Any]:
        return {"operation": operation, "error": str(exc), **context}

    @staticmethod
    def _prefix_hash(messages: List[Dict[str, Any]], maximum_sequence: int) -> str:
        prefix = [message for message in messages if int(message["sequence"]) <= maximum_sequence]
        return hashlib.sha256(_json_bytes(prefix)).hexdigest()

    @staticmethod
    def _accepted_ids(response: Dict[str, Any]) -> List[str]:
        raw = response.get("accepted_ids") or response.get("message_ids") or []
        return [str(value) for value in raw] if isinstance(raw, list) else []

    def sync_project(self, project_path: Path) -> Dict[str, Any]:
        if not self.config.enabled:
            raise RuntimeError("MemoryCore connection is disabled")
        if self.config.operation_mode != "shadow":
            raise RuntimeError("only shadow MemoryCore operation is currently supported")
        started_at = datetime.now(timezone.utc).isoformat()
        started_clock = time.perf_counter()
        project_path = project_path.expanduser().resolve()
        project_id = self.service.project(project_path).project_id
        state = self._load_state()
        project_state = state.setdefault("projects", {}).setdefault(
            project_id, {"conversations": {}, "message_map": {}}
        )
        conversations = project_state.setdefault("conversations", {})
        message_map = project_state.setdefault("message_map", {})
        failures: List[Dict[str, Any]] = []
        try:
            client = self.client(project_path)
        except Exception as exc:
            if not self.config.fail_open:
                raise
            failures.append(self._failure("connect", exc))
            metric = self._run_metric(
                "extraction",
                project_id,
                started_at,
                started_clock,
                status="degraded",
                sent_messages=0,
                sent_batches=0,
                failure_count=1,
            )
            self._record_run(state, metric)
            return {
                "project_id": project_id,
                "mode": "shadow",
                "status": "degraded",
                "fail_open": True,
                "conversations": 0,
                "sent_messages": 0,
                "sent_batches": 0,
                "unchanged_conversations": 0,
                "regenerated_conversations": 0,
                "failures": failures,
                "metrics": metric,
            }
        sent_messages = 0
        sent_batches = 0
        unchanged = 0
        regenerated = 0
        seen: set[str] = set()

        for task in self.service.tasks(project_path, limit=10_000):
            detail = self.service.task(project_path, str(task["task_id"]))
            primary_id = str(detail.get("primary_conversation_id") or "")
            members = list(detail.get("source_copies", [])) + list(detail.get("subagents", []))
            if primary_id and not any(str(member.get("conversation_id")) == primary_id for member in members):
                members.insert(0, {"conversation_id": primary_id})
            for member in members:
                conversation_id = str(member.get("conversation_id") or "")
                if not conversation_id or conversation_id in seen:
                    continue
                seen.add(conversation_id)
                conversation = (
                    detail.get("primary_conversation")
                    if conversation_id == primary_id
                    else self.service.conversation(project_path, conversation_id, redact=True)
                )
                messages = TencentMemoryCoreLab._messages((conversation or {}).get("messages", []))
                record = conversations.setdefault(
                    conversation_id,
                    {"generation": 1, "last_sequence": 0, "prefix_hash": "", "sessions": []},
                )
                last_sequence = int(record.get("last_sequence", 0))
                if last_sequence and record.get("prefix_hash") != self._prefix_hash(messages, last_sequence):
                    record = {
                        "generation": int(record.get("generation", 1)) + 1,
                        "last_sequence": 0,
                        "prefix_hash": "",
                        "sessions": list(record.get("sessions", [])),
                    }
                    conversations[conversation_id] = record
                    last_sequence = 0
                    regenerated += 1
                pending = [message for message in messages if int(message["sequence"]) > last_sequence]
                if not pending:
                    unchanged += 1
                    continue
                generation = int(record.get("generation", 1))
                session_id = "qmemory-%s-g%03d" % (_stable_segment(conversation_id), generation)
                if session_id not in record.setdefault("sessions", []):
                    record["sessions"].append(session_id)
                for batch in TencentMemoryCoreLab._batches(pending, MAX_MEMORYCORE_MESSAGES):
                    try:
                        response = client.add_conversation(
                            session_id,
                            [{"role": item["role"], "content": item["content"]} for item in batch],
                            task_id=self._remote_task_id(str(task["task_id"])),
                        )
                    except Exception as exc:
                        if not self.config.fail_open:
                            raise
                        failures.append(
                            self._failure(
                                "add_conversation",
                                exc,
                                task_id=str(task["task_id"]),
                                conversation_id=conversation_id,
                                session_id=session_id,
                                first_sequence=int(batch[0]["sequence"]),
                            )
                        )
                        # Keep the local cursor unchanged so a later run can safely retry in order.
                        break
                    for accepted_id, item in zip(self._accepted_ids(response), batch):
                        message_map[accepted_id] = {
                            "conversation_id": conversation_id,
                            "sequence": int(item["sequence"]),
                            "task_id": str(task["task_id"]),
                        }
                    record["last_sequence"] = int(batch[-1]["sequence"])
                    record["prefix_hash"] = self._prefix_hash(messages, int(record["last_sequence"]))
                    record["task_id"] = str(task["task_id"])
                    self._save_state(state)
                    sent_messages += len(batch)
                    sent_batches += 1
        status = "degraded" if failures else "ok"
        metric = self._run_metric(
            "extraction",
            project_id,
            started_at,
            started_clock,
            status=status,
            conversations=len(seen),
            sent_messages=sent_messages,
            sent_batches=sent_batches,
            unchanged_conversations=unchanged,
            regenerated_conversations=regenerated,
            failure_count=len(failures),
        )
        self._record_run(state, metric)
        return {
            "project_id": project_id,
            "mode": "shadow",
            "status": status,
            "fail_open": bool(self.config.fail_open),
            "conversations": len(seen),
            "sent_messages": sent_messages,
            "sent_batches": sent_batches,
            "unchanged_conversations": unchanged,
            "regenerated_conversations": regenerated,
            "failures": failures,
            "metrics": metric,
        }

    def sync_all(self) -> Dict[str, Any]:
        if not self.config.enabled:
            raise RuntimeError("MemoryCore connection is disabled")
        results: List[Dict[str, Any]] = []
        failures: List[Dict[str, Any]] = []
        for project in self.service.projects():
            if project.get("ignored") or not project.get("sync_enabled", True):
                continue
            path = Path(str(project["canonical_root"]))
            try:
                result = self.sync_project(path)
                results.append(result)
                failures.extend(
                    {"project": str(path), **failure}
                    for failure in result.get("failures", [])
                    if isinstance(failure, dict)
                )
            except Exception as exc:
                failures.append({"project": str(path), "error": str(exc)})
        return {
            "projects": len(results),
            "sent_messages": sum(int(item.get("sent_messages", 0)) for item in results),
            "sent_batches": sum(int(item.get("sent_batches", 0)) for item in results),
            "failures": failures,
            "results": results,
        }

    def task_layers(self, project_path: Path, task_id: str) -> Dict[str, Any]:
        if self.config.operation_mode != "shadow":
            raise RuntimeError("only shadow MemoryCore operation is currently supported")
        started_at = datetime.now(timezone.utc).isoformat()
        started_clock = time.perf_counter()
        project_path = project_path.expanduser().resolve()
        detail = self.service.task(project_path, task_id)
        state = self._load_state()
        project_id = self.service.project(project_path).project_id
        project_state = state.get("projects", {}).get(project_id, {})
        conversation_state = project_state.get("conversations", {})
        message_map = project_state.get("message_map", {})
        ids = [str(detail.get("primary_conversation_id") or "")]
        ids.extend(
            str(member.get("conversation_id") or "")
            for member in list(detail.get("source_copies", [])) + list(detail.get("subagents", []))
        )
        sessions = [
            session
            for conversation_id in dict.fromkeys(ids)
            for session in conversation_state.get(conversation_id, {}).get("sessions", [])
        ]
        failures: List[Dict[str, Any]] = []
        try:
            client = self.client(project_path)
        except Exception as exc:
            if not self.config.fail_open:
                raise
            client = None
            failures.append(self._failure("connect", exc))
        atomic: List[Any] = []
        for session in sessions:
            if client is None:
                break
            try:
                value = client.query_atomic(session_id=session, limit=self.config.recall_top_k)
            except Exception as exc:
                if not self.config.fail_open:
                    raise
                failures.append(self._failure("query_atomic", exc, session_id=session))
                continue
            items = value.get("items", value.get("memories", [])) if isinstance(value, dict) else value
            for item in items if isinstance(items, list) else []:
                enriched = dict(item) if isinstance(item, dict) else {"content": item}
                source_ids = enriched.get("source_message_ids") or []
                enriched["qmemory_source_refs"] = [
                    message_map[value] for value in source_ids if value in message_map
                ]
                atomic.append(enriched)
        scenarios: Any = {"items": []}
        core: Any = {}
        if client is not None:
            try:
                scenarios = client.list_scenarios()
            except Exception as exc:
                if not self.config.fail_open:
                    raise
                failures.append(self._failure("list_scenarios", exc))
            try:
                core = client.read_core()
            except Exception as exc:
                if not self.config.fail_open:
                    raise
                failures.append(self._failure("read_core", exc))
        referenced = sum(1 for item in atomic if item.get("qmemory_source_refs"))
        status = "degraded" if failures else "ok"
        metric = self._run_metric(
            "recall",
            project_id,
            started_at,
            started_clock,
            status=status,
            task_id=task_id,
            session_count=len(sessions),
            candidate_count=len(atomic),
            provenance_count=referenced,
            provenance_coverage=round(referenced / len(atomic), 6) if atomic else 1.0,
            top_k=int(self.config.recall_top_k),
            failure_count=len(failures),
        )
        self._record_run(state, metric)
        return {
            "task_id": task_id,
            "sessions": sessions,
            "l1": atomic,
            "l2": scenarios,
            "l3": core,
            "processor": "TencentDB MemoryCore",
            "read_only": True,
            "mode": "shadow",
            "status": status,
            "fail_open": bool(self.config.fail_open),
            "candidate_plan": {
                "items": atomic,
                "writes_to_qmemory": False,
                "automatic_confirmation": False,
            },
            "recall_plan": {
                "top_k": int(self.config.recall_top_k),
                "automatic_injection": False,
                "requires_explicit_consumer_action": True,
            },
            "failures": failures,
            "metrics": metric,
        }


class TencentMemoryCoreLab:
    """One-way, replaceable bridge from QMemory's archive to a lab MemoryCore.

    The bridge only reads QMemory through public service methods. It never receives a path to
    QMemory's raw/events/state directories and therefore cannot mutate the canonical archive.
    """

    def __init__(self, service: MemoryService) -> None:
        self.service = service

    def export_project(
        self,
        project_path: Path,
        output_dir: Path,
        *,
        max_messages: int = MAX_MEMORYCORE_MESSAGES,
    ) -> Dict[str, Any]:
        if max_messages < 1 or max_messages > MAX_MEMORYCORE_MESSAGES:
            raise ValueError("max_messages must be between 1 and 100")
        project_path = project_path.expanduser().resolve()
        output_dir = output_dir.expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        exported: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for task in self.service.tasks(project_path, limit=10_000):
            detail = self.service.task(project_path, str(task["task_id"]))
            members = list(detail.get("source_copies", [])) + list(detail.get("subagents", []))
            primary_id = str(detail.get("primary_conversation_id") or "")
            if primary_id and not any(str(member.get("conversation_id")) == primary_id for member in members):
                members.insert(0, {"conversation_id": primary_id})
            for member in members:
                conversation_id = str(member.get("conversation_id") or "")
                if not conversation_id or conversation_id in seen:
                    continue
                seen.add(conversation_id)
                conversation = (
                    detail.get("primary_conversation")
                    if conversation_id == primary_id
                    else self.service.conversation(project_path, conversation_id, redact=True)
                )
                if not isinstance(conversation, dict):
                    continue
                messages = self._messages(conversation.get("messages", []))
                for part, batch in enumerate(self._batches(messages, max_messages), start=1):
                    session_id = "qmemory-%s-p%03d" % (_stable_segment(conversation_id), part)
                    payload = {
                        "session_id": session_id,
                        "messages": [
                            {"role": message["role"], "content": message["content"]}
                            for message in batch
                        ],
                    }
                    file_name = "%s.json" % session_id
                    payload_bytes = _json_bytes(payload)
                    (output_dir / file_name).write_bytes(payload_bytes)
                    exported.append(
                        {
                            "file": file_name,
                            "sha256": hashlib.sha256(payload_bytes).hexdigest(),
                            "task_id": str(task["task_id"]),
                            "task_title": str(task.get("title") or ""),
                            "conversation_id": conversation_id,
                            "source_kind": str(member.get("source_kind") or ""),
                            "session_id": session_id,
                            "message_count": len(batch),
                            "sequences": [message["sequence"] for message in batch],
                        }
                    )

        manifest = {
            "schema_version": 1,
            "format": "tencentdb-memorycore-v3-conversation-add",
            "project_root": str(project_path),
            "max_messages_per_batch": max_messages,
            "conversation_count": len(seen),
            "batch_count": len(exported),
            "message_count": sum(item["message_count"] for item in exported),
            "batches": exported,
        }
        manifest_bytes = _json_bytes(manifest)
        (output_dir / "manifest.json").write_bytes(manifest_bytes)
        return {**manifest, "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest()}

    @staticmethod
    def _messages(raw_messages: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = []
        for raw in raw_messages:
            role = str(raw.get("role") or "")
            text = str(raw.get("text") or raw.get("content") or "").strip()
            if role not in ("user", "assistant") or not text:
                continue
            messages.append(
                {
                    "role": role,
                    "content": text,
                    "sequence": int(raw.get("sequence") or len(messages) + 1),
                }
            )
        return messages

    @staticmethod
    def _batches(messages: List[Dict[str, Any]], size: int) -> Iterable[List[Dict[str, Any]]]:
        for offset in range(0, len(messages), size):
            yield messages[offset : offset + size]

    def import_export(
        self,
        export_dir: Path,
        identity: MemoryCoreIdentity,
        *,
        base_url: str = "http://127.0.0.1:8420",
        api_key: Optional[str] = None,
        timeout: float = 15.0,
    ) -> Dict[str, Any]:
        parsed = urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("the isolation POC only permits a localhost HTTP MemoryCore endpoint")
        export_dir = export_dir.expanduser().resolve()
        manifest = json.loads((export_dir / "manifest.json").read_text(encoding="utf-8"))
        results = []
        for item in manifest["batches"]:
            payload = json.loads((export_dir / item["file"]).read_text(encoding="utf-8"))
            payload.update(
                {
                    "team_id": identity.team_id,
                    "user_id": identity.user_id,
                    "agent_id": identity.agent_id,
                }
            )
            headers = {
                "Content-Type": "application/json",
                "x-tdai-service-id": identity.service_id,
            }
            if api_key:
                headers["Authorization"] = "Bearer %s" % api_key
            request = urllib.request.Request(
                base_url.rstrip("/") + "/v3/conversation/add",
                data=_json_bytes(payload),
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    body = json.loads(response.read().decode("utf-8"))
            except urllib.error.URLError as exc:
                raise RuntimeError("MemoryCore import failed for %s: %s" % (item["file"], exc)) from exc
            results.append({"file": item["file"], "response": body})
        return {"imported_batches": len(results), "results": results}
