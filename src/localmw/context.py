"""Shared CLI context and the repository-selection options used by several commands."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import click

from . import ui
from .config import Config
from .install import (
    ALL_KINDS,
    KIND_CORE,
    KIND_EXTENSION,
    KIND_SKIN,
    KIND_VENDOR,
    Discovery,
    InstallError,
    discover,
    find_install,
)


@dataclass
class AppContext:
    """Everything the commands need from the top-level invocation."""

    config: Config
    mw_override: Path | None = None
    quiet: bool = False
    verbose: bool = False
    _root: Path | None = field(default=None, init=False, repr=False)

    @property
    def root(self) -> Path:
        """The MediaWiki install to operate on, resolved once per run."""
        if self._root is None:
            try:
                self._root = find_install(
                    explicit=self.mw_override,
                    configured=self.config.mediawiki_dir,
                )
            except InstallError as exc:
                raise click.ClickException(str(exc)) from None
        return self._root

    def jobs(self, override: int | None = None) -> int:
        if override is not None:
            if override < 1:
                raise click.BadParameter("--jobs must be at least 1")
            return override
        return self.config.jobs

    def discover(
        self,
        kinds: Sequence[str] = ALL_KINDS,
        only: Iterable[str] = (),
        exclude: Iterable[str] = (),
    ) -> Discovery:
        """Scan the install, folding in the configured ``exclude`` patterns."""
        combined_exclude = [*self.config.exclude, *exclude]
        discovery = discover(self.root, kinds=kinds, only=only, exclude=combined_exclude)

        if not discovery.core_is_git:
            ui.warn(f"{discovery.root} is not a git checkout; skipping core")
        if discovery.non_git and not self.quiet:
            ui.muted(
                f"ignoring {ui.plural(len(discovery.non_git), 'directory', 'directories')} without a "
                f".git: {', '.join(sorted(discovery.non_git)[:5])}" + (" ..." if len(discovery.non_git) > 5 else "")
            )
        return discovery

    def announce_root(self, count: int | None = None) -> None:
        if self.quiet:
            return
        suffix = f" · {ui.plural(count, 'repository', 'repositories')}" if count is not None else ""
        ui.muted(f"{self.root}{suffix}")

    def be_verbose(self, command_flag: bool = False) -> bool:
        """--verbose accepted either before or after the subcommand."""
        self.verbose = self.verbose or command_flag
        return self.verbose

    def announce_work(self, description: str, count: int, jobs: int) -> None:
        """In verbose mode, say what is about to happen instead of drawing a progress bar."""
        if not self.verbose:
            return
        concurrency = f"{jobs} at a time" if jobs > 1 and count > 1 else "one at a time"
        ui.log(f"[localmw.muted]{description} {ui.plural(count, 'repository', 'repositories')} ({concurrency})[/]")


def selection_options(func):
    """Add the ``--core/--extensions/--skins/--vendor/-o/-x`` family to a command."""
    options = (
        click.option("--core", "want_core", is_flag=True, help="Include MediaWiki core."),
        click.option("--extensions", "want_extensions", is_flag=True, help="Include extensions/*."),
        click.option("--skins", "want_skins", is_flag=True, help="Include skins/*."),
        click.option("--vendor", "want_vendor", is_flag=True, help="Include vendor/."),
        click.option(
            "--no-vendor",
            "skip_vendor",
            is_flag=True,
            help="Exclude vendor/, which is otherwise included.",
        ),
        click.option(
            "-o",
            "--only",
            multiple=True,
            metavar="PATTERN",
            help="Only repositories matching PATTERN (glob, case-insensitive, repeatable).",
        ),
        click.option(
            "-x",
            "--exclude",
            multiple=True,
            metavar="PATTERN",
            help="Skip repositories matching PATTERN (glob, case-insensitive, repeatable).",
        ),
    )
    for option in reversed(options):
        func = option(func)
    return func


def resolve_kinds(
    want_core: bool = False,
    want_extensions: bool = False,
    want_skins: bool = False,
    want_vendor: bool = False,
    skip_vendor: bool = False,
) -> tuple[str, ...]:
    """Turn the selection flags into a tuple of repo kinds. No flags means everything."""
    selected = []
    if want_core:
        selected.append(KIND_CORE)
    if want_vendor:
        selected.append(KIND_VENDOR)
    if want_extensions:
        selected.append(KIND_EXTENSION)
    if want_skins:
        selected.append(KIND_SKIN)

    if not selected:
        selected = list(ALL_KINDS)

    if skip_vendor and KIND_VENDOR in selected:
        selected.remove(KIND_VENDOR)

    return tuple(selected)


def require_repos(discovery: Discovery) -> None:
    if not discovery.repos:
        raise click.ClickException("no matching git repositories found (check your --only/--exclude patterns)")
