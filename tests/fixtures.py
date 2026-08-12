"""Helpers for building throwaway MediaWiki installs and git repositories on disk.

The tests drive the real ``git`` binary rather than mocking it, since parsing git's output is
most of what localmw does.
"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

#: An empty 'git init' template. The default one puts 14 sample hooks in every repository, which
#: is 40% of the files the fixtures create — and then copy again for every test.
_EMPTY_TEMPLATE = tempfile.mkdtemp(prefix="localmw-git-template-")
atexit.register(shutil.rmtree, _EMPTY_TEMPLATE, True)

GIT_ENV = {
    "GIT_AUTHOR_NAME": "localmw tests",
    "GIT_AUTHOR_EMAIL": "tests@example.invalid",
    "GIT_COMMITTER_NAME": "localmw tests",
    "GIT_COMMITTER_EMAIL": "tests@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "EMAIL": "tests@example.invalid",
    "LC_ALL": "C",
    "GIT_TEMPLATE_DIR": _EMPTY_TEMPLATE,
    # Settings every test repository wants, applied through the environment rather than with
    # 'git config' calls: the fixtures create a lot of repositories, and each git subprocess
    # costs more than the test that needs it.
    "GIT_CONFIG_COUNT": "3",
    "GIT_CONFIG_KEY_0": "commit.gpgsign",
    "GIT_CONFIG_VALUE_0": "false",
    "GIT_CONFIG_KEY_1": "advice.detachedHead",
    "GIT_CONFIG_VALUE_1": "false",
    "GIT_CONFIG_KEY_2": "init.defaultBranch",
    "GIT_CONFIG_VALUE_2": "master",
}


def git(path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, **GIT_ENV}
    proc = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        env=env,
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {path}:\n{proc.stderr}")
    return proc


def init_repo(path: Path, branch: str = "master") -> Path:
    """Create an empty repository with ``branch`` checked out."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "--quiet", f"--initial-branch={branch}")
    return path


def write(path: Path, name: str, content: str = "content\n") -> Path:
    target = path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def commit(
    path: Path,
    message: str = "a commit",
    *,
    name: str = "file.txt",
    content: str | None = None,
) -> str:
    """Commit a change and return the new HEAD sha."""
    write(path, name, content if content is not None else f"{message}\n")
    git(path, "add", "--all")
    git(path, "commit", "--quiet", "-m", message)
    return git(path, "rev-parse", "HEAD").stdout.strip()


def commit_with_change_id(path: Path, change_id: str, message: str = "a change", **kwargs) -> str:
    """Commit with a Gerrit ``Change-Id`` trailer, as ``git review`` would leave behind."""
    full = f"{message}\n\nBug: T1234\nChange-Id: {change_id}\n"
    return commit(path, full, name=kwargs.pop("name", "file.txt"), **kwargs)


