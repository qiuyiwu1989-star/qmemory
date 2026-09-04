from __future__ import annotations

import json
import math
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Set

from .redaction import redact_text, redact_value


BENCHMARK_SCHEMA_VERSION = 1

DEFAULT_THRESHOLDS: Dict[str, float] = {
    "provenance_coverage_min": 1.0,
    "atom_duplicate_rate_max": 0.05,
    "unsupported_rate_max": 0.0,
    "recall_hit_at_5_min": 0.70,
    "recall_p95_latency_ms_max": 300.0,
}


def build_benchmark_template(
    task_records: Iterable[Mapping[str, Any]], *, per_cohort: int = 10
) -> Dict[str, Any]:
    """Freeze a deterministic single/cross-Agent/large-task labelling set."""
    if per_cohort < 1:
        raise ValueError("per_cohort must be positive")
    records = [dict(item) for item in task_records]
    records.sort(
        key=lambda item: (
            str(item.get("updated_at") or ""),
            str(item.get("project_id") or ""),
            str(item.get("task_id") or ""),
        ),
        reverse=True,
    )
    cohorts = {
        "single_agent": [
            item
            for item in records
            if len(item.get("sources") or []) <= 1 and int(item.get("subagent_count", 0)) == 0
        ],
        "cross_agent": [item for item in records if len(item.get("sources") or []) > 1],
        "large_or_branched": sorted(
            [
                item
                for item in records
                if int(item.get("subagent_count", 0)) > 0
                or int(item.get("unique_readable_message_count", 0)) >= 100
            ],
            key=lambda item: (
                int(item.get("subagent_count", 0)),
                int(item.get("unique_readable_message_count", 0)),
                str(item.get("updated_at") or ""),
            ),
            reverse=True,
        ),
    }
    selected: List[Dict[str, Any]] = []
    used: Set[str] = set()
    for cohort, candidates in cohorts.items():
        count = 0
        for item in candidates:
            identity = "%s:%s" % (item.get("project_id", ""), item.get("task_id", ""))
            if identity in used:
                continue
            used.add(identity)
            selected.append({**item, "cohort": cohort})
            count += 1
            if count >= per_cohort:
                break

    cases = [
        {
            "case_id": "gold-%03d" % position,
            "cohort": item["cohort"],
            "project_id": str(item.get("project_id") or ""),
            "project_root": str(item.get("project_root") or ""),
            "task_id": str(item.get("task_id") or ""),
            "task_title": redact_text(str(item.get("title") or ""))[0],
            "source_conversation_ids": list(item.get("source_conversation_ids") or []),
            "evidence_refs": [],
            "gold_atoms": [],
            "candidate_atoms": [],
            "relevant_memory_ids": [],
            "retrieved": [],
            "latency_ms": 0.0,
            "labeling_status": "pending",
        }
        for position, item in enumerate(selected, start=1)
    ]
    return {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "benchmark_id": "qmemory-real-gold-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "selection_policy": {
            "per_cohort": per_cohort,
            "cohorts": ["single_agent", "cross_agent", "large_or_branched"],
            "deduplicated_across_cohorts": True,
        },
        "thresholds": dict(DEFAULT_THRESHOLDS),
        "cases": cases,
    }


def _normalise_statement(value: str) -> str:
    normalised = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        character
        for character in normalised
        if unicodedata.category(character)[0] in ("L", "N")
    )


def _source_refs(atom: Mapping[str, Any]) -> List[str]:
    raw = atom.get("source_refs") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(value) for value in raw if str(value)] if isinstance(raw, list) else []


def _result_ids(raw: Any) -> List[str]:
    if not isinstance(raw, list):
        return []
    return [
        str(item.get("memory_id") or item.get("id") or "") if isinstance(item, dict) else str(item)
        for item in raw
        if (isinstance(item, dict) and (item.get("memory_id") or item.get("id")))
        or (not isinstance(item, dict) and str(item))
    ]


def _safe_rate(numerator: int, denominator: int, *, empty: float = 1.0) -> float:
    return round(numerator / denominator, 6) if denominator else empty


def _percentile_nearest_rank(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 3)


