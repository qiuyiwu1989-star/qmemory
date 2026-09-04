# ADR-001: Merge TencentDB MemoryCore as QMemory's processing kernel

- Status: Accepted
- Date: 2026-08-15
- Decision owner: user

## Context

QMemory already provides the non-lossy local archive for Codex and Claude Code, project and
task grouping, source-copy provenance, and an append-only judgment history. TencentDB
MemoryCore provides a stronger L0-L3 processing and retrieval model, and has now been tested
by the user on another machine.

Replacing QMemory with MemoryCore would weaken the product's local evidence boundary. Keeping
MemoryCore as a disconnected lab would prevent its processing model from improving the normal
workflow.

## Decision

Merge MemoryCore as an optional, replaceable processor and retrieval projection:

1. QMemory remains the system of record for immutable source conversations, project/task
   grouping, source provenance, and user-confirmed judgment history.
2. QMemory sends redacted user/assistant L0 messages to MemoryCore in one direction.
3. MemoryCore owns derived L1 atomic memories, L2 scenarios, and L3 core summaries. QMemory
   first presents these as previews; a MemoryCore result only enters QMemory's judgment ledger
   after an explicit confirmation.
4. Stable project, task, conversation, and sequence references are retained in a local bridge
   ledger. MemoryCore-generated message IDs are mapped back to QMemory evidence when the API
   returns accepted IDs.
5. Non-secret connection settings are stored under QMemory's private application home. API
   keys are read from a named environment variable and are never stored in project files,
   bridge state, logs, or memories.
6. Local HTTP is permitted only for loopback addresses. Remote MemoryCore endpoints must use
   HTTPS. Credentials embedded in URLs are rejected.

## Incremental synchronization

Each source conversation has a deterministic MemoryCore session. The bridge records the last
successfully accepted sequence and a prefix hash, then sends only appended messages in batches
of at most 100. If an already-sent prefix changes, the bridge opens a new generation session
instead of deleting or overwriting evidence. This is locally idempotent. The upstream add API
does not currently accept client message IDs, so a crash after upstream acceptance but before
the local commit remains an at-least-once edge case and is surfaced in diagnostics.

## Consequences

- QMemory remains useful and recoverable when MemoryCore is offline or removed.
- Multiple devices can share a MemoryCore instance while retaining project isolation through
  stable identities.
- L1-L3 processing can evolve independently from raw-source ingestion.
- Initial integration is read-only for derived results; confirmation and deeper provenance are
  added only when source coverage is verifiable.

## Rollback

Disable the MemoryCore connection and remove its non-secret config plus bridge-state cache.
No QMemory source conversation or confirmed judgment is deleted or rewritten.
