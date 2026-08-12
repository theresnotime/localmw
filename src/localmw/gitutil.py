"""A small wrapper around the ``git`` CLI.

We shell out rather than take a git binding as a dependency: anyone with a MediaWiki checkout
already has a working ``git``, and the handful of plumbing commands we need is stable.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

#: Local, cheap operations.
DEFAULT_TIMEOUT = 60
#: Anything that talks to a remote.
NETWORK_TIMEOUT = 300
#: Local, but can rewrite a lot of files (a checkout of core, say).
WORKTREE_TIMEOUT = 300

CHANGE_ID_RE = re.compile(r"^\s*Change-Id:\s*(I[0-9a-f]{8,40})\s*$", re.MULTILINE | re.IGNORECASE)

#: Cap on how many commits of a topic branch we inspect for Change-Ids.
MAX_BRANCH_COMMITS = 50

_RECORD_SEP = "\x1f"


class GitError(RuntimeError):
    """A git command exited non-zero (or could not be run at all)."""

    def __init__(self, git_args: Sequence[str], returncode: int, stderr: str = "") -> None:
        self.git_args = list(git_args)
        self.returncode = returncode
        self.stderr = (stderr or "").strip()
        super().__init__(self.summary)

    @property
    def detail(self) -> str:
        """The most useful single line of stderr, for table output."""
        for line in self.stderr.splitlines():
            line = line.strip()
            if line and not line.startswith("hint:"):
                return line
        return f"git exited with status {self.returncode}"

    @property
    def summary(self) -> str:
        return f"git {' '.join(self.git_args)}: {self.detail}"


def _git_env() -> dict:
    """Environment for git subprocesses: never block waiting for input."""
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    # We run many repos concurrently with piped stdio, so an ssh passphrase or host-key prompt
    # would hang invisibly until the timeout. Fail fast instead; an explicit GIT_SSH_COMMAND
    # from the user still wins.
    env.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes")
    env["LC_ALL"] = "C"
    return env


def run(
    path: Path,
    args: Sequence[str],
    *,
    check: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
) -> subprocess.CompletedProcess:
    """Run ``git <args>`` inside ``path``."""
    cmd = ["git", "-C", str(path), *args]
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_git_env(),
        )
    except subprocess.TimeoutExpired:
        raise GitError(args, -1, f"timed out after {timeout}s") from None
    except FileNotFoundError:
        raise GitError(args, -1, "git executable not found on PATH") from None
    if check and proc.returncode != 0:
        raise GitError(args, proc.returncode, proc.stderr)
    return proc


def out(path: Path, args: Sequence[str], *, check: bool = True, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Run git and return stripped stdout (empty string on failure when ``check`` is off)."""
    proc = run(path, args, check=check, timeout=timeout)
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def is_repo(path: Path) -> bool:
    """True if ``path`` is the root of a git checkout (or a submodule/worktree of one)."""
    return (path / ".git").exists()


def ref_exists(path: Path, ref: str) -> bool:
    return run(path, ["rev-parse", "--verify", "--quiet", ref], check=False).returncode == 0


# ---------------------------------------------------------------------------
# Repository state
# ---------------------------------------------------------------------------


@dataclass
class Commit:
    sha: str = ""
    subject: str = ""
    relative_date: str = ""
    author: str = ""


@dataclass
class RepoState:
    """Everything ``localmw status`` needs about one checkout."""

    branch: str | None = None
    detached: bool = False
    head: str = ""
    upstream: str | None = None
    ahead: int = 0
    behind: int = 0
    staged: int = 0
    unstaged: int = 0
    untracked: int = 0
    conflicts: int = 0
    last_commit: Commit = field(default_factory=Commit)
    fetch_error: str | None = None
    error: str | None = None

    @property
    def dirty(self) -> bool:
        """Uncommitted work that a pull could clobber. Untracked files do not count."""
        return bool(self.staged or self.unstaged or self.conflicts)

    @property
    def tracked_changes(self) -> int:
        return self.staged + self.unstaged + self.conflicts

    @property
    def has_upstream(self) -> bool:
        return self.upstream is not None

    def worktree_summary(self) -> str:
        if self.error:
            return "?"
        bits = []
        if self.staged:
            bits.append(f"+{self.staged}")
        if self.unstaged:
            bits.append(f"~{self.unstaged}")
        if self.conflicts:
            bits.append(f"!{self.conflicts}")
        if self.untracked:
            bits.append(f"?{self.untracked}")
        return " ".join(bits) if bits else "clean"


