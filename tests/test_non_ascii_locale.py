"""Regression coverage for non-ASCII project paths under an ASCII locale.

QMemory is normally started by an agent client, which on macOS is often
launched from the GUI with no ``LANG``/``LC_ALL`` at all.  In that process
``locale.getpreferredencoding(False)`` resolves to ``US-ASCII``, so any text
boundary that inherits the locale — ``subprocess(text=True)`` reading ``git``
output, or ``print`` writing a project payload — raised ``UnicodeDecodeError``
for a project living under a path such as ``~/Documents/冷静/lengjing``.
Every write for such a project failed silently for the user.

These tests run the real entry points in a child interpreter with the locale
forced to ``C`` and UTF-8 mode disabled, which is the only faithful way to
reproduce a C-level locale decision the parent test process cannot fake.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / "src"

# Mirrors the real-world shape that broke: a non-ASCII directory component
# followed by an ASCII leaf, so the first bad byte lands mid-path.
NON_ASCII_PARENT = "冷静"
PROJECT_LEAF = "lengjing"


def _ascii_locale_env(home: Path) -> dict[str, str]:
    environment = dict(os.environ)
    for name in ("LANG", "LC_ALL", "LC_CTYPE", "PYTHONUTF8", "PYTHONIOENCODING"):
        environment.pop(name, None)
    environment["LC_ALL"] = "C"
    environment["PYTHONCOERCECLOCALE"] = "0"
    environment["PYTHONPATH"] = str(SOURCE_ROOT)
    environment["QMEMORY_HOME"] = str(home)
    return environment


def _run_in_ascii_locale(script: str, home: Path) -> dict:
    """Execute ``script`` in a child interpreter pinned to the C locale."""

    result = subprocess.run(
        [sys.executable, "-X", "utf8=0", "-c", script],
        env=_ascii_locale_env(home),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=path,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        encoding="utf-8",
    )


@pytest.fixture()
def non_ascii_project(tmp_path: Path) -> Path:
    project = tmp_path / NON_ASCII_PARENT / PROJECT_LEAF
    project.mkdir(parents=True)
    _git(project, "init")
    _git(project, "config", "user.email", "qmemory@example.invalid")
    _git(project, "config", "user.name", "QMemory Test")
    (project / "README.md").write_text("深脑\n", encoding="utf-8")
    _git(project, "add", "README.md")
    _git(project, "commit", "-m", "初始提交")
    return project


def test_child_interpreter_really_uses_ascii(tmp_path: Path) -> None:
    """Guard the guard: the harness must actually reproduce the ASCII locale."""

    probe = _run_in_ascii_locale(
        "import json, locale, sys;"
        "print(json.dumps({'preferred': locale.getpreferredencoding(False)}))",
        tmp_path / "home",
    )
    assert probe["preferred"].lower().replace("_", "-") in ("ascii", "us-ascii")


def test_project_identity_resolves_under_ascii_locale(
    tmp_path: Path, non_ascii_project: Path
) -> None:
    payload = _run_in_ascii_locale(
        "import json, sys\n"
        "from pathlib import Path\n"
        "from qmemory.project import identify_project\n"
        "identity = identify_project(Path(%r))\n"
        "sys.stdout.reconfigure(encoding='utf-8')\n"
        "print(json.dumps({'root': identity.root, 'canonical': identity.canonical_root,"
        " 'project_id': identity.project_id}))\n" % str(non_ascii_project),
        tmp_path / "home",
    )
    assert payload["root"] == str(non_ascii_project)
    assert payload["canonical"] == str(non_ascii_project)
    assert payload["project_id"].startswith("path:")


MCP_TOOL_SCRIPT = """
import asyncio, json, sys
from pathlib import Path
from qmemory.mcp_server import create_server

project = Path(%(project)r)
server = create_server()


def call(name, **arguments):
    # FastMCP returns (content_blocks, structured_result); the structured half
    # wraps a non-dict return value under "result".
    result = asyncio.run(server.call_tool(name, arguments))
    structured = result[1] if isinstance(result, tuple) else result
    if isinstance(structured, dict) and set(structured) == {"result"}:
        return structured["result"]
    return structured


proposed = call(
    "memory_propose",
    project_path=str(project),
    memory_type="decision",
    statement="中文路径下的判断必须能写入 %(leaf)s 项目",
    subject="qmemory",
    holder="agent",
    source_ref="claude-code://locale-regression#message-1",
)
memory_id = proposed["memory_id"]

incident = call(
    "record_incident",
    project_path=str(project),
    statement="ASCII locale 曾让中文路径的写入全部静默失败",
    subject="qmemory",
)
handoff = call(
    "session_handoff",
    project_path=str(project),
    statement="已修复 subprocess 解码；下一步重建 QMemory.app",
)
context = call("project_context", project_path=str(project))
search = call("memory_search", project_path=str(project), include_proposed=True)
# A CJK query exercises the same path with non-ASCII arguments as well.
cjk_query = call("project_context", project_path=str(project), query="中文路径")
conversations = call("conversation_search", project_path=str(project), query="中文")
stats = call("insight_pipeline_status", project_path=str(project))
fetched = call("memory_get", memory_id=memory_id)

payload = {
    "memory_id": memory_id,
    "proposed_statement": proposed["statement"],
    "incident_ok": bool(incident.get("memory_id")),
    "handoff_ok": bool(handoff.get("memory_id")),
    "context_root": (context.get("project") or {}).get("root"),
    "context_size": len(context.get("memories") or []),
    "cjk_query_root": (cjk_query.get("project") or {}).get("root"),
    "search_statements": [item["statement"] for item in search],
    "conversation_hits": len(conversations),
    "insight_stats_ok": isinstance(stats, dict),
    "fetched_statement": fetched["statement"],
}
sys.stdout.reconfigure(encoding="utf-8")
print(json.dumps(payload, ensure_ascii=False))
"""


def test_mcp_tools_write_and_read_non_ascii_project(
    tmp_path: Path, non_ascii_project: Path
) -> None:
    pytest.importorskip("mcp.server.fastmcp")
    home = tmp_path / "home"
    payload = _run_in_ascii_locale(
        MCP_TOOL_SCRIPT % {"project": str(non_ascii_project), "leaf": PROJECT_LEAF},
        home,
    )

    assert payload["proposed_statement"].startswith("中文路径下的判断")
    assert payload["incident_ok"] is True
    assert payload["handoff_ok"] is True
    assert payload["context_root"] == str(non_ascii_project)
    # The handoff and the incident are written confirmed, so they show up in the
    # default (active-only) context view; the proposal stays out of it.
    assert payload["context_size"] >= 2
    assert payload["cjk_query_root"] == str(non_ascii_project)
    assert any("中文路径下的判断" in value for value in payload["search_statements"])
    assert payload["conversation_hits"] == 0
    assert payload["insight_stats_ok"] is True
    assert payload["fetched_statement"] == payload["proposed_statement"]


def test_no_locale_dependent_subprocess_text_mode() -> None:
    """Keep every subprocess text boundary explicitly UTF-8."""

    offenders = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        body = path.read_text(encoding="utf-8")
        for number, line in enumerate(body.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "text=True" in stripped or "universal_newlines=True" in stripped:
                offenders.append("%s:%d" % (path.relative_to(REPO_ROOT), number))
            if "capture_output=True" in stripped:
                offenders.append("%s:%d" % (path.relative_to(REPO_ROOT), number))
    assert offenders == [], (
        "use qmemory.textio.run_text instead of locale-dependent text mode: %s"
        % ", ".join(offenders)
    )
