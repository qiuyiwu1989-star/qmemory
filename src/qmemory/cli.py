from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from .adapters import SuperLocalMemoryAdapter
from .benchmark import (
    build_benchmark_template,
    evaluate_benchmark_file,
    write_benchmark_report,
)
from .events import EventValidationError, SyncConflictError
from .service import MEMORY_TYPES, MemoryService
from .textio import force_utf8_streams
from .tencent_memorycore import MemoryCoreBridge, MemoryCoreConfig, load_memorycore_api_key


def emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def add_home(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--home",
        type=Path,
        help="QMemory data directory (default: QMEMORY_HOME or ~/.local/share/qmemory)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qmemory", description="Local-first project memory")
    add_home(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    initialize = subparsers.add_parser("init", help="Initialize local storage")
    initialize.add_argument("--device-id")

    subparsers.add_parser("status", help="Show local memory status")
    subparsers.add_parser("doctor", help="Validate event chains and projection health")
    subparsers.add_parser("issues", help="List deterministic merge/lifecycle issues")
    subparsers.add_parser("rebuild", help="Rebuild SQLite from event shards")

    project = subparsers.add_parser("project", help="Resolve stable project identity")
    project.add_argument("path", nargs="?", type=Path, default=Path.cwd())
    bind_project = subparsers.add_parser(
        "bind-project", help="Explicitly bind one archived conversation to a project"
    )
    bind_project.add_argument("conversation_id")
    bind_project.add_argument("project", type=Path)
    subparsers.add_parser(
        "recompute-projects", help="Recompute derived project mappings without changing evidence"
    )

    remember = subparsers.add_parser("remember", help="Propose or confirm a memory")
    remember.add_argument("statement")
    remember.add_argument("--project", type=Path, default=Path.cwd())
    remember.add_argument("--type", choices=sorted(MEMORY_TYPES), default="fact")
    remember.add_argument("--subject", default="project")
    remember.add_argument("--holder", default="user")
    remember.add_argument("--confirmed", action="store_true")
    remember.add_argument("--source-ref")

    search = subparsers.add_parser("search", help="Search active project memory")
    search.add_argument("query", nargs="?", default="")
    search.add_argument("--project", type=Path, default=Path.cwd())
    search.add_argument("--include-proposed", action="store_true")
    search.add_argument("--limit", type=int, default=10)

    context = subparsers.add_parser("context", help="Build a bounded context pack")
    context.add_argument("query", nargs="?", default="")
    context.add_argument("--project", type=Path, default=Path.cwd())
    context.add_argument("--limit", type=int, default=12)

    subparsers.add_parser("codex-preview", help="Preview local Codex conversation archive")
    subparsers.add_parser(
        "sources-preview", help="Preview Codex and Claude Code conversation archives"
    )
    codex_sync = subparsers.add_parser(
        "codex-sync", help="Incrementally archive local Codex conversations"
    )
    codex_sync.add_argument("--codex-home", type=Path)
    sources_sync = subparsers.add_parser(
        "sources-sync", help="Archive Codex and Claude Code conversations"
    )
    sources_sync.add_argument("--codex-home", type=Path)
    sources_sync.add_argument("--claude-home", type=Path)
    conversations = subparsers.add_parser(
        "conversations", help="List archived conversations for a project"
    )
    conversations.add_argument("query", nargs="?", default="")
    conversations.add_argument("--project", type=Path, default=Path.cwd())
    conversations.add_argument("--limit", type=int, default=100)
    conversation = subparsers.add_parser(
        "conversation", help="Read one redacted archived conversation"
    )
    conversation.add_argument("conversation_id")
    conversation.add_argument("--project", type=Path, default=Path.cwd())
    insight_status = subparsers.add_parser(
        "insight-status", help="Show the conversation-to-memory pipeline status"
    )
    insight_status.add_argument("--project", type=Path, default=Path.cwd())
    quality_report = subparsers.add_parser(
        "quality-report", help="Evaluate project, dedupe, extraction, recall, and feedback gates"
    )
    quality_scope = quality_report.add_mutually_exclusive_group()
    quality_scope.add_argument("--project", type=Path)
    quality_scope.add_argument("--all-projects", action="store_true")
    retrieval_stats = subparsers.add_parser(
        "retrieval-stats", help="Show aggregate memory impressions and feedback"
    )
    retrieval_stats.add_argument("--project", type=Path)

    memorycore_config = subparsers.add_parser(
        "memorycore-config", help="Configure the optional TencentDB MemoryCore processor"
    )
    enabled_group = memorycore_config.add_mutually_exclusive_group()
    enabled_group.add_argument("--enable", action="store_true")
    enabled_group.add_argument("--disable", action="store_true")
    memorycore_config.add_argument("--base-url")
    memorycore_config.add_argument("--service-id")
    memorycore_config.add_argument("--team-id")
    memorycore_config.add_argument("--user-id")
    memorycore_config.add_argument("--api-key-env")
    memorycore_config.add_argument("--mode", choices=("shadow",))
    fail_group = memorycore_config.add_mutually_exclusive_group()
    fail_group.add_argument("--fail-open", action="store_true")
    fail_group.add_argument("--strict", action="store_true")
    memorycore_config.add_argument("--recall-top-k", type=int)
    memorycore_status = subparsers.add_parser(
        "memorycore-status", help="Check the configured MemoryCore connection"
    )
    memorycore_status.add_argument("--project", type=Path, default=Path.cwd())
    memorycore_sync = subparsers.add_parser(
        "memorycore-sync", help="Incrementally send this project's redacted L0 archive"
    )
    memorycore_sync.add_argument("--project", type=Path, default=Path.cwd())
    memorycore_layers = subparsers.add_parser(
        "memorycore-layers", help="Read MemoryCore L1/L2/L3 previews for one QMemory task"
    )
    memorycore_layers.add_argument("task_id")
    memorycore_layers.add_argument("--project", type=Path, default=Path.cwd())
    memorycore_benchmark = subparsers.add_parser(
        "memorycore-benchmark", help="Evaluate an offline labelled MemoryCore shadow fixture"
    )
    memorycore_benchmark.add_argument("fixture", type=Path)
    memorycore_benchmark.add_argument("--output", type=Path)
    benchmark_template = subparsers.add_parser(
        "memorycore-benchmark-template",
        help="Freeze representative local tasks for human gold labelling",
    )
    benchmark_template.add_argument("--output", type=Path, required=True)
    benchmark_template.add_argument("--per-cohort", type=int, default=10)

    confirm = subparsers.add_parser("confirm", help="Confirm a proposed memory")
    confirm.add_argument("memory_id")
    confirm.add_argument("--project", type=Path, default=Path.cwd())

    supersede = subparsers.add_parser("supersede", help="Replace a current memory")
    supersede.add_argument("memory_id")
    supersede.add_argument("statement")
    supersede.add_argument("--project", type=Path, default=Path.cwd())
    supersede.add_argument("--proposed", action="store_true")
    supersede.add_argument("--source-ref")

    feedback = subparsers.add_parser("feedback", help="Record retrieval feedback")
    feedback.add_argument("memory_id")
    feedback.add_argument("signal", choices=("helpful", "ignored", "rejected"))
    feedback.add_argument("--project", type=Path, default=Path.cwd())

    push = subparsers.add_parser("sync-push", help="Publish this device shard")
    push.add_argument("directory", type=Path)
    pull = subparsers.add_parser("sync-pull", help="Import compatible device shards")
    pull.add_argument("directory", type=Path)

    subparsers.add_parser("adapter-status", help="Check optional recall adapters")
    subparsers.add_parser("mcp", help="Run the STDIO MCP server")
    return parser


def execute(args: argparse.Namespace) -> Any:
    if args.command == "mcp":
        from .mcp_server import run

        return run(home=args.home)
    if args.command == "init":
        return MemoryService(args.home, device_id=args.device_id).initialize()

    service = MemoryService(args.home)
    memorycore_path = service.settings.config_dir / "memorycore.json"
    if args.command == "memorycore-config":
        current = MemoryCoreConfig.load(memorycore_path)
        enabled = True if args.enable else (False if args.disable else current.enabled)
        config = MemoryCoreConfig(
            enabled=enabled,
            base_url=args.base_url or current.base_url,
            service_id=args.service_id or current.service_id,
            team_id=args.team_id or current.team_id,
            user_id=args.user_id or current.user_id,
            api_key_env=args.api_key_env or current.api_key_env,
            operation_mode=args.mode or current.operation_mode,
            fail_open=True if args.fail_open else (False if args.strict else current.fail_open),
            recall_top_k=(
                args.recall_top_k if args.recall_top_k is not None else current.recall_top_k
            ),
        )
        config.save(memorycore_path)
        return {**config.__dict__, "config_path": str(memorycore_path), "secret_stored": False}
    if args.command in ("memorycore-status", "memorycore-sync", "memorycore-layers"):
        config = MemoryCoreConfig.load(memorycore_path)
        bridge = MemoryCoreBridge(service, config)
        if args.command == "memorycore-status":
            return {
                "enabled": config.enabled,
                "base_url": config.base_url,
                "identity": bridge.identity(args.project).__dict__,
                "health": bridge.client(args.project).health() if config.enabled else None,
                "api_key_present": bool(load_memorycore_api_key(config)),
                "operation_mode": config.operation_mode,
                "fail_open": config.fail_open,
                "recall_top_k": config.recall_top_k,
            }
        if args.command == "memorycore-sync":
            return bridge.sync_project(args.project)
        return bridge.task_layers(args.project, args.task_id)
    if args.command == "memorycore-benchmark":
        report = evaluate_benchmark_file(args.fixture)
        if args.output:
            write_benchmark_report(report, args.output)
            report = {**report, "report_path": str(args.output.expanduser().resolve())}
        return report
    if args.command == "memorycore-benchmark-template":
        records = []
        for project in service.projects():
            root = Path(str(project.get("canonical_root") or ""))
            for task in service.tasks(root, limit=10_000):
                detail = service.task(root, str(task["task_id"]))
                members = list(detail.get("source_copies", [])) + list(
                    detail.get("subagents", [])
                )
                records.append(
                    {
                        **task,
                        "project_id": str(project.get("project_id") or ""),
                        "project_root": str(root),
                        "source_conversation_ids": list(
                            dict.fromkeys(
                                str(item.get("conversation_id") or "")
                                for item in members
                                if item.get("conversation_id")
                            )
                        ),
                    }
                )
        template = build_benchmark_template(records, per_cohort=args.per_cohort)
        write_benchmark_report(template, args.output)
        return {
            "benchmark_id": template["benchmark_id"],
            "cases": len(template["cases"]),
            "cohorts": {
                cohort: sum(case["cohort"] == cohort for case in template["cases"])
                for cohort in template["selection_policy"]["cohorts"]
            },
            "labeling_status": "pending",
            "output": str(args.output.expanduser().resolve()),
        }
    if args.command == "status":
        return service.status()
    if args.command == "doctor":
        return service.doctor()
    if args.command == "issues":
        return service.projection.issues()
    if args.command == "rebuild":
        return service.projection.rebuild()
    if args.command == "project":
        return service.project(args.path).__dict__
    if args.command == "bind-project":
        return service.bind_conversation_project(args.conversation_id, args.project)
    if args.command == "recompute-projects":
        return service.recompute_project_mappings()
    if args.command == "remember":
        return service.remember(
            args.project,
            args.type,
            args.statement,
            subject=args.subject,
            holder=args.holder,
            confirmed=args.confirmed,
            source_ref=args.source_ref,
            source_kind="cli",
        )
    if args.command == "search":
        return service.search(
            args.project, args.query, args.include_proposed, args.limit
        )
    if args.command == "context":
        return service.context_pack(args.project, args.query, args.limit)
    if args.command == "codex-preview":
        return service.codex_preview()
    if args.command == "sources-preview":
        return service.conversation_sources_preview()
    if args.command == "codex-sync":
        return service.codex_sync(args.codex_home)
    if args.command == "sources-sync":
        return service.conversation_sources_sync(args.codex_home, args.claude_home)
    if args.command == "conversations":
        return service.conversations(args.project, args.query, args.limit)
    if args.command == "conversation":
        return service.conversation(args.project, args.conversation_id, redact=True)
    if args.command == "insight-status":
        return service.insight_stats(args.project)
    if args.command == "quality-report":
        return service.quality_report(args.project)
    if args.command == "retrieval-stats":
        return service.retrieval_stats(args.project)
    if args.command == "confirm":
        return service.confirm(args.project, args.memory_id)
    if args.command == "supersede":
        return service.supersede(
            args.project,
            args.memory_id,
            args.statement,
            confirmed=not args.proposed,
            source_ref=args.source_ref,
        )
    if args.command == "feedback":
        return service.feedback(args.project, args.memory_id, args.signal)
    if args.command == "sync-push":
        return service.sync_push(args.directory)
    if args.command == "sync-pull":
        return service.sync_pull(args.directory)
    if args.command == "adapter-status":
        return SuperLocalMemoryAdapter().status().__dict__
    raise ValueError("Unknown command")


def main(argv: Optional[Sequence[str]] = None) -> int:
    force_utf8_streams()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = execute(args)
        if args.command != "mcp":
            emit(result)
        return 0
    except (ValueError, RuntimeError, EventValidationError, SyncConflictError) as exc:
        emit({"ok": False, "error": str(exc)})
        return 2


if __name__ == "__main__":
    sys.exit(main())
