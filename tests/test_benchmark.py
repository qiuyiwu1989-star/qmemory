from __future__ import annotations

import json
from pathlib import Path

import pytest

from qmemory.benchmark import (
    build_benchmark_template,
    evaluate_benchmark,
    evaluate_benchmark_file,
    write_benchmark_report,
)


FIXTURE = Path(__file__).parent / "fixtures" / "memorycore_benchmark_v1.json"


def test_benchmark_fixture_produces_machine_readable_quality_report(tmp_path: Path) -> None:
    report = evaluate_benchmark_file(FIXTURE)

    assert report["schema_version"] == 1
    assert report["benchmark_id"] == "memorycore-shadow-smoke-v1"
    assert report["summary"]["candidate_count"] == 4
    assert report["summary"]["provenance_coverage"] == 0.75
    assert report["summary"]["atom_duplicate_count"] == 1
    assert report["summary"]["atom_duplicate_rate"] == 0.25
    assert report["summary"]["unsupported_count"] == 1
    assert report["summary"]["atom_precision"] == 0.75
    assert report["summary"]["atom_recall"] == 1.0
    assert report["summary"]["recall"]["hit_at_5_rate"] == 0.5
    assert report["summary"]["recall"]["recall_at_5"] == 0.5
    assert report["summary"]["recall"]["latency_p95_ms"] == 310.0
    assert report["passed"] is False

    output = tmp_path / "report.json"
    write_benchmark_report(report, output)
    assert json.loads(output.read_text(encoding="utf-8")) == report


def test_benchmark_rejects_unversioned_or_empty_documents() -> None:
    with pytest.raises(ValueError, match="schema_version"):
        evaluate_benchmark({"cases": [{}]})
    with pytest.raises(ValueError, match="at least one case"):
        evaluate_benchmark({"schema_version": 1, "cases": []})


def test_clean_benchmark_can_pass_all_default_gates() -> None:
    report = evaluate_benchmark(
        {
            "schema_version": 1,
            "cases": [
                {
                    "case_id": "clean",
                    "evidence_refs": ["codex://c#message-1"],
                    "gold_atoms": [{"statement": "保留原始证据"}],
                    "candidate_atoms": [
                        {
                            "statement": "保留原始证据",
                            "source_refs": ["codex://c#message-1"],
                        }
                    ],
                    "relevant_memory_ids": ["m1"],
                    "retrieved": ["m1"],
                    "latency_ms": 20,
                }
            ],
        }
    )
    assert report["passed"] is True
    assert all(gate["passed"] for gate in report["gates"].values())


def test_benchmark_template_freezes_distinct_representative_tasks() -> None:
    records = [
        {
            "project_id": "p1",
            "project_root": "/p1",
            "task_id": "single",
            "title": "Single",
            "sources": ["codex"],
            "subagent_count": 0,
            "unique_readable_message_count": 20,
        },
        {
            "project_id": "p1",
            "project_root": "/p1",
            "task_id": "cross",
            "title": "Cross",
            "sources": ["codex", "claude-code"],
            "subagent_count": 0,
            "unique_readable_message_count": 40,
        },
        {
            "project_id": "p2",
            "project_root": "/p2",
            "task_id": "branch",
            "title": "Branch",
            "sources": ["codex"],
            "subagent_count": 2,
            "unique_readable_message_count": 120,
        },
    ]
    template = build_benchmark_template(records, per_cohort=1)

    assert len(template["cases"]) == 3
    assert {case["cohort"] for case in template["cases"]} == {
        "single_agent",
        "cross_agent",
        "large_or_branched",
    }
    assert all(case["labeling_status"] == "pending" for case in template["cases"])
    assert all(case["gold_atoms"] == [] for case in template["cases"])
