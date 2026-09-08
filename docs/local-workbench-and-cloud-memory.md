# QMemory: local evidence workspace and personal cloud memory

Date: 2026-09-08. Scope: QMemory only, not Shennao or IoT.

## Product contract

QMemory preserves conversations from different Agents, organizes them by project/task,
and turns source-linked judgments into reusable memory. The destination is a personal
cloud memory space accessible by authorized Agents across platforms and projects.

This change implements the **local read-only evidence workspace**, not a cloud service.
No upload, remote deployment, background synchronization, extraction, database migration,
memory confirmation, source deletion, or Agent configuration change is performed.
The old illustrative prototype at port 8768 remains separate and unchanged.

## Current local implementation

```text
Existing Codex / Claude Code archives + judgment event log
          │ existing import / indexing mechanisms (unchanged)
          ▼
sources.sqlite3 + qmemory.sqlite3 (rebuildable projections)
          │ SQLite mode=ro + query_only
          ▼
127.0.0.1 read-only adapter
          ├── task board: existing source groups, not invented completion states
          ├── source canvas: source copies and execution branches
          ├── memory view: stored judgments directly citing the selected task
          └── evidence drawer: each source's own ordered messages
```

Launch from the repository:

```sh
PYTHONPATH=src .venv/bin/python -m qmemory.workbench --port 8790
```

Open `http://127.0.0.1:8790/qmemory.html`. An alternative local archive can be selected
with `--home PATH`. This must point to existing indexes; missing indexes return an
explicit unavailable state and are never initialized by the workbench.

Optional `?project=<encoded-project-id>` selects a registered project on entry. The
project picker labels directory/repository identities separately. Read-only inspection
found a local QMemory path identity and a Git identity with separate source groups;
these are not silently merged by this adapter. The next identity-mapping increment
must confirm their relationship and preserve both historical aliases.

### Read contract

| Route | Scope / result |
| --- | --- |
| GET /api/projects | Visible registered projects, source counts, index timestamps |
| GET /api/tasks?project=… | Groups using the archive's existing parent/duplicate links |
| GET /api/task?project=…&task=… | Source members and directly source-linked judgments |
| GET /api/messages?project=…&task=…&source=… | 30 ordered messages, next offset, archive version |
| Optional offset / sequence | Page navigation or exact cited message positioning |

All APIs require a process-session header obtained by the local page. The server is
loopback-only and checks Host, Origin and cross-site fetch metadata, with no CORS grant.
Static routes are an allowlist, not a directory server. CSP prohibits third-party assets,
inline scripts and framing. Source text is escaped in the browser, rule-redacted for
display, and not placed in access logs or browser localStorage. Local programs under
the user's account can still access the service: this is not a multi-user security boundary.

The read model exposes `space_id=local-private`, project ID, task ID, source reference,
and a version-scoped evidence fingerprint. This is a local placeholder, **not** implemented
tenant isolation. SQLite read snapshots cover each index independently; this is not an
atomic snapshot across both indexes. A concurrent importer may update between reads.

### Honest limitations

- A task is currently an archive relationship group, not a verified unit of completed work.
- The source canvas is not an AI-authored story. Narrative beats need validated evidence.
- The memory view is a source-linked card view, not yet a full draggable semantic graph.
- Repeated short messages remain separate occurrences. Copies are linked, not discarded.
- All source members are readable, but this does not fix the old extractor's duplicate/branch
  exclusion or guarantee it has analyzed their union. That is the next data-layer increment.
- Only directly referenced judgments appear in a task. Unlinked project memories are not
  inferred into a task. Missing memory indexes are distinct from zero matching memories.
- Messages are indexed text, not byte-exact raw exports. Each preview is limited to 20,000
  characters; the archive is unchanged. Embedded images/tool payloads are not rendered here.
- Evidence hashes include the current archive version; the same occurrence can receive a
  new version fingerprint after append. Durable cross-device occurrence IDs need a separate
  mapping, not reuse of this hash as a universal ID.
- Project/task lists currently load all metadata in scope. Pagination/virtualization and
  large-library latency budgets are required before general distribution.
- This module introduces no archive compaction. Lossless restore/hash gates still apply.

## Target architecture: local-first, selectively cloud-shared

```text
Your computer A / B / other platforms
  Agent collectors → private original archive → normalized evidence occurrences
                                      │
                          source-aware merge / AI candidates
                                      │
                       user judgment + authorized publication
                                      ▼
                    Personal cloud memory service
       identity / spaces / project grants / published revisions / audit
                 │                         │
          memory search + context     selected evidence objects
          SQL + derived search index  encrypted private object storage
                 │                         │
                 └──── permission-checked REST + MCP ────┐
                                                       ▼
                                 your authorized Agents / projects
                                                       │
                                    proposals + use feedback, not blind overwrite
```

### Ownership and publication rules (proposed design)

1. **Personal space** is yours. A shared/team space is a separate security scope, not a
   public toggle on all conversations. New devices and Agent clients receive explicit grants.
2. **Project access is the default**. Cross-project access requires an explicit allowed
   project set or a separately published personal/common knowledge collection. A client
   supplying someone else's `project_id` must never broaden its server-side grant.