def _parse_status_v2(text: str, state: RepoState) -> None:
    for line in text.splitlines():
        if line.startswith("# branch.oid "):
            oid = line[len("# branch.oid ") :].strip()
            state.head = "(initial)" if oid == "(initial)" else oid[:9]
        elif line.startswith("# branch.head "):
            head = line[len("# branch.head ") :].strip()
            if head == "(detached)":
                state.detached = True
                state.branch = None
            else:
                state.branch = head
        elif line.startswith("# branch.upstream "):
            state.upstream = line[len("# branch.upstream ") :].strip()
        elif line.startswith("# branch.ab "):
            for token in line[len("# branch.ab ") :].split():
                try:
                    if token.startswith("+"):
                        state.ahead = int(token[1:])
                    elif token.startswith("-"):
                        state.behind = int(token[1:])
                except ValueError:  # pragma: no cover - malformed git output
                    pass
        elif line.startswith(("1 ", "2 ")):
            xy = line[2:4]
            if xy[0] != ".":
                state.staged += 1
            if xy[1] != ".":
                state.unstaged += 1
        elif line.startswith("u "):
            state.conflicts += 1
        elif line.startswith("? "):
            state.untracked += 1


def read_state(path: Path) -> RepoState:
    """Read the current state of a checkout without touching the network."""
    state = RepoState()
    try:
        status = out(path, ["status", "--porcelain=v2", "--branch", "--untracked-files=normal"])
    except GitError as exc:
        state.error = exc.detail
        return state

    _parse_status_v2(status, state)

    log = out(
        path,
        ["log", "-1", f"--format=%h{_RECORD_SEP}%s{_RECORD_SEP}%cr{_RECORD_SEP}%an"],
        check=False,
    )
    if log:
        parts = log.split(_RECORD_SEP)
        while len(parts) < 4:
            parts.append("")
        state.last_commit = Commit(sha=parts[0], subject=parts[1], relative_date=parts[2], author=parts[3])

    return state


def fetch(path: Path, *, prune: bool = False, tags: bool = False) -> None:
    """Fetch from the default remote. Raises :class:`GitError` on failure."""
    args = ["fetch", "--quiet"]
    if prune:
        args.append("--prune")
    if tags:
        args.append("--tags")
    run(path, args, timeout=NETWORK_TIMEOUT)


def default_branch(path: Path, candidates: Sequence[str]) -> str | None:
    """Best guess at the repository's default branch."""
    head_ref = out(path, ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], check=False)
    if head_ref.startswith("origin/"):
        return head_ref.split("/", 1)[1]
    for candidate in candidates:
        if ref_exists(path, f"refs/remotes/origin/{candidate}"):
            return candidate
    for candidate in candidates:
        if ref_exists(path, f"refs/heads/{candidate}"):
            return candidate
    return None


# ---------------------------------------------------------------------------
# Integrating upstream changes
# ---------------------------------------------------------------------------


def integrate(path: Path, upstream: str, strategy: str) -> None:
    """Bring the current branch up to ``upstream`` using ``strategy``."""
    if strategy == "ff-only":
        args = ["merge", "--ff-only", upstream]
    elif strategy == "rebase":
        args = ["rebase", upstream]
    elif strategy == "merge":
        args = ["merge", "--no-edit", upstream]
    else:  # pragma: no cover - guarded by config validation
        raise ValueError(f"unknown pull strategy {strategy!r}")
    run(path, args, timeout=NETWORK_TIMEOUT)


def update_submodules(path: Path) -> None:
    run(path, ["submodule", "update", "--init", "--recursive"], timeout=NETWORK_TIMEOUT)


def count_commits(path: Path, base: str, tip: str) -> int:
    """How many commits ``tip`` has that ``base`` does not."""
    raw = out(path, ["rev-list", "--count", f"{base}..{tip}"], check=False)
    try:
        return int(raw)
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# Branches
# ---------------------------------------------------------------------------


