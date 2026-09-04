# TencentDB MemoryCore Isolation POC

## Decision

QMemory remains the canonical archive and judgment ledger. TencentDB MemoryCore is evaluated as
an optional, replaceable processing engine for L1–L3. It must never write into QMemory's `raw/`,
`events/`, or SQLite projections.

## Boundary

```text
QMemory source archive (read only)
        │ normalized copies
        ▼
isolated export directory
        │ /v3/conversation/add
        ▼
TencentDB MemoryCore lab instance
        │ L1 / L2 / L3 outputs
        ▼
comparison report (read only)
```

The lab instance uses a separate home, service ID, user, team, and agent. Deleting the lab must not
remove or alter any QMemory data.

## First experiment

1. Select one small project and one large cross-Agent project.
2. Export normalized `user` and `assistant` messages in deterministic batches of at most 100.
3. Preserve the QMemory conversation ID and message sequence in import metadata.
4. Import into a localhost-only MemoryCore instance.
5. Compare L1 precision, L2 usefulness, L3 stability, provenance coverage, latency, and model cost.
6. Do not enable Memory Proxy until archive import and provenance quality pass.

## Acceptance gates

- Exporting twice produces the same manifest and no duplicate logical messages.
- 100% of exported items can be mapped back to QMemory conversation ID + sequence.
- MemoryCore failure or removal leaves QMemory healthy and unchanged.
- Generated L1–L3 data is never promoted to confirmed QMemory memory without an explicit user
  decision.
- Services bind to localhost during the POC; no MemoryCore, Panel, Knowledge, or Proxy port is
  exposed publicly.

## Current environment

- Node.js satisfies MemoryCore's `>=22.16` requirement.
- No Docker daemon or local OpenAI-compatible model was detected.
- Memory extraction therefore needs an explicitly configured model endpoint before live L1–L3
  quality and cost can be measured. Archive export and API-contract tests can proceed without it.

## Completed baseline — 2026-08-15

- The official `feat/server_team` source was cloned into the isolated lab directory at commit
  `9059e52d11b7e66c2a3b5eb6161e4b4b8603c8c2`.
- The OpenVibe project was exported from QMemory twice: 4 logical conversations, 387 readable
  messages, 6 batches, and no batch larger than 100 messages.
- Both exports produced the same manifest hash
  `9e09ff848ceaf4aca33c9d2b19daf0a9749a6665a34bebcf5ea5b851e38f94b1` and the same payload
  hashes. Every exported message retains its QMemory conversation ID and message sequence.
- Export data lives under
  `~/.local/share/qmemory/labs/tencentdb-memorycore/openvibe-20260815-v1` and does not modify the
  canonical QMemory archive.
- Dependency installation in the upstream MemoryCore checkout did not complete in the available
  environment and was stopped without running lifecycle scripts. A localhost service and model
  endpoint are therefore still required before L1–L3 quality, latency, and cost can be compared.
