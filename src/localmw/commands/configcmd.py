"""``localmw config`` — inspect and edit the config file."""

from __future__ import annotations

import json as jsonlib

import click
from rich.markup import escape

from .. import gerrit, ui
from ..config import FIELDS, REDACTED, SCHEMA, Config, ConfigError
from ..context import AppContext


@click.group("config")
def config_group() -> None:
    """Inspect and edit the localmw config file."""


@config_group.command("path")
@click.pass_obj
def path_command(ctx: AppContext) -> None:
    """Print the location of the config file."""
    path = ctx.config.path
    click.echo(str(path))
    if not path.exists():
        ui.muted("(does not exist yet — create it with 'localmw config init')")


@config_group.command("keys")
def keys_command() -> None:
    """List every setting and what it does."""
    table = ui.new_table("Key", "Default", "Description")
    for field in SCHEMA:
        default = field.default
        rendered = "" if default in (None, [], "") else jsonlib.dumps(default)
        table.add_row(f"[localmw.name]{field.key}[/]", f"[localmw.muted]{escape(rendered)}[/]", escape(field.help))
    ui.console.print(table)


@config_group.command("show")
@click.option("--reveal", is_flag=True, help="Show secrets in full instead of redacting them.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of a table.")
@click.pass_obj
def show_command(ctx: AppContext, reveal: bool, as_json: bool) -> None:
    """Show the effective configuration and where each value comes from."""
    config = ctx.config
    values = config.as_dict(redact=not reveal)

    if as_json:
        click.echo(jsonlib.dumps({"path": str(config.path), "values": values}, indent=2))
        return

    ui.muted(f"{config.path}{'' if config.path.exists() else ' (not created yet)'}")
    table = ui.new_table("Key", "Value", "Source")
    for field in SCHEMA:
        value = values[field.key]
        rendered = "" if value is None else (value if isinstance(value, str) else jsonlib.dumps(value))
        style = "localmw.muted" if config.source_of(field.key) == "default" else "none"
        table.add_row(
            f"[localmw.name]{field.key}[/]",
            f"[{style}]{escape(str(rendered))}[/]",
            f"[localmw.muted]{config.source_of(field.key)}[/]",
        )
    ui.console.print(table)
    if not reveal and any(values[f.key] == REDACTED for f in SCHEMA):
        ui.muted("secrets redacted; pass --reveal to show them")


@config_group.command("get")
@click.argument("key")
@click.pass_obj
def get_command(ctx: AppContext, key: str) -> None:
    """Print the effective value of KEY."""
    try:
        value = ctx.config.get(key)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from None
    if value is None:
        return
    click.echo(value if isinstance(value, str) else jsonlib.dumps(value))


@config_group.command("set")
@click.argument("key")
@click.argument("value", required=False)
@click.pass_obj
def set_command(ctx: AppContext, key: str, value: str | None) -> None:
    """Set KEY to VALUE and save the config file.

    Omit VALUE to be prompted for it, which keeps secrets out of your shell history.
    Comma-separated input is accepted for list settings.
    """
    field = FIELDS.get(key)
    if field is None:
        raise click.ClickException(f"unknown config key {key!r} (see 'localmw config keys')")

    if value is None:
        value = click.prompt(f"Value for {key}", hide_input=field.secret, default="", show_default=False)

    config = ctx.config
    try:
        stored = config.set_raw(key, value)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from None

    path = config.save()
    shown = REDACTED if field.secret and stored else stored
    ui.console.print(f"[localmw.ok]set[/] {key} = {escape(str(shown))}")
    ui.muted(f"saved to {path}")


@config_group.command("unset")
@click.argument("key")
@click.pass_obj
def unset_command(ctx: AppContext, key: str) -> None:
    """Remove KEY from the config file, reverting it to its default."""
    config = ctx.config
    try:
        removed = config.unset(key)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from None
    if not removed:
        ui.muted(f"{key} was not set in {config.path}")
        return
    config.save()
    ui.console.print(f"[localmw.ok]unset[/] {key}")


@config_group.command("init")
@click.option("--force", is_flag=True, help="Overwrite an existing config file.")
@click.pass_obj
def init_command(ctx: AppContext, force: bool) -> None:
    """Create a config file pre-filled with the defaults."""
    path = ctx.config.path
    if path.exists() and not force:
        raise click.ClickException(f"{path} already exists (pass --force to overwrite)")

    fresh = Config(data={}, path=path)
    fresh.set_raw("gerrit.url", ctx.config.gerrit_url)
    fresh.set_raw("pull.strategy", "ff-only")
    fresh.set_raw("jobs", str(ctx.config.jobs))
    fresh.set_raw("review_branch_prefix", "review/")
    fresh.save()

    ui.console.print(f"[localmw.ok]created[/] {path}")
    ui.muted("next: localmw config set mediawiki_dir /path/to/mediawiki")
    ui.muted("      localmw config set gerrit.username <you>")
    ui.muted("      localmw config set gerrit.http_password   # prompts, stays out of history")


@config_group.command("check")
@click.pass_obj
def check_command(ctx: AppContext) -> None:
    """Validate the config and check that Gerrit is reachable."""
    config = ctx.config
    problems = config.validate()
    for problem in problems:
        ui.error(problem)

    if not problems:
        ui.console.print(f"[localmw.ok]✓[/] config is valid [localmw.muted]({config.path})[/]")

    mw_dir = config.mediawiki_dir
    if mw_dir is not None:
        from ..install import looks_like_install

        if looks_like_install(mw_dir):
            ui.console.print(f"[localmw.ok]✓[/] mediawiki_dir {escape(str(mw_dir))}")
        else:
            ui.error(f"mediawiki_dir {mw_dir} is not a MediaWiki install")
            problems.append("mediawiki_dir")

    client = gerrit.client_from_config(config)
    mode = "authenticated" if client.authenticated else "anonymous"
    try:
        version = client.version()
    except gerrit.GerritError as exc:
        ui.error(f"Gerrit ({mode}): {exc}")
        problems.append("gerrit")
    else:
        ui.console.print(
            f"[localmw.ok]✓[/] Gerrit {escape(config.gerrit_url)} [localmw.muted]({mode}, server {escape(version)})[/]"
        )

    if problems:
        raise SystemExit(1)
