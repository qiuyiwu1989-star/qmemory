# QMemory × TencentDB MemoryCore

QMemory keeps the complete local evidence archive. MemoryCore is an optional processor for L1
atomic memory, L2 project scenarios, and L3 stable core summaries. Disabling MemoryCore never
removes or changes a QMemory conversation or confirmed judgment.

QMemory v0.11 only permits `shadow` operation. MemoryCore may receive redacted L0 evidence and
return candidate/recall plans, but QMemory does not automatically confirm those candidates or
inject them into an Agent prompt. Active operation remains blocked until the quality benchmark
passes.

## Connect from the desktop

Open **设置与连接 → TencentDB MemoryCore** and enter:

- the MemoryCore base URL (`http://127.0.0.1:8420` locally or an HTTPS remote URL);
- the service, team, and user IDs shared by your devices;
- the *name* of the environment variable containing the gateway key, or the gateway key itself
  for secure storage in macOS Keychain.

Do not put the key in the URL or a project file. The desktop stores the optional gateway key in
macOS Keychain, never in QMemory's JSON settings, bridge state, logs, or memories. The
MemoryCore process itself keeps its LLM provider key.

## Connect from the CLI

```bash
export QMEMORY_MEMORYCORE_API_KEY='gateway-key-if-required'

qmemory memorycore-config --enable \
  --base-url https://memory.example.com \
  --service-id qmemory \
  --team-id qmemory-personal \
  --user-id qiuyiwu \
  --api-key-env QMEMORY_MEMORYCORE_API_KEY \
  --mode shadow --fail-open --recall-top-k 5

qmemory memorycore-status --project /path/to/project
qmemory memorycore-sync --project /path/to/project
qmemory memorycore-benchmark-template \
  --output artifacts/qmemory-real-gold-v1.json --per-cohort 10
qmemory memorycore-benchmark artifacts/qmemory-real-gold-v1.json
```

The desktop automatically performs an incremental MemoryCore sync after the local Codex and
Claude Code archive scan. L1-L3 results appear as read-only previews. MemoryCore results do not
become formal QMemory judgments without explicit confirmation.

## Multiple devices

Use the same base URL, service ID, team ID, and user ID on each device. QMemory derives a stable
MemoryCore agent identity from the QMemory project identity, so copies of the same Git project
share a processing namespace while unrelated projects remain isolated.

The bridge state in `~/.local/share/qmemory/state/memorycore-bridge.json` records only hashes,
IDs, sequence numbers, and provenance mappings. It contains no API or LLM keys.

## Failure and rollback

When MemoryCore is offline, QMemory continues archiving and displaying L0 conversations. To
roll back, disable the connection. The bridge is one-way and has no delete permission over the
QMemory archive.
