"""``localmw cleanup`` — delete ``review/*`` branches whose change has landed in Gerrit."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import click
from rich.markup import escape
from rich.table import Column

from .. import gerrit, gitutil, ui
from ..context import AppContext, require_repos, resolve_kinds, selection_options
from ..install import Repo

DELETE = "delete"
KEEP = "keep"
SKIP = "skip"


@dataclass
class Candidate:
    """One local branch we are considering deleting."""

    repo: Repo
    branch: gitutil.BranchInfo
    change_ids: list[str] = field(default_factory=list)
    merged_locally: bool = False
    changes: dict[str, list[gerrit.Change]] = field(default_factory=dict)
    lookup_error: str | None = None
    decision: str = KEEP
    reason: str = ""
    force: bool = False
    deleted: bool = False
    delete_error: str | None = None

    @property
    def primary_change(self) -> gerrit.Change | None:
        for change_id in self.change_ids:
            matches = self.changes.get(change_id) or []
            if matches:
                return matches[0]
        return None

    @property
    def change_label(self) -> str:
        change = self.primary_change
        if change is not None and change.number:
            extra = f" (+{len(self.change_ids) - 1})" if len(self.change_ids) > 1 else ""
            return f"{change.number}{extra}"
        if self.change_ids:
            return self.change_ids[0][:9]
        return ""


@dataclass
class RepoScan:
    repo: Repo
    candidates: list[Candidate] = field(default_factory=list)
    default_branch: str | None = None
    base_ref: str | None = None
    project: str | None = None
    error: str | None = None


def scan_repo(repo: Repo, *, prefix: str, default_branches: Sequence[str]) -> RepoScan:
    """Find candidate branches in one repository (no network access)."""
    scan = RepoScan(repo=repo)
    try:
        branches = gitutil.list_branches(repo.path, prefix=prefix)
    except gitutil.GitError as exc:
        scan.error = exc.detail
        return scan

    default_branch = gitutil.default_branch(repo.path, default_branches)
    scan.default_branch = default_branch
    protected = {default_branch, *default_branches} - {None}

    base_ref: str | None = None
    if default_branch:
        for candidate_ref in (f"origin/{default_branch}", default_branch):
            if gitutil.ref_exists(repo.path, candidate_ref):
                base_ref = candidate_ref
                break
    scan.base_ref = base_ref

    for branch in branches:
        if branch.name in protected:
            continue
        candidate = Candidate(repo=repo, branch=branch)
        if branch.is_current:
            candidate.decision = SKIP
            candidate.reason = "checked out right now"
            scan.candidates.append(candidate)
            continue
        if base_ref:
            candidate.merged_locally = gitutil.is_ancestor(repo.path, branch.name, base_ref)
        candidate.change_ids = gitutil.change_ids(repo.path, branch.name, base_ref)
        scan.candidates.append(candidate)

    if scan.candidates:
        scan.project = gitutil.gerrit_project(repo.path)
    return scan


def _resolve_status(candidate: Candidate, default_branch: str | None) -> str:
    """Combined Gerrit status across every change on the branch."""
    statuses = []
    for change_id in candidate.change_ids:
        matches = candidate.changes.get(change_id) or []
        if not matches:
            return "unknown"
        on_default = [c for c in matches if default_branch and c.branch == default_branch]
        relevant = on_default or matches
        if any(c.is_merged for c in relevant):
            statuses.append(gerrit.STATUS_MERGED)
        elif all(c.is_abandoned for c in relevant):
            statuses.append(gerrit.STATUS_ABANDONED)
        else:
            statuses.append("OPEN")
    if not statuses:
        return "unknown"
    if all(status == gerrit.STATUS_MERGED for status in statuses):
        return gerrit.STATUS_MERGED
    if all(status in (gerrit.STATUS_MERGED, gerrit.STATUS_ABANDONED) for status in statuses):
        return gerrit.STATUS_ABANDONED
    return "OPEN"


def decide(candidate: Candidate, *, default_branch: str | None, include_abandoned: bool, use_gerrit: bool) -> None:
    """Set ``decision``/``reason`` on a candidate."""
    if candidate.decision == SKIP:
        return

    if candidate.merged_locally:
        candidate.decision = DELETE
        candidate.reason = "already merged locally"
        candidate.force = False
        return

    if not use_gerrit:
        candidate.decision = KEEP
        candidate.reason = "not merged locally (Gerrit lookup disabled)"
        return

    if not candidate.change_ids:
        candidate.decision = KEEP
        candidate.reason = "no Change-Id in its commits"
        return

    if candidate.lookup_error:
        candidate.decision = KEEP
        candidate.reason = f"Gerrit lookup failed: {candidate.lookup_error}"
        return

    status = _resolve_status(candidate, default_branch)
    if status == gerrit.STATUS_MERGED:
        candidate.decision = DELETE
        candidate.reason = "merged in Gerrit"
        candidate.force = True
    elif status == gerrit.STATUS_ABANDONED:
        if include_abandoned:
            candidate.decision = DELETE
            candidate.reason = "abandoned in Gerrit"
            candidate.force = True
        else:
            candidate.decision = KEEP
            candidate.reason = "abandoned in Gerrit (use --include-abandoned)"
    elif status == "unknown":
        candidate.decision = KEEP
        candidate.reason = "change not found in Gerrit"
    else:
        candidate.decision = KEEP
        candidate.reason = "still open in Gerrit"


def _lookup_changes(
    client: gerrit.GerritClient,
    scans: Sequence[RepoScan],
    verbose: bool = False,
) -> None:
    """Fill in Gerrit results for every candidate, one query per repository."""
    pending = [scan for scan in scans if any(c.change_ids for c in scan.candidates)]
    if not pending:
        return

    for scan in pending:
        change_ids = [cid for candidate in scan.candidates for cid in candidate.change_ids]
        if verbose:
            project = scan.project or "unknown project"
            ui.log(f"[localmw.muted]asking Gerrit about {ui.plural(len(change_ids), 'change')} in {escape(project)}[/]")
        try:
            found = client.changes_by_change_id(change_ids, project=scan.project)
        except gerrit.GerritError as exc:
            for candidate in scan.candidates:
                candidate.lookup_error = str(exc)
            continue
        for candidate in scan.candidates:
            candidate.changes = {cid: found.get(cid, []) for cid in candidate.change_ids}


def log_scan(scan: RepoScan, width: int = 0) -> None:
    """The --verbose line for one repository, printed as soon as its branches are scanned."""
    if scan.error:
        ui.log_repo(scan.repo.label, [escape(scan.error)], level="problem", width=width)
        return
    if not scan.candidates:
        ui.log_repo(scan.repo.label, ["[localmw.muted]no matching branches[/]"], width=width)
        return
    names = ", ".join(escape(candidate.branch.name) for candidate in scan.candidates[:3])
    if len(scan.candidates) > 3:
        names += f" (+{len(scan.candidates) - 3} more)"
    ui.log_repo(
        scan.repo.label,
        [ui.plural(len(scan.candidates), "branch", "branches"), f"[localmw.muted]{names}[/]"],
        level="attention",
        width=width,
    )


def _decision_cell(candidate: Candidate) -> str:
    if candidate.delete_error:
        return "[localmw.error]failed[/]"
    if candidate.deleted:
        return "[localmw.ok]deleted[/]"
    if candidate.decision == DELETE:
        return "[localmw.behind]delete[/]"
    if candidate.decision == SKIP:
        return "[localmw.muted]skip[/]"
    return "[localmw.warn]keep[/]"


def render_table(candidates: Sequence[Candidate]) -> None:
    table = ui.new_table(
        Column("Repository", no_wrap=True),
        Column("Branch", no_wrap=True, overflow="ellipsis"),
        Column("Last commit", no_wrap=True, overflow="ellipsis"),
        Column("Change", no_wrap=True),
        Column("Action", no_wrap=True),
        "Reason",
    )
    for candidate in candidates:
        change = candidate.primary_change
        change_cell = candidate.change_label
        if change is not None and change.number:
            change_cell = f"[link={change.url}]{change.number}[/link]"
            if len(candidate.change_ids) > 1:
                change_cell += f" [localmw.muted](+{len(candidate.change_ids) - 1})[/]"
        table.add_row(
            f"[localmw.name]{escape(candidate.repo.label)}[/]",
            escape(candidate.branch.name),
            f"[localmw.muted]{escape(candidate.branch.relative_date)}[/]",
            change_cell,
            _decision_cell(candidate),
            escape(candidate.delete_error or candidate.reason),
        )
    ui.console.print(table)
    ui.console.print()


@click.command("cleanup")
@selection_options
@click.option("--prefix", default=None, help="Branch name prefix to consider (default: review/).")
@click.option(
    "--include-abandoned",
    is_flag=True,
    help="Also delete branches whose change was abandoned in Gerrit.",
)
@click.option(
    "--no-gerrit",
    "no_gerrit",
    is_flag=True,
    help="Do not query Gerrit; only delete branches already merged locally.",
)
@click.option("-n", "--dry-run", is_flag=True, help="Report what would be deleted, changing nothing.")
@click.option("-y", "--yes", is_flag=True, help="Do not ask for confirmation before deleting.")
@click.option("-j", "--jobs", type=int, default=None, help="Repositories to scan concurrently.")
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Log each repository as it is scanned instead of drawing a progress bar, and also list "
    "the branches being kept.",
)
@click.pass_obj
def cleanup_command(
    ctx: AppContext,
    want_core: bool,
    want_extensions: bool,
    want_skins: bool,
    want_vendor: bool,
    skip_vendor: bool,
    only: tuple[str, ...],
    exclude: tuple[str, ...],
    prefix: str | None,
    include_abandoned: bool,
    no_gerrit: bool,
    dry_run: bool,
    yes: bool,
    jobs: int | None,
    verbose: bool,
) -> None:
    """Delete review/* branches whose changes have landed.

    Branches created by 'git review -d' are matched by prefix, their Change-Ids are read from
    the commits they carry, and Gerrit is asked whether those changes are merged. Anything
    still open, unrecognised, or currently checked out is left alone.
    """
    kinds = resolve_kinds(want_core, want_extensions, want_skins, want_vendor, skip_vendor)
    verbose = ctx.be_verbose(verbose)
    discovery = ctx.discover(kinds, only, exclude)
    require_repos(discovery)
    ctx.announce_root(len(discovery))

    use_gerrit = not no_gerrit
    effective_prefix = ctx.config.review_branch_prefix if prefix is None else prefix
    label_width = max(len(repo.label) for repo in discovery.repos)

    def logger(repo: Repo, scan: RepoScan) -> None:
        log_scan(scan, label_width)

    if verbose:
        ctx.announce_work("Scanning", len(discovery), ctx.jobs(jobs))

    scans = ui.run_parallel(
        discovery.repos,
        lambda repo: scan_repo(
            repo,
            prefix=effective_prefix,
            default_branches=ctx.config.default_branches,
        ),
        jobs=ctx.jobs(jobs),
        description="Scanning branches",
        show_progress=not verbose,
        on_result=logger if verbose else None,
    )

    for scan in scans:
        if scan.error:
            ui.warn(f"{scan.repo.label}: {scan.error}")

    scans_with_candidates = [scan for scan in scans if scan.candidates]
    if not scans_with_candidates:
        label = effective_prefix or "local"
        ui.console.print(f"[localmw.ok]No {escape(label)} branches to clean up.[/]")
        return

    if use_gerrit:
        client = gerrit.client_from_config(ctx.config)
        if not ctx.quiet and not client.authenticated:
            ui.muted("querying Gerrit anonymously (set gerrit.username/gerrit.http_password for private changes)")
        if verbose:
            _lookup_changes(client, scans_with_candidates, verbose=True)
        else:
            with ui.console.status("[localmw.muted]Asking Gerrit about changes...[/]", spinner="dots"):
                _lookup_changes(client, scans_with_candidates)
    if verbose:
        ui.console.print()

    candidates: list[Candidate] = []
    for scan in scans_with_candidates:
        for candidate in scan.candidates:
            decide(
                candidate,
                default_branch=scan.default_branch,
                include_abandoned=include_abandoned,
                use_gerrit=use_gerrit,
            )
            candidates.append(candidate)

    to_delete = [c for c in candidates if c.decision == DELETE]
    shown = candidates if verbose else (to_delete or candidates)
    render_table(shown)

    kept = len(candidates) - len(to_delete)
    if not to_delete:
        ui.console.print(f"[localmw.ok]Nothing to delete.[/] {ui.plural(kept, 'branch', 'branches')} kept.")
        return

    if dry_run:
        ui.console.print(
            ui.join_parts(
                [
                    f"[localmw.behind]{ui.plural(len(to_delete), 'branch', 'branches')} would be deleted[/]",
                    f"[localmw.muted]{kept} kept[/]",
                    "[localmw.muted]dry run — nothing was changed[/]",
                ]
            )
        )
        return

    if not yes and not click.confirm(f"Delete {ui.plural(len(to_delete), 'branch', 'branches')}?", default=False):
        ui.muted("aborted; nothing was deleted")
        return

    for candidate in to_delete:
        try:
            gitutil.delete_branch(candidate.repo.path, candidate.branch.name, force=False)
            candidate.deleted = True
        except gitutil.GitError as exc:
            if candidate.force:
                try:
                    gitutil.delete_branch(candidate.repo.path, candidate.branch.name, force=True)
                    candidate.deleted = True
                except gitutil.GitError as force_exc:
                    candidate.delete_error = force_exc.detail
            else:
                candidate.delete_error = exc.detail

    deleted = [c for c in to_delete if c.deleted]
    failed = [c for c in to_delete if not c.deleted]

    for candidate in deleted:
        ui.console.print(
            f"[localmw.ok]✓[/] {escape(candidate.repo.label)} "
            f"[localmw.muted]{escape(candidate.branch.name)} ({candidate.reason})[/]"
        )
    for candidate in failed:
        ui.error(f"{candidate.repo.label} {candidate.branch.name}: {candidate.delete_error}")

    ui.console.print()
    ui.console.print(
        ui.join_parts(
            [
                f"[localmw.ok]{ui.plural(len(deleted), 'branch', 'branches')} deleted[/]",
                f"[localmw.muted]{kept} kept[/]" if kept else "",
                f"[localmw.error]{len(failed)} failed[/]" if failed else "",
            ]
        )
    )

    if failed:
        raise SystemExit(1)
