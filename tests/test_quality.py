from __future__ import annotations

from pathlib import Path

from qmemory.cli import build_parser
from qmemory.quality import QualityThresholds, build_quality_report
from qmemory.service import MemoryService


class _Project:
    project_id = "project-1"


class FakeQualityService:
    def __init__(self, root: Path) -> None:
        self.root = root

    def projects(self):
        return [
            {
                "project_id": "project-1",
                "display_name": "QMemory",
                "canonical_root": str(self.root),
            }
        ]

    def project(self, path: Path):
        return _Project()

    def register_project(self, path: Path):
        return self.projects()[0]

    def tasks(self, path: Path, limit: int = 10_000):
        return [
            {
                "task_id": "task-1",
                "title": "通过验收",
                "primary_coverage": 1.0,
                "has_content_gap": False,
            }
        ]

    def insight_stats(self, path: Path):
        return {"total": 1, "extracted": 1, "pending": 0, "failed": 0}

    def retrieval_stats(self, path=None):
        return {
            "impressions": 2,
            "feedback": {"helpful": 1, "ignored": 0, "rejected": 0},
            "rated": 1,
            "helpful_rate": 1.0,
            "feedback_coverage": 0.5,
        }

    def doctor(self):
        return {"ok": True}


def test_quality_report_is_machine_readable_and_can_pass(tmp_path: Path) -> None:
    service = FakeQualityService(tmp_path)
    report = build_quality_report(
        service,
        thresholds=QualityThresholds(min_recall_impressions=2, min_feedback_ratings=1),
    )
    assert report["schema_version"] == 1
    assert report["release_ready"] is True
    assert report["summary"]["extraction_coverage"] == 1.0
    assert {gate["name"] for gate in report["gates"]} == {
        "ledger_integrity",
        "project_identity",
        "task_deduplication",
        "extraction_coverage",
        "recall_observation",
        "feedback_quality",
    }


def test_retrieval_stats_close_the_observation_loop(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    service = MemoryService(tmp_path / "home", device_id="quality-device")
    memory = service.remember(
        project,
        "decision",
        "Use evidence-first memory",
        confirmed=True,
    )
    service.context_pack(project, "evidence")
    service.feedback(project, memory["memory_id"], "helpful")

    stats = service.retrieval_stats(project)
    assert stats["impressions"] == 1
    assert stats["feedback"] == {"helpful": 1, "ignored": 0, "rejected": 0}
    assert stats["helpful_rate"] == 1.0
    assert stats["feedback_coverage"] == 1.0


def test_quality_cli_supports_global_and_project_scopes() -> None:
    parser = build_parser()
    assert parser.parse_args(["quality-report", "--all-projects"]).command == "quality-report"
    args = parser.parse_args(["quality-report", "--project", "/tmp/qmemory"])
    assert args.project == Path("/tmp/qmemory")
    assert parser.parse_args(["retrieval-stats"]).project is None
    memorycore = parser.parse_args(
        ["memorycore-config", "--enable", "--mode", "shadow", "--recall-top-k", "5"]
    )
    assert memorycore.mode == "shadow"
    assert memorycore.recall_top_k == 5
    benchmark = parser.parse_args(["memorycore-benchmark", "fixture.json"])
    assert benchmark.fixture == Path("fixture.json")
    template = parser.parse_args(
        ["memorycore-benchmark-template", "--output", "gold.json"]
    )
    assert template.output == Path("gold.json")
    assert template.per_cohort == 10
