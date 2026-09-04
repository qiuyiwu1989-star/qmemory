from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from .textio import run_text


@dataclass(frozen=True)
class ProjectIdentity:
    project_id: str
    root: str
    remote: Optional[str]
    commit_sha: Optional[str]
    canonical_root: Optional[str] = None
    identity_source: str = "cwd"


def _git(path: Path, *args: str) -> Optional[str]:
    try:
        result = run_text(
            ["git", "-C", str(path)] + list(args),
            check=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    value = result.stdout.strip()
    return value or None


def canonical_remote(remote: str) -> str:
    value = remote.strip()
    scp_match = re.match(r"^(?:[^@]+@)?([^:]+):(.+)$", value)
    if scp_match and "://" not in value:
        host = scp_match.group(1).lower()
        path = scp_match.group(2)
    else:
        parsed = urlsplit(value)
        host = (parsed.hostname or "local").lower()
        path = parsed.path
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return "%s/%s" % (host, path.lower())


def _canonical_git_root(root: Path) -> Path:
    """Return the main checkout for a repository, including linked worktrees.

    ``--show-toplevel`` returns the linked worktree path.  The common git
    directory is shared by every worktree and lives at ``<main>/.git`` for a
    normal repository, which gives us a stable local identity even before a
    remote is configured.
    """

    common_value = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common_value:
        return root
    common = Path(common_value).expanduser().resolve()
    if common.name == ".git":
        return common.parent
    return root


def _git_remote(root: Path) -> Optional[str]:
    origin = _git(root, "config", "--get", "remote.origin.url")
    if origin:
        return origin
    remotes = _git(root, "remote")
    if not remotes:
        return None
    first = sorted(value for value in remotes.splitlines() if value.strip())[0]
    return _git(root, "config", "--get", "remote.%s.url" % first)


def identify_project(
    path: Optional[Path],
    *,
    explicit_root: Optional[Path] = None,
    manual_fallback: Optional[Path] = None,
) -> ProjectIdentity:
    """Resolve a stable project identity without altering source evidence.

    Resolution order is intentionally explicit:

    1. a conversation-level explicit binding;
    2. the Git remote and canonical repository root (worktree aware);
    3. the conversation working directory;
    4. a manual fallback when no usable working directory was supplied.

    The returned ``root`` is the observed checkout while ``canonical_root`` is
    stable across linked worktrees.  Callers may therefore retain provenance
    without creating a project card per worktree.
    """

    source = "explicit" if explicit_root is not None else "cwd"
    selected = explicit_root if explicit_root is not None else path
    if selected is None:
        selected = manual_fallback or Path.cwd()
        source = "manual"
    candidate = selected.expanduser().resolve()
    root_value = _git(candidate, "rev-parse", "--show-toplevel")
    root = Path(root_value).resolve() if root_value else candidate
    canonical_root = _canonical_git_root(root) if root_value else root
    remote = _git_remote(root)
    commit = _git(root, "rev-parse", "HEAD")
    if remote:
        stable_key = "git:%s" % canonical_remote(remote)
    else:
        digest = hashlib.sha256(str(canonical_root).encode("utf-8")).hexdigest()[:24]
        stable_key = "path:%s" % digest
    return ProjectIdentity(
        project_id=stable_key,
        root=str(root),
        remote=canonical_remote(remote) if remote else None,
        commit_sha=commit,
        canonical_root=str(canonical_root),
        identity_source=source,
    )
