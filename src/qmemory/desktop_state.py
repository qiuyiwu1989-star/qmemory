from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from .textio import run_text


def find_app_bundle(executable: Path) -> Optional[Path]:
    current = executable.expanduser().resolve()
    for parent in (current,) + tuple(current.parents):
        if parent.suffix == ".app" and (parent / "Contents" / "Info.plist").exists():
            return parent
    return None


def bundle_executable(bundle: Path) -> Optional[Path]:
    plist_path = bundle / "Contents" / "Info.plist"
    if not plist_path.exists():
        return None
    with plist_path.open("rb") as handle:
        metadata = plistlib.load(handle)
    executable_name = metadata.get("CFBundleExecutable")
    if not executable_name:
        return None
    executable = bundle / "Contents" / "MacOS" / str(executable_name)
    return executable if executable.exists() else None


def parse_codex_mcp_get(output: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {"configured": False, "enabled": False, "command": None}
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("enabled:"):
            result["configured"] = True
            result["enabled"] = stripped.split(":", 1)[1].strip().lower() == "true"
        elif stripped.startswith("command:"):
            result["configured"] = True
            result["command"] = stripped.split(":", 1)[1].strip()
    return result


def diagnose_codex_connection(
    codex: Optional[Path], expected_executable: Optional[Path]
) -> Dict[str, Any]:
    if codex is None:
        return {
            "state": "codex_missing",
            "title": "未找到 Codex",
            "detail": "安装或打开 Codex 后再连接。",
        }
    try:
        result = run_text(
            [str(codex), "mcp", "get", "qmemory"],
            stderr=subprocess.STDOUT,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"state": "error", "title": "无法检查 Codex", "detail": str(exc)}
    parsed = parse_codex_mcp_get(result.stdout)
    if result.returncode != 0 or not parsed["configured"]:
        return {
            "state": "not_connected",
            "title": "尚未连接 Codex",
            "detail": "连接后，Codex 才能读写当前项目记忆。",
        }
    configured_command = Path(str(parsed.get("command") or "")).expanduser()
    if not parsed["enabled"]:
        return {
            "state": "disabled",
            "title": "Codex 连接已停用",
            "detail": "重新连接即可启用。",
            "command": str(configured_command),
        }
    if not configured_command.exists():
        return {
            "state": "broken",
            "title": "Codex 连接路径已失效",
            "detail": "QMemory 可以自动替换为当前应用的正确入口。",
            "command": str(configured_command),
        }
    if expected_executable and configured_command.resolve() != expected_executable.resolve():
        return {
            "state": "outdated",
            "title": "Codex 仍连接旧版 QMemory",
            "detail": "重新连接后会使用当前安装版本。",
            "command": str(configured_command),
        }
    return {
        "state": "connected",
        "title": "Codex 已连接",
        "detail": "智能体可以读取和写入项目记忆。",
        "command": str(configured_command),
    }


def diagnose_claude_connection(
    claude: Optional[Path], expected_executable: Optional[Path]
) -> Dict[str, Any]:
    if claude is None:
        return {
            "state": "claude_missing",
            "title": "未找到 Claude Code",
            "detail": "安装 Claude Code 后再连接。",
        }
    try:
        result = run_text(
            [str(claude), "mcp", "get", "qmemory"],
            stderr=subprocess.STDOUT,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"state": "error", "title": "无法检查 Claude Code", "detail": str(exc)}
    if result.returncode != 0:
        return {
            "state": "not_connected",
            "title": "尚未连接 Claude Code",
            "detail": "连接后，Claude Code 才能读写当前项目记忆。",
        }
    output = result.stdout
    if expected_executable and str(expected_executable) not in output:
        return {
            "state": "outdated",
            "title": "Claude Code 仍连接旧版 QMemory",
            "detail": "重新连接后会使用当前安装版本。",
        }
    return {
        "state": "connected",
        "title": "Claude Code 已连接",
        "detail": "智能体可以读取和写入项目记忆。",
    }
