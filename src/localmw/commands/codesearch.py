"""``localmw codesearch`` — search Wikimedia Codesearch from the terminal."""

from __future__ import annotations

import json as jsonlib
import re
from dataclasses import asdict
from typing import Any

import click
from rich.markup import escape
from rich.text import Text

from .. import ui
from ..codesearch import BACKENDS, DEFAULT_BACKEND, CodesearchClient, CodesearchError, FileMatch, RepoResult
from ..context import AppContext
from ..install import discover


def _highlighter(query: str, ignore_case: bool) -> re.Pattern[str] | None:
    # Hound uses Go's RE2 syntax; most of it is valid Python, and anything that is not just goes unhighlighted.
    try:
        return re.compile(query, re.IGNORECASE if ignore_case else 0)
    except re.error:
        return None


def _installed_paths(ctx: AppContext) -> set[str]:
    return {repo.rel_path for repo in discover(ctx.root).repos}


def _link(text: str, url: str | None, style: str = "") -> str:
    body = f"[{style}]{escape(text)}[/]" if style else escape(text)
    return f"[link={url}]{body}[/link]" if url else body


def _file_lines(file: FileMatch) -> list[tuple[int, str, bool]]:
    """Merge each match with its context, so overlapping context is printed once."""
    lines: dict[int, tuple[str, bool]] = {}
    for match in file.lines:
        for offset, text in enumerate(match.before):
            lines.setdefault(match.number - len(match.before) + offset, (text, False))
        for offset, text in enumerate(match.after, start=1):
            lines.setdefault(match.number + offset, (text, False))
        lines[match.number] = (match.line, True)
    return [(number, text, is_match) for number, (text, is_match) in sorted(lines.items())]


def _render_repo(result: RepoResult, pattern: re.Pattern[str] | None, *, names_only: bool, local: str | None) -> None:
    header = f"[localmw.name]{escape(result.repo)}[/]"
    count = ui.plural(result.files_with_match, "file")
    header += f" [localmw.muted]· {count}[/]"
    if local:
        header += f" [localmw.muted]· {escape(local)}[/]"
    ui.console.print(header, soft_wrap=True)

    for file in result.files:
        ui.console.print(f"  {_link(file.path, result.file_url(file.path), 'localmw.behind')}")
        if names_only:
            continue
        lines = _file_lines(file)
        width = len(str(lines[-1][0])) if lines else 0
        previous: int | None = None
        for number, text, is_match in lines:
            if previous is not None and number > previous + 1:
                ui.console.print(f"    [localmw.muted]{'…':>{width}}[/]")
            previous = number
            gutter = _link(f"{number:>{width}}", result.file_url(file.path, number), "localmw.muted")
            content = Text(text.expandtabs(4).rstrip(), style="" if is_match else "dim")
            if is_match and pattern is not None:
                content.highlight_regex(pattern, "bold yellow")
            ui.console.print(Text.from_markup(f"    {gutter}  ").append_text(content), soft_wrap=True)

    hidden = result.files_with_match - len(result.files)
    if hidden > 0:
        ui.console.print(f"  [localmw.muted]+{ui.plural(hidden, 'more file')} (raise --max-files to see them)[/]")


def _repo_dict(result: RepoResult, local: str | None) -> dict[str, Any]:
    return {
        "repo": result.repo,
        "revision": result.revision,
        "files_with_match": result.files_with_match,
        "local_path": local,
        "files": [
            {
                "path": file.path,
                "url": result.file_url(file.path),
                "matches": [asdict(line) for line in file.lines],
            }
            for file in result.files
        ],
    }


