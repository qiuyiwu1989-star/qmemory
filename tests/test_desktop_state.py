from __future__ import annotations

import plistlib
from pathlib import Path

from qmemory.desktop_state import (
    bundle_executable,
    diagnose_claude_connection,
    find_app_bundle,
    parse_codex_mcp_get,
)


def test_bundle_executable_uses_info_plist_not_python_guess(tmp_path: Path) -> None:
    bundle = tmp_path / "QMemory.app"
    macos = bundle / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    executable = macos / "desktop_main"
    executable.write_text("binary", encoding="utf-8")
    with (bundle / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump({"CFBundleExecutable": "desktop_main"}, handle)
    assert bundle_executable(bundle) == executable
    assert find_app_bundle(macos / "python") == bundle


def test_parse_codex_mcp_get_finds_broken_command() -> None:
    parsed = parse_codex_mcp_get(
        "qmemory\n  enabled: true\n  transport: stdio\n  command: /missing/python\n  args: --mcp\n"
    )
    assert parsed == {"configured": True, "enabled": True, "command": "/missing/python"}


def test_diagnose_claude_connection_detects_configured_executable(
    tmp_path: Path, monkeypatch
) -> None:
    executable = tmp_path / "qmemory"
    executable.touch()

    class Result:
        returncode = 0
        stdout = "qmemory\n  Command: %s --mcp\n" % executable

    monkeypatch.setattr("qmemory.desktop_state.subprocess.run", lambda *args, **kwargs: Result())
    assert diagnose_claude_connection(Path("/bin/claude"), executable)["state"] == "connected"
