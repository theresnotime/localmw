"""``localmw switch`` — put repositories back onto master/main."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import click
from rich.markup import escape
from rich.table import Column

from .. import gitutil, ui
from ..context import AppContext, require_repos, resolve_kinds, selection_options
from ..install import Repo

SWITCHED = "switched"
ALREADY = "already there"
WOULD_SWITCH = "would switch"
SKIPPED = "skipped"
FAILED = "failed"

#: Order used when listing results: problems first, nothing-to-do last.
_STATUS_ORDER = {FAILED: 0, SKIPPED: 1, WOULD_SWITCH: 2, SWITCHED: 3, ALREADY: 4}


@dataclass
class SwitchPlan:
    """What we intend to do to one repository, and then what we did."""

    repo: Repo
    status: str = ALREADY
    from_branch: str = ""
    target: str | None = None
    #: Set when the default branch has to be created from its remote-tracking ref first.
    create_from: str | None = None
    #: How many tracked changes --discard-changes will throw away.
    discard: int = 0
    behind: int = 0
    reason: str = ""
    hint: str = ""

    @property
    def interesting(self) -> bool:
        """Worth a row in the results table."""
        return self.status != ALREADY

    @property
    def sort_key(self):
        return (_STATUS_ORDER.get(self.status, 9), self.repo.sort_key)


def plan_switch(repo: Repo, *, default_branches: Sequence[str], discard_changes: bool) -> SwitchPlan:
    """Decide what to do with one repository, without changing anything."""
    plan = SwitchPlan(repo=repo)
    state = gitutil.read_state(repo.path)
    if state.error:
        plan.status = FAILED
        plan.reason = state.error
        return plan

    plan.from_branch = f"detached @ {state.head[:7]}" if state.detached else (state.branch or "?")

    target = gitutil.default_branch(repo.path, default_branches)
    if target is None:
        plan.status = SKIPPED
        plan.reason = f"no {' or '.join(default_branches)} branch to switch to"
        return plan
    plan.target = target

    if not state.detached and state.branch == target:
        plan.status = ALREADY
        return plan

    if not gitutil.branch_exists(repo.path, target):
        remote_ref = f"origin/{target}"
        if not gitutil.ref_exists(repo.path, remote_ref):  # pragma: no cover - default_branch found it somehow
            plan.status = SKIPPED
            plan.reason = f"no local branch {target} and no {remote_ref}"
            return plan
        plan.create_from = remote_ref

    if state.dirty:
        if not discard_changes:
            plan.status = SKIPPED
            plan.reason = ui.plural(state.tracked_changes, "uncommitted change")
            plan.hint = "--discard-changes to throw them away"
            return plan
        plan.discard = state.tracked_changes

    plan.status = WOULD_SWITCH
    return plan


def perform_switch(plan: SwitchPlan) -> SwitchPlan:
    """Carry out a planned switch, recording the outcome on the plan."""
    if plan.target is None:  # pragma: no cover - only planned switches get here
        return plan
    try:
        gitutil.checkout(
            plan.repo.path,
            plan.target,
            force=bool(plan.discard),
            track=plan.create_from,
        )
    except gitutil.GitError as exc:
        plan.status = FAILED
        plan.reason = exc.detail
        return plan

    plan.status = SWITCHED
    plan.behind = gitutil.read_state(plan.repo.path).behind
    return plan


def _result_cell(plan: SwitchPlan) -> str:
    if plan.status == SWITCHED:
        return "[localmw.ok]switched[/]"
    if plan.status == WOULD_SWITCH:
        return "[localmw.behind]would switch[/]"
    if plan.status == FAILED:
        return "[localmw.error]failed[/]"
    if plan.status == SKIPPED:
        return "[localmw.warn]skipped[/]"
    return "[localmw.muted]already there[/]"


def _detail_cell(plan: SwitchPlan) -> str:
    parts: list[str] = []
    if plan.reason:
        parts.append(escape(plan.reason))
    if plan.discard and plan.status in (WOULD_SWITCH, SWITCHED):
        verb = "discarding" if plan.status == WOULD_SWITCH else "discarded"
        parts.append(f"[localmw.warn]{verb} {ui.plural(plan.discard, 'change')}[/]")
    if plan.create_from and plan.status in (WOULD_SWITCH, SWITCHED):
        parts.append(f"[localmw.muted]new branch tracking {escape(plan.create_from)}[/]")
    if plan.behind:
        parts.append(f"[localmw.behind]{plan.behind} behind[/]")
    if plan.hint:
        parts.append(f"[localmw.muted]{escape(plan.hint)}[/]")
    return " · ".join(parts)


def log_plan(plan: SwitchPlan, width: int = 0) -> None:
    """The --verbose line for one repository: what will happen, then what did."""
    if plan.status == FAILED:
        level = "problem"
    elif plan.status in (SKIPPED, WOULD_SWITCH):
        level = "attention"
    else:
        level = "ok"
    if plan.status == ALREADY:
        move = f"[localmw.muted]{escape(plan.from_branch)}[/]"
    else:
        move = f"[localmw.muted]{escape(plan.from_branch)} → {escape(plan.target or '?')}[/]"
    ui.log_repo(plan.repo.label, [move, _result_cell(plan), _detail_cell(plan)], level=level, width=width)


def render_results(plans: Sequence[SwitchPlan], verbose: bool) -> None:
    rows = [plan for plan in plans if verbose or plan.interesting]
    if not rows:
        return
    table = ui.new_table(
        Column("Repository", no_wrap=True),
        Column("From", no_wrap=True, overflow="ellipsis"),
        Column("To", no_wrap=True, overflow="ellipsis"),
        Column("Result", no_wrap=True),
        "Detail",
    )
    for plan in sorted(rows, key=lambda p: p.sort_key):
        table.add_row(
            f"[localmw.name]{escape(plan.repo.label)}[/]",
            f"[localmw.warn]{escape(plan.from_branch)}[/]" if plan.interesting else escape(plan.from_branch),
            f"[localmw.muted]{escape(plan.target or '')}[/]",
            _result_cell(plan),
            _detail_cell(plan),
        )
    ui.console.print(table)
    ui.console.print()


def _summarise(plans: Sequence[SwitchPlan]) -> None:
    counts = {status: sum(1 for plan in plans if plan.status == status) for status in _STATUS_ORDER}
    parts = [ui.plural(len(plans), "repository", "repositories")]
    if counts[SWITCHED]:
        parts.append(f"[localmw.ok]{counts[SWITCHED]} switched[/]")
    if counts[WOULD_SWITCH]:
        parts.append(f"[localmw.behind]{counts[WOULD_SWITCH]} would switch[/]")
    if counts[ALREADY]:
        parts.append(f"[localmw.muted]{counts[ALREADY]} already there[/]")
    if counts[SKIPPED]:
        parts.append(f"[localmw.warn]{counts[SKIPPED]} skipped[/]")
    if counts[FAILED]:
        parts.append(f"[localmw.error]{counts[FAILED]} failed[/]")
    ui.console.print(ui.join_parts(parts))


@click.command("switch")
@selection_options
@click.option(
    "--discard-changes",
    is_flag=True,
    help="Throw away uncommitted changes to tracked files instead of skipping the repository.",
)
@click.option("-n", "--dry-run", is_flag=True, help="Report what would be switched, changing nothing.")
@click.option("-y", "--yes", is_flag=True, help="Do not ask before discarding uncommitted changes.")
@click.option("-j", "--jobs", type=int, default=None, help="Repositories to process concurrently.")
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Log each repository as it is processed instead of drawing a progress bar, and list every "
    "repository in the results, not just the ones that moved.",
)
@click.pass_obj
def switch_command(
    ctx: AppContext,
    want_core: bool,
    want_extensions: bool,
    want_skins: bool,
    want_vendor: bool,
    skip_vendor: bool,
    only: tuple[str, ...],
    exclude: tuple[str, ...],
    discard_changes: bool,
    dry_run: bool,
    yes: bool,
    jobs: int | None,
    verbose: bool,
) -> None:
    """Switch repositories that are off master/main back onto it.

    Anything already on its default branch is left alone. A repository with uncommitted changes
    to tracked files is reported and skipped, unless --discard-changes is given — which throws
    that work away, so you are asked to confirm first. Untracked files are never touched, and
    the branch you were on is not deleted.

    \b
    Examples:
      localmw switch                   # every repository that is off master/main
      localmw switch --extensions      # just extensions/*
      localmw switch -o Vector         # just that one
      localmw switch --dry-run         # show what would move
      localmw switch --discard-changes # move even where there is uncommitted work
    """
    kinds = resolve_kinds(want_core, want_extensions, want_skins, want_vendor, skip_vendor)
    verbose = ctx.be_verbose(verbose)
    discovery = ctx.discover(kinds, only, exclude)
    require_repos(discovery)
    ctx.announce_root(len(discovery))

    effective_jobs = ctx.jobs(jobs)
    label_width = max(len(repo.label) for repo in discovery.repos)

    def logger(item, plan: SwitchPlan) -> None:
        log_plan(plan, label_width)

    if verbose:
        ctx.announce_work("Reading", len(discovery), effective_jobs)

    plans = ui.run_parallel(
        discovery.repos,
        lambda repo: plan_switch(
            repo,
            default_branches=ctx.config.default_branches,
            discard_changes=discard_changes,
        ),
        jobs=effective_jobs,
        description="Checking branches",
        show_progress=not verbose,
        on_result=logger if verbose else None,
    )
    if verbose:
        ui.console.print()

    pending = [plan for plan in plans if plan.status == WOULD_SWITCH]
    discarding = [plan for plan in pending if plan.discard]

    if not pending:
        render_results(plans, verbose=verbose)
        if not any(plan.interesting for plan in plans):
            ui.console.print("[localmw.ok]Every repository is already on its default branch.[/]")
        _summarise(plans)
        if any(plan.status == FAILED for plan in plans):
            raise SystemExit(1)
        return

    if dry_run:
        render_results(plans, verbose=verbose)
        _summarise(plans)
        ui.muted("dry run — nothing was changed")
        return

    if discarding and not yes:
        # Show what is at stake before asking, since this is the one destructive path.
        render_results(plans, verbose=verbose)
        changes = sum(plan.discard for plan in discarding)
        question = (
            f"Discard {ui.plural(changes, 'uncommitted change')} in "
            f"{ui.plural(len(discarding), 'repository', 'repositories')} and switch?"
        )
        if not click.confirm(question, default=False):
            ui.muted("aborted; nothing was changed")
            return
        ui.console.print()

    if verbose:
        ctx.announce_work("Switching", len(pending), effective_jobs)

    ui.run_parallel(
        pending,
        perform_switch,
        jobs=effective_jobs,
        description="Switching",
        show_progress=not verbose,
        on_result=logger if verbose else None,
    )
    if verbose:
        ui.console.print()

    render_results(plans, verbose=verbose)
    _summarise(plans)

    behind = [plan for plan in pending if plan.status == SWITCHED and plan.behind]
    if behind:
        ui.muted(f"{ui.plural(len(behind), 'repository', 'repositories')} now behind — run 'localmw pull' to update")

    if any(plan.status == FAILED for plan in plans):
        raise SystemExit(1)