3. **Publish memory first, evidence selectively**. Show content, destination, audience and
   evidence attachments before first publication. A later automatic publishing policy must
   be explicitly enabled for a named collection. Regex redaction is not proof of safe sharing.
4. **Keep private original conversations local by default**. Cloud evidence is an approved
   excerpt/artifact, not a local path or supplier URL. Do not upload the large raw archive.
5. **Separate stable identity from revision**. Use stable UUIDs for cloud space/project/memory,
   source session and message occurrence; map local Git/path aliases to stable projects.
   A revision has a content digest, extraction version, source occurrence and immutable
   evidence version. Identical text alone does not establish the same occurrence.
6. **Explicit state changes**: proposed → confirmed; confirmed → superseded / revoked.
   Agents may propose; exact user approval or a specifically authorized policy controls
   promotion. Contradictions become reviewable conflicts, not silent last-write-wins.
7. **Cloud published revisions have one authoritative write service**. Offline devices queue
   idempotent events with expected revision; stale revisions create conflicts. Local private
   events remain private. Revocation is propagated with tombstones; it cannot retract content
   already copied into an external Agent's conversation. State that limitation in the UI.
8. **Every retrieval is authorized** across search results, source excerpts, caches and exports.
   Keep a scoped audit of requesting client, memory revision, purpose and feedback, not full
   sensitive prompts by default. Default-deny empty/missing grants.

### Future cloud protocol sketch (not implemented)

Common envelope: `space_id`, `project_id`, `memory_id`, `revision`, `evidence_refs`,
`visibility`, `state`, `created_by`, `updated_at`. Identity/permissions come from verified
client credentials, not fields supplied by an Agent.

- `context.search`: scoped query → authorized confirmed revisions + evidence availability.
- `evidence.get`: stable occurrence/version → only approved authorized excerpt.
- `memory.propose`: source-linked candidate; an idempotency key prevents retry duplicates.
- `memory.publish / supersede / revoke`: owner/policy-authorized revision operations.
- `memory.feedback`: record actual retrieval/use/outcome without treating retrieval as adoption.

REST and MCP should call the same authorization and domain services. Start with one backend
and a SQL database plus private object storage; a replaceable vector index is a projection,
not the source of truth. Do not add a separate graph database merely for canvas rendering.
Hosting, identity provider, credentials, budget and initial publication set remain undecided.

## Next increments and acceptance gates

| Order | Increment | Gate before proceeding |
| --- | --- | --- |
| 1 | Read-only real workspace (this change) | Project isolation, all source members, exact cited positions, no mutations; browser checks |
| 2 | Source union and stable occurrence IDs | Claude imported into Codex: duplicates linked; both sides' unique content and repeated occurrences retained; re-import idempotent |
| 3 | Evidence-grounded analysis | Small authorized sample; every candidate cites resolvable evidence; conflicts visible; user can confirm/reject/supersede |
| 4 | Three real views | Board for work state, story for evidence-backed turning points, map for reusable judgments; switching preserves focus; no invented states |
| 5 | Personal cloud pilot | Explicitly publish a small set; second-device authenticated retrieval; forbidden project denied; retries do not duplicate; revoke verified |
| 6 | Team/open-source release | Fresh install, scoped invitations, export/restore, deletion policy, secret scan, privacy-safe fixtures, documented operational costs |

The closed loop is: capture → preserve → associate → derive → judge → retrieve → verify
usefulness → revise. “Stored” is not “remembered”; “retrieved” is not “helped the task”.
The first cloud pilot should demonstrate one new Agent using a previously confirmed memory
and linking back to its approved evidence, rather than reporting only connection success.

## Implementation handoff / verification

Implemented files: `src/qmemory/workbench.py`, packaged `src/qmemory/web/` assets,
package-data declaration in `pyproject.toml`, and `tests/test_workbench.py`.

Verified on 2026-09-08:

- Full local regression: **114 passed in 60.83s**, including 12 new workbench cases.
- `node --check src/qmemory/web/workbench.js` and `git diff --check`: passed.
- Synthetic real-schema tests: cross-project rejection, duplicate/branch members,
  source-linked memories, exact positions, repeat occurrences, pagination, redaction,
  missing indexes, invalid parameters, origin/session guards and read-only DB enforcement.
  Before/after database file hashes match for read operations on the fixture.
- Live browser: repository QMemory task opened; 30-message pages advanced from 1–30 to
  31–60; directory QMemory group exposed three source members; changing source updated
  the evidence reference; refresh retained the task; no document horizontal overflow
  in the inspected browser viewport.
- Both inspected QMemory task groups had no directly linked judgments in the memory view.
  Their empty state was displayed honestly. The evidence button's exact-position backend
  behavior was tested with synthetic judgments, not a live linked judgment from these groups.
- The legacy illustrative prototype at 8768 was not changed. The separate development
  process at 8790 is manually launched, not installed as an auto-start desktop service.

No cloud service, cloud identity, team sharing, analysis job or archive compaction was
enabled. No release/version bump, GitHub push or desktop installation was performed.
Existing untracked product-methodology/productization-roadmap documents were preserved.

Next concrete task: audit and propose a reversible project alias map for the QMemory
path/Git identities, then implement a task-level evidence-union read model with stable
occurrence identity. Test imported Claude histories plus later Codex continuations before
using that evidence set for AI extraction. Do not silently merge same-name projects.