def _gold_norms(raw_gold: Any) -> Set[str]:
    norms: Set[str] = set()
    for item in raw_gold if isinstance(raw_gold, list) else []:
        if isinstance(item, str):
            statements: Iterable[Any] = [item]
        elif isinstance(item, dict):
            statements = [item.get("statement", ""), *(item.get("aliases") or [])]
        else:
            statements = []
        norms.update(_normalise_statement(str(value)) for value in statements if str(value).strip())
    return norms


def _gate(actual: float, threshold: float, comparison: str) -> Dict[str, Any]:
    passed = actual >= threshold if comparison == "min" else actual <= threshold
    return {
        "actual": actual,
        "threshold": threshold,
        "comparison": comparison,
        "passed": passed,
    }


def evaluate_benchmark(document: Mapping[str, Any]) -> Dict[str, Any]:
    """Evaluate a deterministic, labelled MemoryCore shadow-run fixture.

    This is deliberately an offline evaluator: it never calls MemoryCore and never mutates
    QMemory. Semantic support remains a human/LLM labelling concern; this harness verifies that
    every emitted atom points to fixture evidence and honours an explicit ``supported=false``
    label when present.
    """

    if int(document.get("schema_version", 0)) != BENCHMARK_SCHEMA_VERSION:
        raise ValueError("unsupported benchmark schema_version")
    raw_cases = document.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("benchmark requires at least one case")

    totals = {
        "candidates": 0,
        "with_source_refs": 0,
        "valid_provenance": 0,
        "duplicates": 0,
        "unsupported": 0,
        "gold_atoms": 0,
        "matched_candidates": 0,
        "matched_gold_atoms": 0,
        "recall_queries": 0,
        "recall_hits_at_5": 0,
        "relevant_items": 0,
        "relevant_items_at_5": 0,
    }
    latencies: List[float] = []
    case_reports: List[Dict[str, Any]] = []

    for position, raw_case in enumerate(raw_cases, start=1):
        if not isinstance(raw_case, dict):
            raise ValueError("benchmark case %d must be an object" % position)
        case_id = str(raw_case.get("case_id") or "case-%d" % position)
        evidence_refs = {str(value) for value in (raw_case.get("evidence_refs") or []) if str(value)}
        candidates = raw_case.get("candidate_atoms") or []
        if not isinstance(candidates, list):
            raise ValueError("candidate_atoms for %s must be a list" % case_id)

        seen_norms: Set[str] = set()
        duplicate_count = 0
        unsupported_count = 0
        with_source_refs = 0
        valid_provenance = 0
        matched_candidates = 0
        matched_gold: Set[str] = set()
        gold_norms = _gold_norms(raw_case.get("gold_atoms"))

        for raw_atom in candidates:
            atom = raw_atom if isinstance(raw_atom, dict) else {"statement": str(raw_atom)}
            statement_norm = _normalise_statement(str(atom.get("statement") or atom.get("content") or ""))
            if statement_norm in seen_norms and statement_norm:
                duplicate_count += 1
            seen_norms.add(statement_norm)

            refs = _source_refs(atom)
            if refs:
                with_source_refs += 1
            has_valid_provenance = bool(refs) and bool(evidence_refs) and set(refs).issubset(evidence_refs)
            if has_valid_provenance:
                valid_provenance += 1
            is_unsupported = atom.get("supported") is False or not has_valid_provenance
            if is_unsupported:
                unsupported_count += 1
            if statement_norm and statement_norm in gold_norms:
                matched_candidates += 1
                matched_gold.add(statement_norm)

        relevant_ids = {str(value) for value in (raw_case.get("relevant_memory_ids") or []) if str(value)}
        retrieved_ids = _result_ids(raw_case.get("retrieved"))[:5]
        relevant_at_5 = relevant_ids.intersection(retrieved_ids)
        has_recall_query = bool(relevant_ids)
        latency_ms = float(raw_case.get("latency_ms", 0.0))
        if has_recall_query:
            latencies.append(latency_ms)

        case_report = {
            "case_id": case_id,
            "candidate_count": len(candidates),
            "source_ref_presence_rate": _safe_rate(with_source_refs, len(candidates)),
            "provenance_coverage": _safe_rate(valid_provenance, len(candidates)),
            "atom_duplicate_count": duplicate_count,
            "atom_duplicate_rate": _safe_rate(duplicate_count, len(candidates), empty=0.0),
            "unsupported_count": unsupported_count,
            "unsupported_rate": _safe_rate(unsupported_count, len(candidates), empty=0.0),
            "atom_precision": _safe_rate(matched_candidates, len(candidates)),
            "atom_recall": _safe_rate(len(matched_gold), len(gold_norms)),
            "recall": {
                "top_k": 5,
                "relevant_count": len(relevant_ids),
                "relevant_at_5": len(relevant_at_5),
                "hit_at_5": bool(relevant_at_5) if has_recall_query else None,
                "recall_at_5": _safe_rate(len(relevant_at_5), len(relevant_ids), empty=0.0)
                if has_recall_query
                else None,
                "latency_ms": round(latency_ms, 3) if has_recall_query else None,
            },
        }
        case_reports.append(case_report)

        totals["candidates"] += len(candidates)
        totals["with_source_refs"] += with_source_refs
        totals["valid_provenance"] += valid_provenance
        totals["duplicates"] += duplicate_count
        totals["unsupported"] += unsupported_count
        totals["gold_atoms"] += len(gold_norms)
        totals["matched_candidates"] += matched_candidates
        totals["matched_gold_atoms"] += len(matched_gold)
        if has_recall_query:
            totals["recall_queries"] += 1
            totals["recall_hits_at_5"] += int(bool(relevant_at_5))
            totals["relevant_items"] += len(relevant_ids)
            totals["relevant_items_at_5"] += len(relevant_at_5)

    summary = {
        "case_count": len(case_reports),
        "candidate_count": totals["candidates"],
        "source_ref_presence_rate": _safe_rate(totals["with_source_refs"], totals["candidates"]),
        "provenance_coverage": _safe_rate(totals["valid_provenance"], totals["candidates"]),
        "atom_duplicate_count": totals["duplicates"],
        "atom_duplicate_rate": _safe_rate(totals["duplicates"], totals["candidates"], empty=0.0),
        "unsupported_count": totals["unsupported"],
        "unsupported_rate": _safe_rate(totals["unsupported"], totals["candidates"], empty=0.0),
        "atom_precision": _safe_rate(totals["matched_candidates"], totals["candidates"]),
        "atom_recall": _safe_rate(totals["matched_gold_atoms"], totals["gold_atoms"]),
        "recall": {
            "query_count": totals["recall_queries"],
            "hit_at_5_rate": _safe_rate(
                totals["recall_hits_at_5"], totals["recall_queries"], empty=0.0
            ),
            "recall_at_5": _safe_rate(
                totals["relevant_items_at_5"], totals["relevant_items"], empty=0.0
            ),
            "latency_mean_ms": round(sum(latencies) / len(latencies), 3) if latencies else 0.0,
            "latency_p95_ms": _percentile_nearest_rank(latencies, 0.95),
        },
    }

    thresholds = {**DEFAULT_THRESHOLDS, **dict(document.get("thresholds") or {})}
    gates = {
        "provenance_coverage": _gate(
            summary["provenance_coverage"], float(thresholds["provenance_coverage_min"]), "min"
        ),
        "atom_duplicate_rate": _gate(
            summary["atom_duplicate_rate"], float(thresholds["atom_duplicate_rate_max"]), "max"
        ),
        "unsupported_rate": _gate(
            summary["unsupported_rate"], float(thresholds["unsupported_rate_max"]), "max"
        ),
        "recall_hit_at_5": _gate(
            summary["recall"]["hit_at_5_rate"], float(thresholds["recall_hit_at_5_min"]), "min"
        ),
        "recall_p95_latency_ms": _gate(
            summary["recall"]["latency_p95_ms"],
            float(thresholds["recall_p95_latency_ms_max"]),
            "max",
        ),
    }
    return {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "benchmark_id": str(document.get("benchmark_id") or "memorycore-shadow"),
        "mode": "offline-shadow-evaluation",
        "matching_policy": "unicode-nfkc-alphanumeric-exact",
        "summary": summary,
        "gates": gates,
        "passed": all(item["passed"] for item in gates.values()),
        "cases": case_reports,
    }


def evaluate_benchmark_file(path: Path) -> Dict[str, Any]:
    return evaluate_benchmark(json.loads(path.read_text(encoding="utf-8")))


def write_benchmark_report(report: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    safe_report, _ = redact_value(dict(report))
    temporary.write_text(
        json.dumps(safe_report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
