from __future__ import annotations

import subprocess
import sys
from typing import IO, Any, Optional, Sequence


TEXT_ENCODING = "utf-8"
TEXT_ERRORS = "replace"


# ``subprocess`` and the standard streams fall back to
# ``locale.getpreferredencoding(False)`` when no encoding is given. A process
# started without ``LANG``/``LC_ALL`` — which is exactly how a GUI-launched
# agent client spawns the MCP server — resolves that to US-ASCII, so any git
# output or payload containing a non-ASCII project path raises UnicodeDecodeError.
# Every text boundary QMemory owns is therefore pinned to UTF-8 in code, so
# behaviour no longer depends on the locale of the machine that happens to run it.


def run_text(
    command: Sequence[str],
    *,
    input: Optional[str] = None,
    check: bool = False,
    stdout: Optional[Any] = subprocess.PIPE,
    stderr: Optional[Any] = None,
    timeout: Optional[float] = None,
    cwd: Optional[str] = None,
) -> "subprocess.CompletedProcess[str]":
    """Run a command and decode its output as UTF-8 regardless of locale."""

    return subprocess.run(
        list(command),
        input=input,
        check=check,
        stdout=stdout,
        stderr=stderr,
        timeout=timeout,
        cwd=cwd,
        encoding=TEXT_ENCODING,
        errors=TEXT_ERRORS,
    )


def _reconfigure(stream: Optional[IO[Any]]) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding=TEXT_ENCODING, errors=TEXT_ERRORS)
    except (OSError, ValueError):
        return


def force_utf8_streams() -> None:
    """Pin stdin/stdout/stderr to UTF-8 so non-ASCII output never crashes."""

    _reconfigure(sys.stdin)
    _reconfigure(sys.stdout)
    _reconfigure(sys.stderr)
