"""``localmw status`` — what state is this install in, and does anything need updating?"""

from __future__ import annotations

import json as jsonlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import click
from rich.markup import escape
from rich.table import Column

from .. import gitutil, ui
from ..context import AppContext, require_repos, resolve_kinds, selection_options
from ..install import Repo


@dataclass
class RepoStatus:
    """A repository plus the interpretation ``status`` needs."""

    repo: Repo
    state: gitutil.RepoState
    default_branch: str | None = None
    allowed_branches: tuple[str, ...] = ()

    @property
    def on_default_branch(self) -> bool:
        branch = self.state.branch
        if branch is None:
            return False
        if self.default_branch:
            return branch == self.default_branch
        return branch in self.allowed_branches

    @property
    def diverged(self) -> bool:
        return bool(self.state.ahead and self.state.behind)

    @property
    def needs_update(self) -> bool:
        return bool(self.state.behind)

    @property
    def needs_attention(self) -> bool:
        return bool(
            self.state.error
            or self.state.fetch_error
            or self.state.detached
            or not self.on_default_branch
            or self.state.dirty
            or self.state.behind
            or not self.state.has_upstream
        )

    @property
    def notes(self) -> list[str]:
        notes: list[str] = []
        if self.state.error:
            notes.append(self.state.error)
        if self.state.fetch_error:
            notes.append(f"fetch failed: {self.state.fetch_error}")
        if self.diverged:
            notes.append("diverged from upstream")
        return notes

    def to_dict(self) -> dict[str, Any]:
        state = self.state
        return {
            "kind": self.repo.kind,
            "name": self.repo.name,
            "path": str(self.repo.path),
            "label": self.repo.label,
            "branch": state.branch,
            "detached": state.detached,
            "default_branch": self.default_branch,
            "on_default_branch": self.on_default_branch,
            "head": state.head,
            "upstream": state.upstream,
            "ahead": state.ahead,
            "behind": state.behind,
            "staged": state.staged,
            "unstaged": state.unstaged,
            "untracked": state.untracked,
            "conflicts": state.conflicts,
            "dirty": state.dirty,
            "needs_update": self.needs_update,
            "needs_attention": self.needs_attention,
            "last_commit": {
                "sha": state.last_commit.sha,
                "subject": state.last_commit.subject,
                "relative_date": state.last_commit.relative_date,
                "author": state.last_commit.author,
            },
            "error": state.error,
            "fetch_error": state.fetch_error,
            "notes": self.notes,
        }


def collect(
    repos: Sequence[Repo],
    *,
    default_branches: Sequence[str],
    do_fetch: bool,
    prune: bool,
    jobs: int,
    show_progress: bool = True,
    on_result: Callable[[Repo, RepoStatus], None] | None = None,
) -> list[RepoStatus]:
    """Read the state of every repository, optionally fetching first."""

    def worker(repo: Repo) -> RepoStatus:
        fetch_error: str | None = None
        if do_fetch:
            try:
                gitutil.fetch(repo.path, prune=prune)
            except gitutil.GitError as exc:
                fetch_error = exc.detail

        state = gitutil.read_state(repo.path)
        state.fetch_error = fetch_error

        default_branch = None
        if not state.error:
            default_branch = gitutil.default_branch(repo.path, default_branches)

        return RepoStatus(
            repo=repo,
            state=state,
            default_branch=default_branch,
            allowed_branches=tuple(default_branches),
        )

    description = "Fetching" if do_fetch else "Reading"
    return ui.run_parallel(
        list(repos),
        worker,
        jobs=jobs,
        description=description,
        show_progress=show_progress,
        on_result=on_result,
    )


def log_status(status: RepoStatus, width: int = 0) -> None:
    """The --verbose line for one repository, printed as soon as it has been read."""
    if status.state.error:
        level = "problem"
    elif status.needs_attention:
        level = "attention"
    else:
        level = "ok"
    parts = [_branch_cell(status), _sync_cell(status), _worktree_cell(status), *map(escape, status.notes)]
    ui.log_repo(status.repo.label, parts, level=level, width=width)


def _branch_cell(status: RepoStatus) -> str:
    state = status.state
    if state.error:
        return "[localmw.error]?[/]"
    if state.detached:
        return f"[localmw.warn]detached @ {escape(state.head[:7])}[/]"
    branch = escape(state.branch or "?")
    if status.on_default_branch:
        return branch
    return f"[localmw.warn]{branch}[/]"


def _sync_cell(status: RepoStatus) -> str:
    state = status.state
    if state.error:
        return ""
    if not state.has_upstream:
        return "[localmw.warn]no upstream[/]"
    parts = []
    if state.behind:
        parts.append(f"[localmw.behind]{state.behind} behind[/]")
    if state.ahead:
        parts.append(f"[localmw.ahead]{state.ahead} ahead[/]")
    if not parts:
        return "[localmw.muted]up to date[/]"
    return " ".join(parts)


def _worktree_cell(status: RepoStatus) -> str:
    summary = status.state.worktree_summary()
    if summary == "clean":
        return "[localmw.muted]clean[/]"
    if status.state.dirty:
        return f"[localmw.warn]{summary}[/]"
    return f"[localmw.muted]{summary}[/]"


def render_table(statuses: Sequence[RepoStatus]) -> None:
    table = ui.new_table(
        Column("Repository", no_wrap=True),
        Column("Branch", no_wrap=True, overflow="ellipsis"),
        Column("Upstream", no_wrap=True),
        Column("Working tree", no_wrap=True),
        Column("Last commit", no_wrap=True, overflow="ellipsis"),
        "Notes",
    )
    for status in statuses:
        table.add_row(
            f"[localmw.name]{escape(status.repo.label)}[/]",
            _branch_cell(status),
            _sync_cell(status),
            _worktree_cell(status),
            f"[localmw.muted]{escape(status.state.last_commit.relative_date)}[/]",
            "[localmw.error]" + escape("; ".join(status.notes)) + "[/]" if status.notes else "",
        )
    ui.console.print(table)