@dataclass
class BranchInfo:
    name: str
    sha: str = ""
    relative_date: str = ""
    subject: str = ""
    is_current: bool = False


def list_branches(path: Path, prefix: str = "") -> list[BranchInfo]:
    """Local branches, optionally filtered by name prefix, newest commit first."""
    fmt = _RECORD_SEP.join(
        ["%(refname:short)", "%(objectname:short)", "%(committerdate:relative)", "%(contents:subject)", "%(HEAD)"]
    )
    raw = out(path, ["for-each-ref", "--sort=-committerdate", f"--format={fmt}", "refs/heads/"])
    branches: list[BranchInfo] = []
    for line in raw.splitlines():
        parts = line.split(_RECORD_SEP)
        while len(parts) < 5:
            parts.append("")
        name = parts[0]
        if prefix and not name.startswith(prefix):
            continue
        branches.append(
            BranchInfo(
                name=name,
                sha=parts[1],
                relative_date=parts[2],
                subject=parts[3],
                is_current=parts[4].strip() == "*",
            )
        )
    return branches


def commit_messages(path: Path, branch: str, base: str | None, limit: int = MAX_BRANCH_COMMITS) -> list[str]:
    """Full commit messages unique to ``branch`` (or just its tip if ``base`` is unknown)."""
    if base and ref_exists(path, base):
        rev_range = f"{base}..{branch}"
    else:
        rev_range = branch
        limit = 1
    raw = out(path, ["log", f"--max-count={limit}", "--format=%B%x00", rev_range], check=False)
    return [chunk.strip() for chunk in raw.split("\0") if chunk.strip()]


def change_ids(path: Path, branch: str, base: str | None) -> list[str]:
    """Gerrit Change-Ids of the commits unique to ``branch``, newest first, de-duplicated."""
    seen: list[str] = []
    for message in commit_messages(path, branch, base):
        for match in CHANGE_ID_RE.finditer(message):
            change_id = match.group(1)
            if change_id not in seen:
                seen.append(change_id)
    return seen


def branch_exists(path: Path, branch: str) -> bool:
    return ref_exists(path, f"refs/heads/{branch}")


def checkout(path: Path, branch: str, *, force: bool = False, track: str | None = None) -> None:
    """Check out ``branch``, creating it from ``track`` (e.g. ``origin/master``) if given.

    ``force`` throws away uncommitted changes to tracked files that are in the way. Untracked
    files are left alone either way, since git does not need to touch them.
    """
    args = ["checkout", "--quiet"]
    if force:
        args.append("--force")
    if track:
        args.extend(["-b", branch, "--track", track])
    else:
        args.append(branch)
    run(path, args, timeout=WORKTREE_TIMEOUT)


def is_ancestor(path: Path, maybe_ancestor: str, descendant: str) -> bool:
    return run(path, ["merge-base", "--is-ancestor", maybe_ancestor, descendant], check=False).returncode == 0


def delete_branch(path: Path, branch: str, *, force: bool = False) -> None:
    run(path, ["branch", "-D" if force else "-d", branch])


def gerrit_project(path: Path) -> str | None:
    """The Gerrit project name for a checkout, from .gitreview or the origin remote."""
    gitreview = path / ".gitreview"
    if gitreview.is_file():
        try:
            for line in gitreview.read_text(encoding="utf-8", errors="replace").splitlines():
                key, _, value = line.partition("=")
                if key.strip().lower() == "project" and value.strip():
                    return value.strip().removesuffix(".git")
        except OSError:
            pass

    url = out(path, ["config", "--get", "remote.origin.url"], check=False)
    if not url:
        return None

    if "://" in url:
        # ssh://user@host:29418/mediawiki/core, https://gerrit.wikimedia.org/r/mediawiki/core
        remote_path = urlsplit(url).path
    else:
        # scp-like syntax: [user@]host:project
        _, _, remote_path = url.partition(":")

    parts = [part for part in remote_path.split("/") if part]
    if len(parts) > 1 and parts[0] == "r":
        parts = parts[1:]  # Gerrit's web prefix
    if not parts:
        return None
    return "/".join(parts).removesuffix(".git") or None