@click.command("codesearch")
@click.argument("query", nargs=-1, required=True)
@click.option(
    "-b",
    "--backend",
    type=click.Choice(BACKENDS, case_sensitive=False),
    default=DEFAULT_BACKEND,
    show_default=True,
    help="Which Codesearch index to search.",
)
@click.option("-i", "--ignore-case", is_flag=True, help="Match case-insensitively.")
@click.option("-F", "--fixed-strings", is_flag=True, help="Treat QUERY as a literal string, not a regular expression.")
@click.option("-f", "--files", "files", default="", metavar="REGEX", help="Only search files whose path matches REGEX.")
@click.option("--exclude-files", default="", metavar="REGEX", help="Skip files whose path matches REGEX.")
@click.option(
    "-r",
    "--repo",
    "repos",
    multiple=True,
    metavar="NAME",
    help="Only search this Codesearch repository, e.g. 'Extension:Echo' or 'MediaWiki core' (repeatable).",
)
@click.option(
    "--installed",
    is_flag=True,
    help="Only show repositories checked out in the local MediaWiki install.",
)
@click.option(
    "-C",
    "--context",
    default=0,
    show_default=True,
    metavar="N",
    type=click.IntRange(0, 10),
    help="Lines of context around each match.",
)
@click.option(
    "-m",
    "--max-files",
    default=20,
    show_default=True,
    metavar="N",
    type=click.IntRange(1),
    help="Most files to show per repository.",
)
@click.option("-l", "--files-with-matches", "names_only", is_flag=True, help="Only list the matching files.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
@click.pass_obj
def codesearch_command(
    ctx: AppContext,
    query: tuple[str, ...],
    backend: str,
    ignore_case: bool,
    fixed_strings: bool,
    files: str,
    exclude_files: str,
    repos: tuple[str, ...],
    installed: bool,
    context: int,
    max_files: int,
    names_only: bool,
    as_json: bool,
) -> None:
    """Search Wikimedia Codesearch (codesearch.wmcloud.org).

    QUERY is a regular expression (RE2 syntax), unless -F is given. Several words are joined with
    spaces, so quoting is only needed to protect characters from the shell.

    \b
    Examples:
      localmw codesearch getUserBlock                       # everything, everywhere
      localmw codesearch -F 'wfGetDB('                      # a literal string
      localmw codesearch -b core -f '\\.php$' HookRunner     # core only, PHP files only
      localmw codesearch -r Extension:Echo -C 2 onUserSave  # one repository, with context
      localmw codesearch --installed GlobalBlocking         # only what you have checked out

    Exits 1 when nothing matches.
    """
    text = " ".join(query).strip()
    if not text:
        raise click.BadParameter("give something to search for", param_hint="QUERY")
    pattern_text = re.escape(text) if fixed_strings else text
    backend = backend.lower()

    client = CodesearchClient(ctx.config.codesearch_url)
    params = client.params(
        pattern_text,
        repos=",".join(repos) or "*",
        files=files,
        exclude_files=exclude_files,
        ignore_case=ignore_case,
        context=0 if names_only else context,
        max_files=max_files,
    )

    local_paths = _installed_paths(ctx) if installed else None

    try:
        results = client.search(backend, params)
    except CodesearchError as exc:
        raise click.ClickException(f"codesearch: {exc}") from None

    def local_of(result: RepoResult) -> str | None:
        rel = result.local_rel_path
        return rel if local_paths is not None and rel in local_paths else None

    if local_paths is not None:
        results = [result for result in results if local_of(result)]

    if as_json:
        payload = {
            "query": pattern_text,
            "backend": backend,
            "url": client.ui_url(backend, params),
            "results": [_repo_dict(result, local_of(result)) for result in results],
        }
        click.echo(jsonlib.dumps(payload, indent=2))
        if not results:
            raise SystemExit(1)
        return

    pattern = _highlighter(pattern_text, ignore_case)
    for index, result in enumerate(results):
        if index:
            ui.console.print()
        local = local_of(result)
        _render_repo(result, pattern, names_only=names_only, local=None if local is None else str(ctx.root / local))

    if not ctx.quiet:
        if results:
            ui.console.print()
            matches = sum(result.match_count for result in results)
            shown_files = sum(len(result.files) for result in results)
            summary = [
                "" if names_only else ui.plural(matches, "match", "matches"),
                ui.plural(shown_files, "file"),
                ui.plural(len(results), "repository", "repositories"),
            ]
            ui.muted(escape(ui.join_parts(summary)))
        else:
            ui.muted("no matches" + (" in the installed repositories" if installed else ""))
        url = client.ui_url(backend, params)
        ui.console.print(f"[localmw.muted][link={url}]{escape(url)}[/link][/]", soft_wrap=True)

    if not results:
        raise SystemExit(1)
