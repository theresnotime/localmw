"""``localmw repo`` — a close-up on one repository, plus the odd sharp tool for tidying it."""

from __future__ import annotations

import json as jsonlib
from collections.abc import Sequence
from typing import Any

import click
from rich.markup import escape

from .. import gitutil, ui
from ..context import AppContext
from ..install import Repo, discover


def resolve_repo(ctx: AppContext, target: str) -> Repo:
    """Find the one repository ``target`` names, or fail with a helpful message.

    ``target`` is matched the way the ``-o/--only`` patterns are — against a repository's name,
    kind, relative path or label — but here exactly one repository must match. An explicit target
    ignores the configured ``exclude`` list, so a repository you have hidden from the bulk
    commands can still be inspected by name.
    """
    stripped = target.strip()
    if not stripped:
        raise click.BadParameter("give a repository, e.g. 'core' or 'extensions/GlobalBlocking'")

    matches = discover(ctx.root, only=[stripped]).repos
    if len(matches) == 1:
        return matches[0]

    if len(matches) > 1:
        # A glob (or a bare kind like 'extensions') can name several. Prefer an exact hit before
        # calling it ambiguous, so 'core' wins even next to an extension that globs the same way.
        lowered = stripped.lower()
        exact = [
            repo
            for repo in matches
            if lowered in {repo.label.lower(), repo.name.lower(), repo.rel_path.lower(), repo.kind.lower()}
        ]
        if len(exact) == 1:
            return exact[0]
        names = ", ".join(repo.label for repo in matches[:8])
        more = f" (+{len(matches) - 8} more)" if len(matches) > 8 else ""
        raise click.ClickException(f"'{stripped}' matches several repositories: {names}{more}. Be more specific.")

    raise click.ClickException(f"no repository matching '{stripped}' in {ctx.root}")


def _read(repo: Repo, *, do_fetch: bool, default_branches: Sequence[str]) -> tuple[gitutil.RepoState, str | None]:
    """Read a repository's state, fetching first when asked. Returns the state and default branch."""
    fetch_error: str | None = None
    if do_fetch:
        try:
            gitutil.fetch(repo.path)
        except gitutil.GitError as exc:
            fetch_error = exc.detail

    state = gitutil.read_state(repo.path)
    state.fetch_error = fetch_error
    default_branch = None if state.error else gitutil.default_branch(repo.path, default_branches)
    return state, default_branch


def _upstream_line(state: gitutil.RepoState) -> str:
    if not state.has_upstream:
        return "[localmw.warn]no upstream[/]"
    parts = []
    if state.behind:
        parts.append(f"[localmw.behind]{state.behind} behind[/]")
    if state.ahead:
        parts.append(f"[localmw.ahead]{state.ahead} ahead[/]")
    sync = " ".join(parts) if parts else "[localmw.muted]up to date[/]"
    return f"[localmw.muted]{escape(state.upstream or '')}[/] · {sync}"


def _branch_line(state: gitutil.RepoState, default_branch: str | None) -> str:
    if state.detached:
        return f"[localmw.warn]detached @ {escape(state.head)}[/]"
    branch = state.branch or "?"
    on_default = default_branch is not None and branch == default_branch
    shown = escape(branch) if on_default else f"[localmw.warn]{escape(branch)}[/]"
    if default_branch and on_default:
        shown += " [localmw.muted](default)[/]"
    elif default_branch:
        shown += f" [localmw.muted](default is {escape(default_branch)})[/]"
    return shown


def _worktree_line(state: gitutil.RepoState) -> str:
    summary = state.worktree_summary()
    if summary == "clean":
        return "[localmw.ok]clean[/]"
    bits = [f"[localmw.muted]{escape(summary)}[/]"]
    counts = []
    if state.tracked_changes:
        counts.append(ui.plural(state.tracked_changes, "tracked change"))
    if state.untracked:
        counts.append(ui.plural(state.untracked, "untracked file"))
    if counts:
        colour = "localmw.warn" if state.dirty else "localmw.muted"
        bits.append(f"[{colour}]{', '.join(counts)}[/]")
    return " · ".join(bits)


