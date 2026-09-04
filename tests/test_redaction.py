from __future__ import annotations

import json

import pytest

from qmemory.redaction import redact_text, redact_value


@pytest.mark.parametrize(
    ("label", "secret", "expected_rule"),
    [
        ("GitHub", "ghp_" + "A" * 30, "github-token"),
        ("GitHub fine-grained", "github_pat_" + "B" * 30, "github-token"),
        ("model", "sk-ant-" + "C" * 30, "model-api-key"),
        ("cloud id", "AKID" + "D" * 20, "cloud-secret-id"),
        ("AWS", "AKIA" + "E" * 16, "aws-access-key"),
        ("GitLab", "glpat-" + "F" * 24, "gitlab-token"),
        ("npm", "npm_" + "G" * 24, "npm-token"),
        ("Slack", "xoxb-" + "1" * 12 + "-" + "H" * 20, "slack-token"),
        (
            "JWT",
            "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 16,
            "jwt",
        ),
    ],
)
def test_prefixed_credentials_are_redacted(
    label: str, secret: str, expected_rule: str
) -> None:
    safe, findings = redact_text("%s value %s" % (label, secret))

    assert secret not in safe
    assert "[REDACTED:%s]" % expected_rule in safe
    assert expected_rule in findings


@pytest.mark.parametrize(
    "text",
    [
        "password=synthetic-passphrase",
        "SecretKey: synthetic-secret-material",
        "API_KEY is synthetic-api-material",
        "--access-token synthetic-cli-material",
        "Authorization: Bearer synthetic.header.payload",
        "Cookie: session=synthetic-cookie-material",
        "postgresql://sample-user:sample-password@db.invalid/qmemory",
        "https://sample-user:sample-password@example.invalid/private",
    ],
)
def test_contextual_credentials_are_redacted(text: str) -> None:
    safe, findings = redact_text(text)

    assert "synthetic" not in safe
    assert findings


def test_private_key_block_is_removed_without_copying_material() -> None:
    begin = "-----BEGIN " + "PRIVATE KEY-----"
    end = "-----END " + "PRIVATE KEY-----"
    private_key = (
        begin + "\n"
        "c3ludGhldGljLW5vdC1hLXJlYWwta2V5\n" + end
    )

    safe, findings = redact_text("title\n%s\nend" % private_key)

    assert private_key not in safe
    assert "private-key" in findings


def test_redaction_is_idempotent_and_does_not_redact_normal_project_text() -> None:
    ordinary = "Use SQLite and keep task L1/L2/L3 provenance in QMemory."
    once, first_findings = redact_text(ordinary)
    twice, second_findings = redact_text(once)

    assert once == ordinary
    assert twice == once
    assert first_findings == second_findings == []


def test_nested_preview_and_log_payload_is_redacted_without_schema_changes() -> None:
    secret = "ghp_" + "Z" * 30
    payload = {
        "title": "Imported task %s" % secret,
        "preview": ["safe", {"error": "password=synthetic-failure"}],
        "count": 2,
    }

    safe, findings = redact_value(payload)
    serialized = json.dumps(safe)

    assert secret not in serialized
    assert "synthetic-failure" not in serialized
    assert safe["count"] == 2
    assert set(safe) == set(payload)
    assert findings == ["assigned-secret", "github-token"]
