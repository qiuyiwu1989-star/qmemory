from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4

from .redaction import redact_text, redact_value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_sync_run_id() -> str:
    """Create a non-semantic correlation id safe to expose in sync status."""

    return "sync_%s" % uuid4().hex


@dataclass
class _SourceCounts:
    found: int = 0
    imported: int = 0
    unchanged: int = 0
    failures: int = 0
    messages_indexed: int = 0
    bytes_copied: int = 0

    def as_dict(self) -> Dict[str, int]:
        return {
            "found": self.found,
            "imported": self.imported,
            "unchanged": self.unchanged,
            "failures": self.failures,
            "messages_indexed": self.messages_indexed,
            "bytes_copied": self.bytes_copied,
        }


@dataclass
class SyncRunTracker:
    """Accumulate one idempotent coding-agent archive sync run.

    File paths are not retained. Error samples are redacted and bounded before
    becoming status output, so telemetry cannot become a second secret store.
    """

    sync_run_id: str = field(default_factory=new_sync_run_id)
    started_at: str = field(default_factory=_now)
    max_error_samples: int = 10
    _by_source: Dict[str, _SourceCounts] = field(default_factory=dict)
    _errors: list[Dict[str, str]] = field(default_factory=list)

    def _source(self, source_kind: str) -> _SourceCounts:
        safe_source, _ = redact_text(str(source_kind))
        return self._by_source.setdefault(safe_source, _SourceCounts())

    def record_found(self, source_kind: str, count: int = 1) -> None:
        self._source(source_kind).found += max(0, int(count))

    def record_imported(
        self,
        source_kind: str,
        *,
        messages_indexed: int = 0,
        bytes_copied: int = 0,
        count: int = 1,
    ) -> None:
        source = self._source(source_kind)
        source.imported += max(0, int(count))
        source.messages_indexed += max(0, int(messages_indexed))
        source.bytes_copied += max(0, int(bytes_copied))

    def record_unchanged(self, source_kind: str, count: int = 1) -> None:
        self._source(source_kind).unchanged += max(0, int(count))

    def record_failure(self, source_kind: str, error: Optional[BaseException] = None) -> None:
        safe_source, _ = redact_text(str(source_kind))
        self._source(source_kind).failures += 1
        if error is not None and len(self._errors) < max(0, self.max_error_samples):
            safe_error, _ = redact_text(str(error))
            self._errors.append(
                {
                    "source": safe_source,
                    "error_type": type(error).__name__,
                    "message": safe_error[:500],
                }
            )

    def snapshot(self, **extra: Any) -> Dict[str, Any]:
        by_source = {
            source: counts.as_dict()
            for source, counts in sorted(self._by_source.items())
        }
        totals = {
            field_name: sum(values[field_name] for values in by_source.values())
            for field_name in (
                "found",
                "imported",
                "unchanged",
                "failures",
                "messages_indexed",
                "bytes_copied",
            )
        }
        result: Dict[str, Any] = {
            "sync_run_id": self.sync_run_id,
            "started_at": self.started_at,
            "finished_at": _now(),
            **totals,
            "by_source": by_source,
        }
        if self._errors:
            result["errors"] = list(self._errors)
        result.update(extra)
        safe_result, _ = redact_value(result)
        return safe_result