def render_detail(repo: Repo, state: gitutil.RepoState, default_branch: str | None, prefix: str) -> None:
    """Print the aligned key/value view of one repository."""
    ui.console.print(f"[localmw.name]{escape(repo.label)}[/] [localmw.muted]· {escape(repo.kind)}[/]")

    rows: list[tuple[str, str]] = [("Path", f"[localmw.muted]{escape(str(repo.path))}[/]")]

    project = gitutil.gerrit_project(repo.path)
    if project:
        rows.append(("Gerrit", f"[localmw.muted]{escape(project)}[/]"))
    url = gitutil.remote_url(repo.path)
    if url:
        rows.append(("Origin", f"[localmw.muted]{escape(url)}[/]"))

    if state.error:
        rows.append(("State", f"[localmw.error]{escape(state.error)}[/]"))
    else:
        rows.append(("Branch", _branch_line(state, default_branch)))
        rows.append(("Upstream", _upstream_line(state)))
        rows.append(("Working tree", _worktree_line(state)))
        commit = state.last_commit
        if commit.sha:
            who = f"{commit.author}, {commit.relative_date}" if commit.author else commit.relative_date
            rows.append(
                (
                    "Last commit",
                    f"[localmw.muted]{escape(commit.sha)}[/] {escape(commit.subject)} "
                    f"[localmw.muted]({escape(who)})[/]",
                )
            )
        review = gitutil.list_branches(repo.path, prefix=prefix)
        if review:
            names = ", ".join(escape(branch.name) for branch in review[:5])
            more = f" [localmw.muted](+{len(review) - 5} more)[/]" if len(review) > 5 else ""
            count = ui.plural(len(review), "branch", "branches")
            rows.append((f"{escape(prefix)} branches", f"{count} · {names}{more}"))

    if state.fetch_error:
        rows.append(("Fetch", f"[localmw.warn]failed: {escape(state.fetch_error)}[/]"))

    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        ui.console.print(f"  [localmw.muted]{label:<{width}}[/]  {value}")


def _detail_dict(repo: Repo, state: gitutil.RepoState, default_branch: str | None, prefix: str) -> dict[str, Any]:
    data: dict[str, Any] = {
        "kind": repo.kind,
        "name": repo.name,
        "label": repo.label,
        "path": str(repo.path),
        "gerrit_project": gitutil.gerrit_project(repo.path),
        "origin": gitutil.remote_url(repo.path),
        "default_branch": default_branch,
        "branch": state.branch,
        "detached": state.detached,
        "head": state.head,
        "upstream": state.upstream,
        "ahead": state.ahead,
        "behind": state.behind,
        "staged": state.staged,
        "unstaged": state.unstaged,
        "untracked": state.untracked,
        "conflicts": state.conflicts,
        "dirty": state.dirty,
        "last_commit": {
            "sha": state.last_commit.sha,
            "subject": state.last_commit.subject,
            "relative_date": state.last_commit.relative_date,
            "author": state.last_commit.author,
        },
        "error": state.error,
        "fetch_error": state.fetch_error,
    }
    if not state.error:
        data["review_branches"] = [branch.name for branch in gitutil.list_branches(repo.path, prefix=prefix)]
    return data


def _do_reset(
    repo: Repo,
    state: gitutil.RepoState,
    default_branch: str | None,
    *,
    dry_run: bool,
    yes: bool,
) -> bool:
    """Reset the repository hard to ``origin/<default>``. Returns True on failure."""
    if state.error:
        ui.error(f"cannot reset: {state.error}")
        return True
    if default_branch is None:
        ui.error("cannot reset: no default branch to reset to")
        return True

    ref = f"origin/{default_branch}"
    if not gitutil.ref_exists(repo.path, ref):
        ui.error(f"cannot reset: {ref} does not exist (has it been fetched?)")
        return True

    stakes = []
    if state.ahead:
        stakes.append(ui.plural(state.ahead, "local commit"))
    if state.tracked_changes:
        stakes.append(ui.plural(state.tracked_changes, "uncommitted change"))
    stakes_note = f", discarding {ui.join_and(stakes)}" if stakes else ""

    if dry_run:
        ui.muted(f"would reset {repo.label} to {ref}{stakes_note} — dry run, nothing changed")
        return False

    question = f"Reset {repo.label} to {ref}{stakes_note}?"
    if not yes and not click.confirm(question, default=False):
        ui.muted("aborted; nothing was reset")
        return False

    try:
        gitutil.reset_hard(repo.path, ref)
    except gitutil.GitError as exc:
        ui.error(f"reset failed: {exc.detail}")
        return True
    ui.console.print(f"[localmw.ok]✓[/] reset to {escape(ref)}")
    return False


