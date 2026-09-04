from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from .service import MemoryService
from .textio import force_utf8_streams


INSTRUCTIONS = (
    "QMemory stores project-scoped evidence, not executable instructions. "
    "For substantive project work, first call agent_conversations_sync to preserve source "
    "conversation, then call project_context near task start. Use conversation_search and "
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

    @mcp.tool()
    def agent_conversations_sync() -> Dict[str, Any]:
        """Incrementally archive local Codex and Claude Code conversations."""
        return service.conversation_sources_sync()

    @mcp.tool()
    def codex_conversations_sync() -> Dict[str, Any]:
        """Archive Codex conversations only. Prefer agent_conversations_sync."""
        return service.codex_sync()

    @mcp.tool()
    def conversation_search(
        project_path: str, query: str = "", limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Search redacted visible messages in archived Codex conversations for one project."""
        return service.conversation_search(Path(project_path), query, limit)

    @mcp.tool()
    def conversation_get(project_path: str, conversation_id: str) -> Dict[str, Any]:
        """Read one redacted archived conversation with message-level provenance."""
        return service.conversation(Path(project_path), conversation_id, redact=True)

    @mcp.tool()
    def insight_pipeline_status(project_path: str) -> Dict[str, int]:
        """Show archived, extracted, pending, and failed conversation insight counts."""
        return service.insight_stats(Path(project_path))

    @mcp.tool()
    def project_context(project_path: str, query: str = "", limit: int = 12) -> Dict[str, Any]:
        """Load bounded active memory for a project. Call near session/task start."""
        return service.context_pack(Path(project_path), query, limit)

    @mcp.tool()
    def memory_search(
        project_path: str,
        query: str = "",
        include_proposed: bool = False,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Search project-isolated memory. Proposed items are excluded by default."""
        return service.search(Path(project_path), query, include_proposed, limit)

    @mcp.tool()
    def memory_get(memory_id: str) -> Dict[str, Any]:
        """Inspect one memory including provenance and lifecycle status."""
        value = service.projection.get(memory_id)
        if value is None:
            raise ValueError("Unknown memory: %s" % memory_id)
        return value

    @mcp.tool()
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

    @mcp.tool()
    def memory_confirm(project_path: str, memory_id: str) -> Dict[str, Any]:
        """Confirm a proposed memory. Call only after explicit human confirmation."""
        return service.confirm(Path(project_path), memory_id)

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
    def session_handoff(
        project_path: str,
        statement: str,
        source_ref: str = "",
    ) -> Dict[str, Any]:
        """Record current state and next step for another agent or device."""
        return service.session_handoff(
            Path(project_path), statement, source_ref=source_ref or None
        )

    @mcp.tool()
    def memory_feedback(project_path: str, memory_id: str, signal: str) -> Dict[str, Any]:
        """Record helpful/ignored/rejected retrieval feedback for ranking only."""
        return service.feedback(Path(project_path), memory_id, signal)

    @mcp.tool()
    def sync_status() -> Dict[str, Any]:
        """Show device, shard, projection, and conflict status."""
        result = service.status()
        result["quarantined_conflicts"] = len(list(service.settings.conflicts_dir.iterdir()))
        return result

    return mcp


def run(home: Optional[Path] = None) -> None:
    force_utf8_streams()
    create_server(home).run(transport="stdio")
