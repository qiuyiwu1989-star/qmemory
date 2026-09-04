from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple


REDACTION_VERSION = 2


# The raw transcript mirror is intentionally lossless. These rules are used at
# persistence boundaries for derived memory, search indexes, previews, titles,
# logs, and status payloads. Keep them prefix- or context-based: redacting every
# high-entropy string would destroy useful source material.
REDACTION_RULES = (
    (
        "private-key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?"
            r"-----END [A-Z0-9 ]*PRIVATE KEY-----"
        ),
    ),
    (
        "authorization",
        re.compile(
            r"(?i)\b(?:proxy-)?authorization\s*:\s*"
            r"(?:bearer|basic|token)?\s*[A-Za-z0-9._~+/=-]{8,}"
        ),
    ),
    ("cookie", re.compile(r"(?i)\b(?:set-)?cookie\s*:\s*[^\r\n]{6,}")),
    (
        "database-url",
        re.compile(
            r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis):\/\/"
            r"[^\s/@:]+:[^\s/@]+@[^\s]+"
        ),
    ),
    (
        "url-credential",
        re.compile(r"(?i)\bhttps?:\/\/[^\s/@:]+:[^\s/@]+@[^\s]+"),
    ),
    (
        "github-token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    ),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("model-api-key", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}\b")),
    ("google-api-key", re.compile(r"\bAIza[A-Za-z0-9_-]{25,}\b")),
    ("cloud-secret-id", re.compile(r"\bAKID[A-Za-z0-9]{12,}\b")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    (
        "jwt",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\."
            r"[A-Za-z0-9_-]{8,}\b"
        ),
    ),
    (
        "secret-flag",
        re.compile(
            r"(?i)(--(?:password|passwd|secret(?:-?key)?|api-?key|"
            r"access-?token|auth-?token)\s+)(?:['\"])?([^\s'\"]{6,})(?:['\"])?"
        ),
    ),
    (
        "assigned-secret",
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret(?:[_-]?(?:id|key))?|"
            r"client[_-]?secret|api[_-]?key|access[_-]?key(?:[_-]?id)?|"
            r"access[_-]?token|auth[_-]?token|refresh[_-]?token|private[_-]?token|"
            r"x-api-key)\b(\s*(?::|=|\bis\b)\s*)"
            r"(?:['\"])?([^\s,;'\"]{6,})(?:['\"])?"
        ),
    ),
)


def redact_text(value: str) -> Tuple[str, List[str]]:
    """Return display/index-safe text and triggered rule names.

    The function is deterministic and idempotent. Findings contain rule names
    only; secret values must never be copied into telemetry.
    """

    redacted = str(value)
    findings: List[str] = []
    for name, pattern in REDACTION_RULES:
        if name == "assigned-secret":

            def replace_assignment(match: re.Match[str]) -> str:
                return "%s%s[REDACTED:%s]" % (match.group(1), match.group(2), name)

            redacted, count = pattern.subn(replace_assignment, redacted)
        elif name == "secret-flag":

            def replace_flag(match: re.Match[str]) -> str:
                return "%s[REDACTED:%s]" % (match.group(1), name)

            redacted, count = pattern.subn(replace_flag, redacted)
        else:
            redacted, count = pattern.subn("[REDACTED:%s]" % name, redacted)
        if count:
            findings.extend([name] * count)
    return redacted, sorted(set(findings))


def redact_value(value: Any) -> Tuple[Any, List[str]]:
    """Recursively redact a JSON-like status, preview, or log payload."""

    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        result: Dict[Any, Any] = {}
        findings: List[str] = []
        for key, item in value.items():
            safe_item, item_findings = redact_value(item)
            result[key] = safe_item
            findings.extend(item_findings)
        return result, sorted(set(findings))
    if isinstance(value, list):
        result_list: List[Any] = []
        findings = []
        for item in value:
            safe_item, item_findings = redact_value(item)
            result_list.append(safe_item)
            findings.extend(item_findings)
        return result_list, sorted(set(findings))
    if isinstance(value, tuple):
        safe_list, findings = redact_value(list(value))
        return tuple(safe_list), findings
    return value, []
