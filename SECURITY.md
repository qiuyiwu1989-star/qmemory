# Security policy

## Supported version

Security fixes are applied to the latest release on the default branch.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository. Do not include real
credentials, private conversation archives, or personal memory databases in an issue.

## Data boundary

QMemory keeps raw Agent conversations under its local application data directory. The Git
repository, normal event-shard sync, and TencentDB MemoryCore bridge do not publish that raw
archive. MemoryCore receives only the redacted user/assistant projection when explicitly
enabled.

QMemory provides rule-based credential redaction at display, indexing, diagnostics, and bridge
boundaries. It is not a substitute for rotating a credential that has already been pasted into
an Agent conversation.
