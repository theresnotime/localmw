"""Locating a MediaWiki install and enumerating the git repositories inside it."""

from __future__ import annotations

import contextlib
import fnmatch
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import gitutil

KIND_CORE = "core"
KIND_VENDOR = "vendor"
KIND_EXTENSION = "extension"
KIND_SKIN = "skin"

#: Ordered for display: core first, then its bundled repos.
ALL_KINDS: tuple[str, ...] = (KIND_CORE, KIND_VENDOR, KIND_EXTENSION, KIND_SKIN)

_KIND_ORDER = {kind: index for index, kind in enumerate(ALL_KINDS)}

#: How deep to look for an install when walking up from the current directory.
MAX_PARENT_WALK = 12


class InstallError(RuntimeError):
    """The target directory is missing or is not a MediaWiki install."""


@dataclass(frozen=True)
class Repo:
    """One git checkout within a MediaWiki install."""

    kind: str
    name: str
    path: Path
    root: Path

    @property
    def rel_path(self) -> str:
        try:
            rel = self.path.relative_to(self.root)
        except ValueError:  # pragma: no cover - defensive
            return str(self.path)
        return "." if str(rel) == "." else str(rel)

    @property
    def label(self) -> str:
        """Short human label, e.g. ``core`` or ``extensions/Vector``."""
        if self.kind in (KIND_CORE, KIND_VENDOR):
            return self.kind
        return self.rel_path

    def matches(self, pattern: str) -> bool:
        """Case-insensitive glob match against the name, kind, or relative path."""
        pattern = pattern.strip()
        if not pattern:
            return False
        candidates = (self.name, self.kind, self.rel_path, self.label)
        lowered = pattern.lower()
        return any(fnmatch.fnmatch(candidate.lower(), lowered) for candidate in candidates)

    @property
    def sort_key(self) -> tuple[int, str]:
        return (_KIND_ORDER.get(self.kind, len(ALL_KINDS)), self.name.lower())


@dataclass
class Discovery:
    """The result of scanning an install."""

    root: Path
    repos: list[Repo] = field(default_factory=list)
    non_git: list[str] = field(default_factory=list)
    core_is_git: bool = True

    def __iter__(self):
        return iter(self.repos)

    def __len__(self) -> int:
        return len(self.repos)


def looks_like_install(path: Path) -> bool:
    """Heuristic check that ``path`` is the root of a MediaWiki install."""
    if not path.is_dir():
        return False
    if not ((path / "includes").is_dir() and (path / "maintenance").is_dir()):
        return False
    strong_markers = (
        (path / "includes" / "Setup.php").is_file(),
        (path / "mw-config").is_dir(),
        (path / "extensions").is_dir() and (path / "skins").is_dir(),
        any(path.glob("RELEASE-NOTES-*")),
    )
    return any(strong_markers)


def find_install(
    explicit: Path | None = None,
    configured: Path | None = None,
    start: Path | None = None,
) -> Path:
    """Work out which MediaWiki install to operate on.

    Resolution order: ``--mw`` (explicit), then the current directory or the nearest parent
    that looks like an install, then the configured default.
    """
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise InstallError(f"{path} does not exist")
        path = path.resolve()
        if not looks_like_install(path):
            raise InstallError(f"{path} does not look like a MediaWiki install")
        return path

    start = (Path(start) if start else Path.cwd()).expanduser()
    with contextlib.suppress(OSError):  # pragma: no cover - unreadable cwd
        start = start.resolve()
    for candidate in (start, *list(start.parents)[:MAX_PARENT_WALK]):
        if looks_like_install(candidate):
            return candidate

    if configured is not None:
        path = Path(configured).expanduser()
        if not path.exists():
            raise InstallError(f"configured mediawiki_dir {path} does not exist")
        path = path.resolve()
        if not looks_like_install(path):
            raise InstallError(f"configured mediawiki_dir {path} does not look like a MediaWiki install")
        return path

    raise InstallError(
        f"{start} is not a MediaWiki install. Run localmw from inside one, pass --mw /path/to/mw, "
        "or set a default with: localmw config set mediawiki_dir /path/to/mw"
    )


def _child_repos(root: Path, parent: Path, kind: str, non_git: list[str]) -> list[Repo]:
    if not parent.is_dir():
        return []
    repos: list[Repo] = []
    for entry in sorted(parent.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if gitutil.is_repo(entry):
            repos.append(Repo(kind=kind, name=entry.name, path=entry, root=root))
        else:
            non_git.append(f"{parent.name}/{entry.name}")
    return repos


def discover(
    root: Path,
    kinds: Sequence[str] = ALL_KINDS,
    only: Iterable[str] = (),
    exclude: Iterable[str] = (),
) -> Discovery:
    """Enumerate the git repositories in an install, applying the given filters."""
    kinds = tuple(kinds) or ALL_KINDS
    only = [pattern for pattern in only if pattern]
    exclude = [pattern for pattern in exclude if pattern]

    result = Discovery(root=root)
    candidates: list[Repo] = []

    core_is_git = gitutil.is_repo(root)
    result.core_is_git = core_is_git
    if KIND_CORE in kinds and core_is_git:
        candidates.append(Repo(kind=KIND_CORE, name="core", path=root, root=root))

    if KIND_VENDOR in kinds and gitutil.is_repo(root / "vendor"):
        candidates.append(Repo(kind=KIND_VENDOR, name="vendor", path=root / "vendor", root=root))

    if KIND_EXTENSION in kinds:
        candidates.extend(_child_repos(root, root / "extensions", KIND_EXTENSION, result.non_git))
    if KIND_SKIN in kinds:
        candidates.extend(_child_repos(root, root / "skins", KIND_SKIN, result.non_git))

    for repo in candidates:
        if only and not any(repo.matches(pattern) for pattern in only):
            continue
        if exclude and any(repo.matches(pattern) for pattern in exclude):
            continue
        result.repos.append(repo)

    result.repos.sort(key=lambda repo: repo.sort_key)
    return result
