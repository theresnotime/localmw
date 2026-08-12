"""``localmw pull`` — bring core, extensions and skins up to date, carefully."""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass

import click
from rich.markup import escape
from rich.table import Column

from .. import gitutil, ui
from ..config import PULL_STRATEGIES
from ..context import AppContext, require_repos, resolve_kinds, selection_options
from ..install import Repo

UPDATED = "updated"
UP_TO_DATE = "up to date"
WOULD_UPDATE = "would update"
SKIPPED = "skipped"
FAILED = "failed"

#: Order used when listing results: problems first.
_STATUS_ORDER = {FAILED: 0, SKIPPED: 1, WOULD_UPDATE: 2, UPDATED: 3, UP_TO_DATE: 4}


@dataclass
class PullOutcome:
    repo: Repo
    status: str
    branch: str | None = None
    commits: int = 0
    ahead: int = 0
    reason: str = ""
    hint: str = ""

    @property
    def interesting(self) -> bool:
        """Worth a row in the results table."""
        return self.status != UP_TO_DATE or bool(self.ahead)

    @property
    def sort_key(self):
        return (_STATUS_ORDER.get(self.status, 9), self.repo.sort_key)


def pull_repo(
    repo: Repo,
    *,
    default_branches: Sequence[str],
    strategy: str,
    allow_dirty: bool,
    allow_branch: bool,
    prune: bool,
    submodules: bool,
    dry_run: bool,
    do_fetch: bool = True,
) -> PullOutcome:
    """Update a single repository, refusing to do anything surprising.

    ``do_fetch=False`` reuses whatever the last fetch left in the remote-tracking refs, which is
    how --interactive pulls the repositories you said yes to without fetching them twice. Every
    check is still made against the working tree as it is now.
    """
    state = gitutil.read_state(repo.path)
    if state.error:
        return PullOutcome(repo, FAILED, reason=state.error)

    branch = state.branch
    if state.detached:
        return PullOutcome(repo, SKIPPED, branch="(detached)", reason="detached HEAD")

    default_branch = gitutil.default_branch(repo.path, default_branches)
    on_default = branch == default_branch if default_branch else branch in tuple(default_branches)
    if not on_default and not allow_branch:
        return PullOutcome(
            repo,
            SKIPPED,
            branch=branch,
            reason=f"on branch {branch}",
            hint="--any-branch to pull anyway",
        )

    if state.dirty and not allow_dirty:
        return PullOutcome(
            repo,
            SKIPPED,
            branch=branch,
            reason=f"{ui.plural(state.tracked_changes, 'uncommitted change')}",
            hint="--allow-dirty to pull anyway",
        )

    if do_fetch:
        try:
            gitutil.fetch(repo.path, prune=prune)
        except gitutil.GitError as exc:
            return PullOutcome(repo, FAILED, branch=branch, reason=f"fetch: {exc.detail}")

    # Re-read so ahead/behind reflect what we just fetched.
    state = gitutil.read_state(repo.path)
    if state.error:
        return PullOutcome(repo, FAILED, branch=branch, reason=state.error)
    if not state.has_upstream:
        return PullOutcome(repo, SKIPPED, branch=branch, reason="no upstream branch")

    if state.behind == 0:
        return PullOutcome(repo, UP_TO_DATE, branch=branch, ahead=state.ahead)

    if state.ahead and strategy == "ff-only":
        return PullOutcome(
            repo,
            SKIPPED,
            branch=branch,
            ahead=state.ahead,
            commits=state.behind,
            reason=f"diverged: {ui.plural(state.ahead, 'local commit')}",
            hint="--strategy rebase to replay them on top",
        )

    if dry_run:
        return PullOutcome(repo, WOULD_UPDATE, branch=branch, commits=state.behind, ahead=state.ahead)

    upstream = state.upstream or ""
    try:
        gitutil.integrate(repo.path, upstream, strategy)
    except gitutil.GitError as exc:
        return PullOutcome(repo, FAILED, branch=branch, reason=exc.detail)

    outcome = PullOutcome(repo, UPDATED, branch=branch, commits=state.behind, ahead=state.ahead)

    if submodules:
        try:
            gitutil.update_submodules(repo.path)
        except gitutil.GitError as exc:
            outcome.hint = f"submodules: {exc.detail}"

    return outcome


def _status_cell(outcome: PullOutcome) -> str:
    if outcome.status == UPDATED:
        return f"[localmw.ok]updated[/] [localmw.muted]({ui.plural(outcome.commits, 'commit')})[/]"
    if outcome.status == WOULD_UPDATE:
        return f"[localmw.behind]would update[/] [localmw.muted]({ui.plural(outcome.commits, 'commit')})[/]"
    if outcome.status == FAILED:
        return "[localmw.error]failed[/]"
    if outcome.status == SKIPPED:
        return "[localmw.warn]skipped[/]"
    return "[localmw.muted]up to date[/]"