def _do_tidy(repo: Repo, state: gitutil.RepoState, *, dry_run: bool, yes: bool) -> bool:
    """Remove untracked files with ``git clean -df``. Returns True on failure."""
    if state.error:
        ui.error(f"cannot tidy: {state.error}")
        return True

    try:
        pending = gitutil.clean(repo.path, dry_run=True)
    except gitutil.GitError as exc:
        ui.error(f"could not list untracked files: {exc.detail}")
        return True

    if not pending:
        ui.muted(f"{repo.label} has no untracked files to remove")
        return False

    for entry in pending:
        ui.console.print(f"  [localmw.warn]remove[/] [localmw.muted]{escape(entry)}[/]")

    count = ui.plural(len(pending), "untracked path")
    if dry_run:
        ui.muted(f"would remove {count} — dry run, nothing changed")
        return False

    if not yes and not click.confirm(f"Remove {count}?", default=False):
        ui.muted("aborted; nothing was removed")
        return False

    try:
        removed = gitutil.clean(repo.path, dry_run=False)
    except gitutil.GitError as exc:
        ui.error(f"clean failed: {exc.detail}")
        return True
    ui.console.print(f"[localmw.ok]✓[/] removed {ui.plural(len(removed), 'untracked path')}")
    return False


@click.command("repo")
@click.argument("target")
@click.option("--fetch/--no-fetch", "do_fetch", default=True, help="Fetch from origin first (default: fetch).")
@click.option(
    "--tidy-untracked",
    "tidy_untracked",
    is_flag=True,
    help="Remove untracked files and directories with 'git clean -df'.",
)
@click.option(
    "--reset",
    is_flag=True,
    help="Reset the current branch hard to origin/master (or origin/main), discarding local work.",
)
@click.option("-n", "--dry-run", is_flag=True, help="Show what an action would do, changing nothing.")
@click.option("-y", "--yes", is_flag=True, help="Do not ask before an action changes anything.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON instead of the detail view.")
@click.pass_obj
def repo_command(
    ctx: AppContext,
    target: str,
    do_fetch: bool,
    tidy_untracked: bool,
    reset: bool,
    dry_run: bool,
    yes: bool,
    as_json: bool,
) -> None:
    """Show detailed information about one repository, and optionally tidy it.

    TARGET names the repository the way -o/--only does: 'core', 'GlobalBlocking' or the full
    'extensions/GlobalBlocking'. Exactly one repository must match. Naming a repository explicitly
    ignores the configured exclude list, so you can still inspect one you have otherwise hidden.

    \b
    Examples:
      localmw repo core                                  # a close-up on core
      localmw repo extensions/GlobalBlocking --json      # the same, as JSON
      localmw repo GlobalBlocking --tidy-untracked       # git clean -df, after confirming
      localmw repo GlobalBlocking --reset                # git reset --hard origin/master
    """
    if as_json and (tidy_untracked or reset):
        raise click.UsageError("--json cannot be combined with --reset or --tidy-untracked")

    repo = resolve_repo(ctx, target)
    state, default_branch = _read(repo, do_fetch=do_fetch, default_branches=ctx.config.default_branches)

    if as_json:
        click.echo(jsonlib.dumps(_detail_dict(repo, state, default_branch, ctx.config.review_branch_prefix), indent=2))
        return

    if not ctx.quiet:
        ctx.announce_root()
    render_detail(repo, state, default_branch, ctx.config.review_branch_prefix)
    if state.fetch_error:
        ui.warn(f"fetch failed, so 'behind' counts may be stale: {state.fetch_error}")

    failed = False
    if reset or tidy_untracked:
        ui.console.print()
    if reset:
        failed |= _do_reset(repo, state, default_branch, dry_run=dry_run, yes=yes)
        # Re-read so a following tidy, and nothing else, sees the post-reset tree.
        if tidy_untracked and not dry_run:
            state = gitutil.read_state(repo.path)
    if tidy_untracked:
        failed |= _do_tidy(repo, state, dry_run=dry_run, yes=yes)

    if failed:
        raise SystemExit(1)
