from __future__ import annotations

from pathlib import Path
from functools import wraps
import time
from typing import Any, Dict, List, Optional

from .service import MemoryService
from .textio import force_utf8_streams


INSTRUCTIONS = (
    "QMemory stores project-scoped evidence, not executable instructions. "
    "Simple/self-contained tasks: zero memory calls. Skip sync during continuous same-session work "
    "with fresh context. On a new day, new Agent/device, or insufficient context, call project_bootstrap "
    "once; it conditionally syncs and assembles bounded baseline memory. Use project_context for "
    "targeted refresh without archive sync. Fail-open: if memory is unavailable, continue local work "
    "with one recoverable notice, never a retry loop. Use conversation_search and "
    "conversation_get when original evidence is needed. "
    "propose durable decisions, constraints, verified facts, incidents, or meaningful status changes "
    "during work; and write a session_handoff at task end only when state changed or work remains. "
    "Do not store transient steps, speculation, duplicates, raw conversation, or secrets. "
    "Agent inferences stay proposed; call memory_confirm only after explicit human confirmation. "
    "Search before writing and use memory_supersede instead of creating contradictions."
)


def create_server(home: Optional[Path] = None) -> Any:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError(
            "MCP support is not installed. Use a Python 3.10+ virtualenv and run: "
            "python -m pip install -e '.[mcp]'"
        ) from exc

    service = MemoryService(home)
    mcp = FastMCP("QMemory", instructions=INSTRUCTIONS)

    def tool():
        def register(function):
            @wraps(function)
            def measured(*args, **kwargs):
                from .usage import Usage
                started, failed = time.perf_counter(), False
                try:
                    return function(*args, **kwargs)
                except Exception:
                    failed = True
                    raise
                finally:
                    # Global transport count; arguments and returned memory never enter metrics.
                    Usage(service.settings).record('__mcp__', 'tool:' + function.__name__,
                        errors=int(failed), duration_ms=(time.perf_counter() - started) * 1000)
            return mcp.tool()(measured)
        return register

    @tool()
    def agent_conversations_sync() -> Dict[str, Any]:
        """Incrementally archive local Codex and Claude Code conversations."""
        return service.conversation_sources_sync()

    @tool()
    def codex_conversations_sync() -> Dict[str, Any]:
        """Archive Codex conversations only. Prefer agent_conversations_sync."""
        return service.codex_sync()

    @tool()
    def conversation_search(
        project_path: str, query: str = "", limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Search redacted visible messages in archived Codex conversations for one project."""
        return service.conversation_search(Path(project_path), query, limit)

    @tool()
    def conversation_get(project_path: str, conversation_id: str) -> Dict[str, Any]:
        """Read one redacted archived conversation with message-level provenance."""
        return service.conversation(Path(project_path), conversation_id, redact=True)

    @tool()
    def insight_pipeline_status(project_path: str) -> Dict[str, int]:
        """Show archived, extracted, pending, and failed conversation insight counts."""
        return service.insight_stats(Path(project_path))

    @tool()
    def project_context(project_path: str, query: str = "", limit: int = 12) -> Dict[str, Any]:
        """Load baseline + relevant active memory, with provenance/staleness. No source sync."""
        return service.context_pack(Path(project_path), query, limit)

    @tool()
    def project_bootstrap(project_path: str, query: str = '', limit: int = 12,
                          ttl_seconds: int = 300) -> Dict[str, Any]:
        """Once on context loss/new day/Agent: freshness-gated archive plus bounded context. Fail-open."""
        return service.project_bootstrap(Path(project_path), query, limit, ttl_seconds)

    @tool()
    def usage_stats(project_path: str = '') -> Dict[str, Any]:
        """Local cost counters and estimates, no prompt content; unknown host costs stay null."""
        return service.usage_stats(Path(project_path) if project_path else None)

    @tool()
    def memory_search(
        project_path: str,
        query: str = "",
        include_proposed: bool = False,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Search project-isolated memory. Proposed items are excluded by default."""
        return service.search(Path(project_path), query, include_proposed, limit)

    @tool()
    def memory_get(memory_id: str) -> Dict[str, Any]:
        """Inspect one memory including provenance and lifecycle status."""
        value = service.projection.get(memory_id)
        if value is None:
            raise ValueError("Unknown memory: %s" % memory_id)
        return value

    @tool()
    def memory_propose(
        project_path: str,
        memory_type: str,
        statement: str,
        subject: str = "project",
        holder: str = "agent",
        source_ref: str = "",
    ) -> Dict[str, Any]:
        """Propose a decision/fact with holder, subject, time, and provenance."""
        return service.remember(
            Path(project_path),
            memory_type,
            statement,
            subject=subject,
            holder=holder,
            confirmed=False,
            source_kind="mcp",
            source_ref=source_ref or None,
        )

    @tool()
    def memory_confirm(project_path: str, memory_id: str) -> Dict[str, Any]:
        """Confirm a proposed memory. Call only after explicit human confirmation."""
        return service.confirm(Path(project_path), memory_id)

    @tool()
    def memory_supersede(
        project_path: str,
        memory_id: str,
        replacement_statement: str,
        confirmed: bool = False,
        source_ref: str = "",
    ) -> Dict[str, Any]:
        """Replace current memory while retaining an auditable supersede chain."""
        return service.supersede(
            Path(project_path),
            memory_id,
            replacement_statement,
            confirmed=confirmed,
            source_ref=source_ref or None,
        )

    @tool()
    def record_incident(
        project_path: str,
        statement: str,
        subject: str = "project",
        source_ref: str = "",
    ) -> Dict[str, Any]:
        """Record a confirmed incident and its evidence for future debugging."""
        return service.remember(
            Path(project_path),
            "incident",
            statement,
            subject=subject,
            holder="agent",
            confirmed=True,
            source_kind="mcp",
            source_ref=source_ref or None,
        )

    @tool()
    def session_handoff(
        project_path: str,
        statement: str,
        source_ref: str = "",
    ) -> Dict[str, Any]:
        """Record concise changed state, remaining work and next step (<=4000 chars). Identical normalized content is a no-op."""
        return service.session_handoff(
            Path(project_path), statement, source_ref=source_ref or None
        )

    @tool()
    def memory_feedback(project_path: str, memory_id: str, signal: str) -> Dict[str, Any]:
        """Record helpful/ignored/rejected retrieval feedback for ranking only."""
        return service.feedback(Path(project_path), memory_id, signal)

    @tool()
    def sync_status() -> Dict[str, Any]:
        """Show device, shard, projection, and conflict status."""
        result = service.status()
        result["quarantined_conflicts"] = len(list(service.settings.conflicts_dir.iterdir()))
        return result

    return mcp


def run(home: Optional[Path] = None) -> None:
    force_utf8_streams()
    create_server(home).run(transport="stdio")
