"""Command-line entry point."""

from __future__ import annotations

from pathlib import Path

import click

from . import __version__, ui
from .commands.cleanup import cleanup_command
from .commands.configcmd import config_group
from .commands.listcmd import list_command
from .commands.pull import pull_command
from .commands.repo import repo_command
from .commands.status import status_command
from .commands.switch import switch_command
from .config import Config, ConfigError
from .context import AppContext

CONTEXT_SETTINGS = {
    "help_option_names": ["-h", "--help"],
    "max_content_width": 100,
}


@click.group(context_settings=CONTEXT_SETTINGS)
@click.version_option(__version__, "-V", "--version", prog_name="localmw")
@click.option(
    "--mw",
    "mw_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    metavar="PATH",
    help="MediaWiki install to operate on (default: the current directory, then config).",
)
@click.option(
    "--config",
    "config_file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    metavar="PATH",
    help="Use a specific config file instead of the default location.",
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Log each repository as it is processed instead of drawing a progress bar. "
    "Accepted before or after the subcommand.",
)
@click.option("-q", "--quiet", is_flag=True, help="Suppress progress output.")
@click.option("--no-color", is_flag=True, help="Disable coloured output.")
@click.pass_context
def cli(
    ctx: click.Context,
    mw_dir: Path | None,
    config_file: Path | None,
    verbose: bool,
    quiet: bool,
    no_color: bool,
) -> None:
    """Manage local MediaWiki development installs.

    Run localmw from inside a MediaWiki checkout, or point it at one with --mw.

    \b
    Common tasks:
      localmw status            # what state is everything in?
      localmw list              # the same, offline: no fetching
      localmw pull              # fast-forward core, extensions and skins
      localmw switch            # put repositories back on master/main
      localmw cleanup           # bin review/* branches that have merged
      localmw repo core         # a close-up on one repository
      localmw config show       # see the current settings
    """
    ui.set_color(no_color)
    ui.set_quiet(quiet)

    # A malformed environment override (e.g. LOCALMW_JOBS=lots) can't be parsed into a value, so
    # loading raises rather than warning. Report it cleanly instead of letting it become a traceback.
    try:
        config = Config.load(config_file)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from None
    for warning in config.warnings:
        ui.warn(warning)

    ctx.obj = AppContext(config=config, mw_override=mw_dir, quiet=quiet, verbose=verbose)


cli.add_command(status_command)
cli.add_command(list_command)
cli.add_command(pull_command)
cli.add_command(switch_command)
cli.add_command(cleanup_command)
cli.add_command(repo_command)
cli.add_command(config_group)


def main() -> None:
    """Console-script entry point."""
    cli()


if __name__ == "__main__":  # pragma: no cover
    main()