def add_origin(repo: Path, bare: Path, branch: str = "master") -> Path:
    """Give ``repo`` a bare origin with ``branch`` pushed and tracked."""
    bare.mkdir(parents=True, exist_ok=True)
    git(bare, "init", "--quiet", "--bare")
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "--quiet", "--set-upstream", "origin", branch)
    # Point the bare repo's HEAD at the branch we pushed, so clones of it check out the same one.
    git(bare, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
    git(repo, "remote", "set-head", "origin", branch)
    return bare


def advance_origin(bare: Path, workdir: Path, message: str = "upstream commit", count: int = 1) -> None:
    """Push ``count`` new commits to ``bare`` from a scratch clone, so clones fall behind."""
    clone = workdir / f"clone-{bare.name}"
    if clone.exists():
        git(clone, "pull", "--quiet")
    else:
        subprocess.run(
            ["git", "clone", "--quiet", str(bare), str(clone)],
            check=True,
            capture_output=True,
            env={**os.environ, **GIT_ENV},
        )
    for index in range(count):
        commit(clone, f"{message} {index + 1}", name="upstream.txt", content=f"{message} {index + 1}\n")
    git(clone, "push", "--quiet")


def clone_snapshot(source: Path, dest: Path) -> Path:
    """Copy a prebuilt tree of repositories into ``dest``, repointing remotes at the copy.

    Building an install with real git costs about a second per test, and almost every test just
    wants a pristine one; copying is an order of magnitude quicker. The only place the old
    location is recorded is each ``.git/config``, since the remote URLs are absolute paths.
    """
    shutil.copytree(source, dest, dirs_exist_ok=True, symlinks=True)
    old, new = str(source), str(dest)
    for config in dest.glob("**/.git/config"):
        text = config.read_text(encoding="utf-8")
        if old in text:
            config.write_text(text.replace(old, new), encoding="utf-8")
    return dest


class Snapshots:
    """A session-scoped cache of prebuilt layouts, to be copied with :func:`clone_snapshot`."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self._built: dict[str, Path] = {}

    def get(self, key: str, build: Callable[[Path], object]) -> Path:
        """Return the snapshot for ``key``, calling ``build(path)`` the first time it is asked for."""
        path = self._built.get(key)
        if path is None:
            path = self.base / key
            path.mkdir(parents=True, exist_ok=True)
            build(path)
            self._built[key] = path
        return path


class FakeGerritClient:
    """Stands in for :class:`localmw.gerrit.GerritClient` with canned change statuses."""

    def __init__(self, statuses: dict | None = None, *, authenticated: bool = False, error: str | None = None):
        self.statuses = dict(statuses or {})
        self.authenticated = authenticated
        self.error = error
        self.queries: list = []

    def changes_by_change_id(self, change_ids, project=None):
        from localmw import gerrit

        change_ids = list(change_ids)
        self.queries.append({"change_ids": change_ids, "project": project})
        if self.error:
            raise gerrit.GerritError(self.error)

        result = {}
        for index, change_id in enumerate(change_ids):
            entry = self.statuses.get(change_id)
            if entry is None:
                result[change_id] = []
                continue
            status, branch = entry if isinstance(entry, tuple) else (entry, "master")
            result[change_id] = [
                gerrit.Change(
                    change_id=change_id,
                    number=1100000 + index,
                    project=project or "mediawiki/core",
                    branch=branch,
                    status=status,
                    subject="a change",
                    url=f"https://gerrit.example.org/c/{project}/+/{1100000 + index}",
                )
            ]
        return result

    def version(self):
        from localmw import gerrit

        if self.error:
            raise gerrit.GerritError(self.error)
        return "3.9.0"


def install_fake_gerrit(monkeypatch, statuses: dict | None = None, **kwargs) -> FakeGerritClient:
    """Patch ``client_from_config`` so no test ever reaches the network."""
    from localmw import gerrit

    client = FakeGerritClient(statuses, **kwargs)
    monkeypatch.setattr(gerrit, "client_from_config", lambda *a, **kw: client)
    return client


def make_install(root: Path) -> Path:
    """Create a directory that looks like a MediaWiki install (but is not a git repo yet)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "includes").mkdir(exist_ok=True)
    (root / "maintenance").mkdir(exist_ok=True)
    (root / "extensions").mkdir(exist_ok=True)
    (root / "skins").mkdir(exist_ok=True)
    (root / "includes" / "Setup.php").write_text("<?php\n", encoding="utf-8")
    (root / "RELEASE-NOTES-1.44").write_text("notes\n", encoding="utf-8")
    return root


def make_repo_with_origin(
    repo: Path,
    origins: Path,
    *,
    branch: str = "master",
    gitreview_project: str | None = None,
) -> Path:
    """Init ``repo``, make one commit, and wire it up to a fresh bare origin."""
    init_repo(repo, branch=branch)
    if gitreview_project:
        write(
            repo,
            ".gitreview",
            "[gerrit]\nhost=gerrit.wikimedia.org\nport=29418\n"
            f"project={gitreview_project}.git\ndefaultbranch={branch}\n",
        )
    commit(repo, "initial commit", name="README.md")
    add_origin(repo, origins / repo.name, branch=branch)
    return repo


def build_install(
    root: Path,
    origins: Path,
    *,
    extensions: Sequence[str] = ("AbuseFilter", "Echo"),
    skins: Sequence[str] = ("Vector",),
    with_vendor: bool = True,
    core_project: str = "mediawiki/core",
) -> Path:
    """A full install: core, vendor, extensions and skins, each with an origin."""
    make_install(root)
    make_repo_with_origin(root, origins, gitreview_project=core_project)

    if with_vendor:
        make_repo_with_origin(root / "vendor", origins, gitreview_project="mediawiki/vendor")

    for name in extensions:
        make_repo_with_origin(
            root / "extensions" / name,
            origins,
            gitreview_project=f"mediawiki/extensions/{name}",
        )
    for name in skins:
        make_repo_with_origin(
            root / "skins" / name,
            origins,
            gitreview_project=f"mediawiki/skins/{name}",
        )
    return root