def _detail_cell(outcome: PullOutcome) -> str:
    parts: list[str] = []
    if outcome.reason:
        parts.append(escape(outcome.reason))
    if outcome.ahead and outcome.status in (UP_TO_DATE, UPDATED):
        parts.append(f"{ui.plural(outcome.ahead, 'unpushed commit')}")
    if outcome.hint:
        parts.append(f"[localmw.muted]{escape(outcome.hint)}[/]")
    return " · ".join(parts)


def log_outcome(outcome: PullOutcome, width: int = 0) -> None:
    """The --verbose line for one repository, printed as soon as it has been dealt with."""
    if outcome.status == FAILED:
        level = "problem"
    elif outcome.status == SKIPPED:
        level = "attention"
    else:
        level = "ok"
    branch = f"[localmw.muted]{escape(outcome.branch)}[/]" if outcome.branch else ""
    ui.log_repo(
        outcome.repo.label,
        [branch, _status_cell(outcome), _detail_cell(outcome)],
        level=level,
        width=width,
    )


def render_results(outcomes: Sequence[PullOutcome], verbose: bool) -> None:
    rows = [o for o in outcomes if verbose or o.interesting]
    if not rows:
        return
    table = ui.new_table(
        Column("Repository", no_wrap=True),
        Column("Branch", no_wrap=True, overflow="ellipsis"),
        Column("Result", no_wrap=True),
        "Detail",
    )
    for outcome in sorted(rows, key=lambda o: o.sort_key):
        table.add_row(
            f"[localmw.name]{escape(outcome.repo.label)}[/]",
            f"[localmw.muted]{escape(outcome.branch or '')}[/]",
            _status_cell(outcome),
            _detail_cell(outcome),
        )
    ui.console.print(table)
    ui.console.print()


#: What counts as "Pull" and "Skip" at the --interactive prompt. y/n are accepted too, since a
#: prompt with two options trains fingers to type them.
_PULL_ANSWERS = {"p", "pull", "y", "yes"}
_SKIP_ANSWERS = {"s", "skip", "n", "no"}


def behind_by(outcome: PullOutcome) -> str:
    return f"{ui.plural(outcome.commits, 'commit')} behind"


def ask_about(outcome: PullOutcome, width: int = 0, detail_width: int = 0) -> bool:
    """Ask whether to pull one repository. Pull is the default: it is why you ran ``pull``."""
    label = f"{outcome.repo.label:<{width}}" if width else outcome.repo.label
    detail = f"{behind_by(outcome):<{detail_width}}" if detail_width else behind_by(outcome)
    question = f"  {label}  {detail}  [P]ull / [s]kip"
    while True:
        answer = click.prompt(question, default="pull", show_default=False, prompt_suffix=" ").strip().lower()
        if answer in _PULL_ANSWERS:
            return True
        if answer in _SKIP_ANSWERS:
            return False
        ui.muted("  'p' to pull, 's' to skip, or Enter to pull")


def skip_at_prompt(outcome: PullOutcome) -> None:
    """Record that the user said no to a repository we would otherwise have pulled."""
    outcome.status = SKIPPED
    outcome.reason = behind_by(outcome)
    outcome.hint = "skipped at the prompt"


def _adopt(outcome: PullOutcome, fresh: PullOutcome) -> PullOutcome:
    """Copy the result of a second pass onto the outcome the plan already knows about."""
    for field in dataclasses.fields(outcome):
        if field.name != "repo":
            setattr(outcome, field.name, getattr(fresh, field.name))
    return outcome


