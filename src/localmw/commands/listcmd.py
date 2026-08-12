"""``localmw list`` — the same report as ``status``, but never touching the network."""

from __future__ import annotations

import click

from ..context import AppContext, resolve_kinds, selection_options
from .status import run_report


@click.command("list")
@selection_options
@click.option("--attention", "attention_only", is_flag=True, help="Only list repositories that need attention.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON instead of a table.")
@click.option("-j", "--jobs", type=int, default=None, help="Repositories to process concurrently.")
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Log each repository as it is read instead of drawing a progress bar.",
)
@click.pass_obj
def list_command(
    ctx: AppContext,
    want_core: bool,
    want_extensions: bool,
    want_skins: bool,
    want_vendor: bool,
    skip_vendor: bool,
    only: tuple[str, ...],
    exclude: tuple[str, ...],
    attention_only: bool,
    as_json: bool,
    jobs: int | None,
    verbose: bool,
) -> None:
    """List the current state of core, extensions and skins, without fetching or pulling.

    Everything is read from your local checkouts, so this is quick and works offline. The
    'behind' counts are as of your last fetch — run 'localmw status' if you want them refreshed.

    \b
    Examples:
      localmw list                     # everything
      localmw list --extensions        # just extensions/*
      localmw list --skins             # just skins/*
      localmw list --attention         # only what is off master/main, dirty, or behind
    """
    run_report(
        ctx,
        kinds=resolve_kinds(want_core, want_extensions, want_skins, want_vendor, skip_vendor),
        only=only,
        exclude=exclude,
        do_fetch=False,
        prune=False,
        jobs=jobs,
        attention_only=attention_only,
        as_json=as_json,
        verbose=verbose,
        stale_note="nothing was fetched, so 'behind' counts are as of your last fetch",
    )
