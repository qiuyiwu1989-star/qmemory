from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .redaction import redact_text, redact_value
from .textio import run_text


@dataclass(frozen=True)
class AdapterStatus:
    name: str
    available: bool
    detail: str


class SuperLocalMemoryAdapter:
    """Optional derived projection into the local ``slm`` CLI.

    QMemory events remain canonical. This adapter is deliberately one-way so an
    SLM upgrade, reindex, or removal cannot corrupt synchronized memory history.
    """

    name = "superlocalmemory"

    def status(self) -> AdapterStatus:
        executable = shutil.which("slm")
        if not executable:
            return AdapterStatus(self.name, False, "slm executable not found")
        return AdapterStatus(self.name, True, executable)

    def project_memory(self, memory: Dict[str, Any]) -> Dict[str, Any]:
        status = self.status()
        if not status.available:
            raise RuntimeError(status.detail)
        envelope = {
            "qmemory_id": memory["memory_id"],
            "project_id": memory["project_id"],
            "type": memory["memory_type"],
            "holder": memory["holder"],
            "subject": memory["subject"],
            "statement": memory["statement"],
            "as_of": memory["as_of"],
            "source_ref": memory.get("source_ref"),
        }
        envelope, _ = redact_value(envelope)
        result = run_text(
            [status.detail, "remember", json.dumps(envelope, ensure_ascii=False), "--json"],
            stderr=subprocess.PIPE,
            timeout=60,
        )
        if result.returncode != 0:
            safe_error, _ = redact_text(result.stderr.strip())
            raise RuntimeError("SLM projection failed: %s" % safe_error)
        try:
            payload, _ = redact_value(json.loads(result.stdout))
            return payload
        except json.JSONDecodeError:
            safe_output, _ = redact_text(result.stdout.strip())
            return {"ok": True, "output": safe_output}