@click.command("pull")
@selection_options
@click.option("--allow-dirty", is_flag=True, help="Pull even where there are uncommitted changes.")
@click.option(
    "--any-branch",
    "allow_branch",
    is_flag=True,
    help="Pull even where a non-default branch (not master/main) is checked out.",
)
@click.option(
    "--strategy",
    type=click.Choice(PULL_STRATEGIES),
    default=None,
    help="How to integrate upstream commits (default: from config, ff-only).",
)
@click.option("--submodules/--no-submodules", "submodules", default=None, help="Update submodules after pulling.")
@click.option("--prune", is_flag=True, help="Prune deleted remote-tracking branches while fetching.")
@click.option(
    "-i",
    "--interactive",
    is_flag=True,
    help="Ask about each repository that has upstream commits: Pull or Skip.",
)
@click.option("-n", "--dry-run", is_flag=True, help="Report what would be pulled, changing nothing.")
@click.option("-j", "--jobs", type=int, default=None, help="Repositories to process concurrently.")
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Log each repository as it is pulled instead of drawing a progress bar, and list every "
    "repository in the results, not just the interesting ones.",
)
@click.pass_obj
def pull_command(
    ctx: AppContext,
    want_core: bool,
    want_extensions: bool,
    want_skins: bool,
    want_vendor: bool,
    skip_vendor: bool,
    only: tuple[str, ...],
    exclude: tuple[str, ...],
    allow_dirty: bool,
    allow_branch: bool,
    strategy: str | None,
    submodules: bool | None,
    prune: bool,
    interactive: bool,
    dry_run: bool,
    jobs: int | None,
    verbose: bool,
) -> None:
    """Pull the latest changes for core, extensions and skins.

    By default every repository is updated, but only where it is safe to do so: repositories
    with uncommitted changes, or with something other than master/main checked out, are
    reported and left alone.

    \b
    Examples:
      localmw pull                     # everything that is safe to fast-forward
      localmw pull --extensions        # just extensions/*
      localmw pull --core --skins      # core and skins/*
      localmw pull -o Vector -o Echo   # just those two
      localmw pull --dry-run           # show what would change
      localmw pull --interactive       # ask about each one: Pull or Skip
    """
    if interactive and dry_run:
        raise click.UsageError("--interactive cannot be combined with --dry-run, which never changes anything")

    kinds = resolve_kinds(want_core, want_extensions, want_skins, want_vendor, skip_vendor)
    verbose = ctx.be_verbose(verbose)
    discovery = ctx.discover(kinds, only, exclude)
    require_repos(discovery)
    ctx.announce_root(len(discovery))

    effective_strategy = strategy or ctx.config.pull_strategy
    effective_submodules = ctx.config.pull_submodules if submodules is None else submodules
    effective_jobs = ctx.jobs(jobs)
    label_width = max(len(repo.label) for repo in discovery.repos)

    rules = {
        "default_branches": ctx.config.default_branches,
        "strategy": effective_strategy,
        "allow_dirty": allow_dirty,
        "allow_branch": allow_branch,
        "prune": prune,
        "submodules": effective_submodules,
    }

    def worker(repo: Repo) -> PullOutcome:
        # --interactive checks first and pulls afterwards, so its first pass changes nothing
        # either — exactly what --dry-run does.
        return pull_repo(repo, dry_run=dry_run or interactive, **rules)

    def pull_now(outcome: PullOutcome) -> PullOutcome:
        """Pull a repository the user said yes to; the checking pass already fetched it."""
        return _adopt(outcome, pull_repo(outcome.repo, dry_run=False, do_fetch=False, **rules))

    def logger(item, outcome: PullOutcome) -> None:
        log_outcome(outcome, label_width)

    description = "Checking" if dry_run or interactive else "Pulling"
    if verbose:
        ctx.announce_work(description, len(discovery), effective_jobs)

    outcomes = ui.run_parallel(
        discovery.repos,
        worker,
        jobs=effective_jobs,
        description=description,
        show_progress=not verbose,
        on_result=logger if verbose else None,
    )
    if verbose:
        ui.console.print()

    if interactive:
        pending = [outcome for outcome in outcomes if outcome.status == WOULD_UPDATE]
        if pending:
            ui.console.print(f"{ui.plural(len(pending), 'repository has', 'repositories have')} upstream commits:")
            detail_width = max(len(behind_by(outcome)) for outcome in pending)
            try:
                decisions = [(outcome, ask_about(outcome, label_width, detail_width)) for outcome in pending]
            except click.Abort:
                ui.muted("aborted; nothing was pulled")
                return
            wanted = [outcome for outcome, yes in decisions if yes]
            for outcome, yes in decisions:
                if not yes:
                    skip_at_prompt(outcome)
            ui.console.print()

            if wanted:
                if verbose:
                    ctx.announce_work("Pulling", len(wanted), effective_jobs)
                ui.run_parallel(
                    wanted,
                    pull_now,
                    jobs=effective_jobs,
                    description="Pulling",
                    show_progress=not verbose,
                    on_result=logger if verbose else None,
                )
                if verbose:
                    ui.console.print()

    render_results(outcomes, verbose=verbose)

    counts = {status: sum(1 for o in outcomes if o.status == status) for status in _STATUS_ORDER}
    parts = [ui.plural(len(outcomes), "repository", "repositories")]
    if counts[UPDATED]:
        parts.append(f"[localmw.ok]{counts[UPDATED]} updated[/]")
    if counts[WOULD_UPDATE]:
        parts.append(f"[localmw.behind]{counts[WOULD_UPDATE]} would update[/]")
    if counts[UP_TO_DATE]:
        parts.append(f"[localmw.muted]{counts[UP_TO_DATE]} up to date[/]")
    if counts[SKIPPED]:
        parts.append(f"[localmw.warn]{counts[SKIPPED]} skipped[/]")
    if counts[FAILED]:
        parts.append(f"[localmw.error]{counts[FAILED]} failed[/]")
    ui.console.print(ui.join_parts(parts))

    if dry_run:
        ui.muted("dry run — nothing was changed")

    if counts[FAILED]:
        raise SystemExit(1)