def summarise(statuses: Sequence[RepoStatus], stale_note: str = "") -> None:
    behind = [s for s in statuses if s.needs_update]
    dirty = [s for s in statuses if s.state.dirty]
    off_branch = [s for s in statuses if not s.on_default_branch and not s.state.detached and not s.state.error]
    detached = [s for s in statuses if s.state.detached]
    errored = [s for s in statuses if s.state.error or s.state.fetch_error]

    parts = [ui.plural(len(statuses), "repository", "repositories")]
    if behind:
        parts.append(f"[localmw.behind]{len(behind)} behind[/]")
    if dirty:
        parts.append(f"[localmw.warn]{len(dirty)} with local changes[/]")
    if off_branch:
        parts.append(f"[localmw.warn]{len(off_branch)} on another branch[/]")
    if detached:
        parts.append(f"[localmw.warn]{len(detached)} detached[/]")
    if errored:
        parts.append(f"[localmw.error]{len(errored)} with errors[/]")
    if len(parts) == 1:
        parts.append("[localmw.ok]all clean and up to date[/]")

    ui.console.print(ui.join_parts(parts))

    if behind:
        names = ", ".join(escape(s.repo.label) for s in behind[:6])
        more = f" (+{len(behind) - 6} more)" if len(behind) > 6 else ""
        ui.muted(f"behind: {names}{more} — run 'localmw pull' to update")
    if stale_note:
        ui.muted(stale_note)


def run_report(
    ctx: AppContext,
    *,
    kinds: Sequence[str],
    only: tuple[str, ...],
    exclude: tuple[str, ...],
    do_fetch: bool,
    prune: bool,
    jobs: int | None,
    attention_only: bool,
    as_json: bool,
    verbose: bool,
    stale_note: str = "",
) -> None:
    """The shared body of ``localmw status`` and ``localmw list``."""
    verbose = ctx.be_verbose(verbose)
    quiet_for_json = as_json

    if quiet_for_json:
        ctx.quiet = True
    discovery = ctx.discover(kinds, only, exclude)
    require_repos(discovery)
    if not quiet_for_json:
        ctx.announce_root(len(discovery))

    label_width = max(len(repo.label) for repo in discovery.repos)

    def logger(repo: Repo, status: RepoStatus) -> None:
        log_status(status, label_width)

    if verbose:
        ctx.announce_work("Fetching" if do_fetch else "Reading", len(discovery), ctx.jobs(jobs))

    statuses = collect(
        discovery.repos,
        default_branches=ctx.config.default_branches,
        do_fetch=do_fetch,
        prune=prune,
        jobs=ctx.jobs(jobs),
        show_progress=not quiet_for_json and not verbose,
        on_result=logger if verbose else None,
    )
    if verbose and not as_json:
        ui.console.print()

    shown = [s for s in statuses if s.needs_attention] if attention_only else list(statuses)

    if as_json:
        payload = {
            "root": str(discovery.root),
            "fetched": do_fetch,
            "repositories": [status.to_dict() for status in shown],
            "summary": {
                "total": len(statuses),
                "behind": sum(1 for s in statuses if s.needs_update),
                "dirty": sum(1 for s in statuses if s.state.dirty),
                "off_default_branch": sum(1 for s in statuses if not s.on_default_branch and not s.state.error),
                "errors": sum(1 for s in statuses if s.state.error or s.state.fetch_error),
            },
        }
        click.echo(jsonlib.dumps(payload, indent=2))
    else:
        if shown:
            render_table(shown)
        elif attention_only:
            ui.console.print("[localmw.ok]Nothing needs attention.[/]")
        ui.console.print()
        summarise(statuses, stale_note)

    if any(s.state.error for s in statuses):
        raise SystemExit(1)


@click.command("status")
@selection_options
@click.option("--fetch/--no-fetch", "do_fetch", default=True, help="Fetch from remotes first (default: fetch).")
@click.option("--prune", is_flag=True, help="Prune deleted remote-tracking branches while fetching.")
@click.option("-j", "--jobs", type=int, default=None, help="Repositories to process concurrently.")
@click.option("--attention", "attention_only", is_flag=True, help="Only list repositories that need attention.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON instead of a table.")
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Log each repository as it is read instead of drawing a progress bar.",
)
@click.pass_obj
def status_command(
    ctx: AppContext,
    want_core: bool,
    want_extensions: bool,
    want_skins: bool,
    want_vendor: bool,
    skip_vendor: bool,
    only: tuple[str, ...],
    exclude: tuple[str, ...],
    do_fetch: bool,
    prune: bool,
    jobs: int | None,
    attention_only: bool,
    as_json: bool,
    verbose: bool,
) -> None:
    """Report the state of core, extensions and skins.

    Shows the checked-out branch, how far behind/ahead of its upstream each repository is, and
    whether it has uncommitted changes. Fetches from remotes first, which is what makes 'behind'
    meaningful; 'localmw list' is the same report without the network.
    """
    run_report(
        ctx,
        kinds=resolve_kinds(want_core, want_extensions, want_skins, want_vendor, skip_vendor),
        only=only,
        exclude=exclude,
        do_fetch=do_fetch,
        prune=prune,
        jobs=jobs,
        attention_only=attention_only,
        as_json=as_json,
        verbose=verbose,
        stale_note="" if do_fetch else "ran with --no-fetch, so 'behind' counts may be stale",
    )
