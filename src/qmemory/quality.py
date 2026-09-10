from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


@dataclass(frozen=True)
class QualityThresholds:
    """Release gates for the first measurable QMemory value loop."""

    extraction_coverage: float = 0.95
    primary_coverage: float = 0.95
    min_recall_impressions: int = 100
    min_feedback_ratings: int = 30
    helpful_rate: float = 0.70


def _normal_title(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().casefold())


def _gate(name: str, passed: bool, actual: Any, target: str, detail: str) -> Dict[str, Any]:
    return {
        "name": name,
        "status": "passed" if passed else "not_ready",
        "actual": actual,
        "target": target,
        "detail": detail,
    }


def build_quality_report(
    service: Any,
    *,
    project_path: Optional[Path] = None,
    thresholds: QualityThresholds = QualityThresholds(),
) -> Dict[str, Any]:
    """Build a deterministic, machine-readable release report from public service APIs.

    The report deliberately contains counts and identifiers, not conversation text.  It is
    suitable for the desktop quality panel, CLI automation, and release acceptance checks.
    """
    all_projects = list(service.projects())
    if project_path is not None:
        expected = service.project(project_path).project_id
        selected = [item for item in all_projects if str(item.get("project_id")) == expected]
        if not selected:
            selected = [service.register_project(project_path)]
    else:
        selected = all_projects

    project_reports: List[Dict[str, Any]] = []
    title_locations: Dict[tuple[str, str], List[Dict[str, str]]] = defaultdict(list)
    canonical_locations: Dict[str, List[str]] = defaultdict(list)
    total_tasks = 0
    coverage_below_target = 0
    coverage_below_safe_default = 0
    content_gap_tasks = 0
    extraction_total = 0
    extraction_done = 0
    extraction_pending = 0
    extraction_failed = 0

    for project in selected:
        raw_root = str(project.get("canonical_root") or "")
        root = Path(raw_root) if raw_root else Path("/__qmemory_missing_project_root__")
        project_id = str(project.get("project_id") or "")
        if raw_root:
            canonical_locations[str(root.expanduser().resolve())].append(project_id)
        tasks: List[Dict[str, Any]] = []
        insight: Dict[str, int] = {"total": 0, "extracted": 0, "pending": 0, "failed": 0}
        error: Optional[str] = None
        try:
            if not raw_root:
                raise ValueError("project has no canonical root")
            tasks = list(service.tasks(root, limit=10_000))
            insight = dict(service.insight_stats(root))
        except (OSError, RuntimeError, ValueError) as exc:
            error = type(exc).__name__

        total_tasks += len(tasks)
        for task in tasks:
            coverage = float(task.get("primary_coverage", 1.0))
            coverage_below_target += int(coverage < thresholds.primary_coverage)
            coverage_below_safe_default += int(coverage < 0.80)
            content_gap_tasks += int(bool(task.get("has_content_gap")))
            title = _normal_title(str(task.get("title") or ""))
            if title:
                title_locations[(project_id, title)].append(
                    {"project_id": project_id, "task_id": str(task.get("task_id") or "")}
                )

        extraction_total += int(insight.get("total", 0))
        extraction_done += int(insight.get("extracted", 0))
        extraction_pending += int(insight.get("pending", 0))
        extraction_failed += int(insight.get("failed", 0))
        project_reports.append(
            {
                "project_id": project_id,
                "display_name": str(project.get("display_name") or ""),
                "canonical_root": raw_root,
                "tasks": len(tasks),
                "insights": insight,
                "error": error,
            }
        )

    duplicate_titles = [
        {"project_id": key[0], "title": key[1], "tasks": locations}
        for key, locations in title_locations.items()
        if len(locations) > 1
    ]
    duplicate_project_roots = {
        root: ids for root, ids in canonical_locations.items() if root and len(set(ids)) > 1
    }
    extraction_coverage = extraction_done / extraction_total if extraction_total else 0.0
    retrieval = dict(service.retrieval_stats(project_path))
    doctor = dict(service.doctor())
    helpful_rate = retrieval.get("helpful_rate")
    gates = [
        _gate(
            "ledger_integrity",
            bool(doctor.get("ok")),
            bool(doctor.get("ok")),
            "true",
            "事件链、投影与冲突隔离必须全部健康",
        ),
        _gate(
            "project_identity",
            not duplicate_project_roots and not any(item["error"] for item in project_reports),
            len(duplicate_project_roots),
            "0 duplicate canonical roots",
            "同一 canonical root 只能属于一个项目，且项目必须可读取",
        ),
        _gate(
            "task_deduplication",
            not duplicate_titles and coverage_below_safe_default == 0,
            {"exact_title_clusters": len(duplicate_titles), "coverage_below_80": coverage_below_safe_default},
            "0 unresolved clusters and 0 unsafe primary-only tasks",
            "同标题簇需清零；覆盖不足的任务不能只显示主会话",
        ),
        _gate(
            "extraction_coverage",
            extraction_total > 0
            and extraction_coverage >= thresholds.extraction_coverage
            and extraction_failed == 0,
            round(extraction_coverage, 4),
            ">= %.0f%% and 0 failed" % (thresholds.extraction_coverage * 100),
            "任务应被提炼或明确跳过，不能永久积压",
        ),
        _gate(
            "recall_observation",
            int(retrieval.get("impressions", 0)) >= thresholds.min_recall_impressions,
            int(retrieval.get("impressions", 0)),
            ">= %d impressions" % thresholds.min_recall_impressions,
            "先积累影子召回样本，再开启自动注入",
        ),
        _gate(
            "feedback_quality",
            int(retrieval.get("rated", 0)) >= thresholds.min_feedback_ratings
            and helpful_rate is not None
            and float(helpful_rate) >= thresholds.helpful_rate,
            {"rated": int(retrieval.get("rated", 0)), "helpful_rate": helpful_rate},
            ">= %d ratings and helpful >= %.0f%%"
            % (thresholds.min_feedback_ratings, thresholds.helpful_rate * 100),
            "召回是否有帮助必须形成可量化反馈",
        ),
    ]
    return {
        "schema_version": 1,
        "release_ready": all(item["status"] == "passed" for item in gates),
        "scope": str(project_path.expanduser().resolve()) if project_path else "all-projects",
        "thresholds": asdict(thresholds),
        "summary": {
            "usage": service.usage_stats(project_path) if hasattr(service, 'usage_stats') else {'available': False},
            "projects": len(selected),
            "tasks": total_tasks,
            "content_gap_tasks": content_gap_tasks,
            "primary_coverage_below_target": coverage_below_target,
            "primary_coverage_below_80": coverage_below_safe_default,
            "exact_title_clusters": len(duplicate_titles),
            "duplicate_project_roots": len(duplicate_project_roots),
            "extraction_total": extraction_total,
            "extraction_done": extraction_done,
            "extraction_pending": extraction_pending,
            "extraction_failed": extraction_failed,
            "extraction_coverage": round(extraction_coverage, 4),
            "retrieval": retrieval,
        },
        "gates": gates,
        "project_reports": project_reports,
        "review_queues": {
            "duplicate_titles": duplicate_titles,
            "duplicate_project_roots": duplicate_project_roots,
        },
    }
