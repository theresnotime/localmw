"""Console output helpers."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TypeVar

from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Column, Table
from rich.theme import Theme

T = TypeVar("T")
R = TypeVar("R")

THEME = Theme(
    {
        "localmw.ok": "green",
        "localmw.warn": "yellow",
        "localmw.error": "bold red",
        "localmw.behind": "cyan",
        "localmw.ahead": "magenta",
        "localmw.muted": "dim",
        "localmw.name": "bold",
    }
)


def make_console(*, stderr: bool = False, no_color: bool = False, quiet: bool = False) -> Console:
    return Console(
        theme=THEME,
        stderr=stderr,
        no_color=no_color or bool(os.environ.get("NO_COLOR")),
        quiet=quiet,
        highlight=False,
        soft_wrap=False,
    )


console = make_console()
err_console = make_console(stderr=True)

#: Set by --quiet; suppresses progress bars but never results.
QUIET = False


def set_color(no_color: bool) -> None:
    """Rebuild the module-level consoles, honouring --no-color."""
    global console, err_console
    console = make_console(no_color=no_color)
    err_console = make_console(stderr=True, no_color=no_color)


def set_quiet(quiet: bool) -> None:
    global QUIET
    QUIET = quiet


def warn(message: str) -> None:
    err_console.print(f"[localmw.warn]warning:[/] {message}")


def error(message: str) -> None:
    err_console.print(f"[localmw.error]error:[/] {message}")


def muted(message: str) -> None:
    console.print(f"[localmw.muted]{message}[/]")


def log(message: str) -> None:
    """A --verbose progress line: one line per repository, on stderr so it never mixes with
    results. Over-long lines are clipped rather than wrapped, to keep the log scannable — the
    full detail is in the table that follows."""
    err_console.print(message, no_wrap=True, overflow="ellipsis", crop=True)


#: How a --verbose line is flagged: fine, worth a look, or broken.
GLYPHS = {
    "ok": "[localmw.ok]✓[/]",
    "attention": "[localmw.warn]![/]",
    "problem": "[localmw.error]✗[/]",
}


def log_repo(label: str, parts: Iterable[str], *, level: str = "ok", width: int = 0) -> None:
    """One --verbose line about one repository, with the label padded for alignment."""
    padded = f"{label:<{width}}" if width else label
    details = join_parts(parts)
    line = f"{GLYPHS[level]} [localmw.name]{escape(padded)}[/]"
    log(f"{line}  {details}" if details else line)


def new_table(*columns: str | Column, title: str | None = None) -> Table:
    """A borderless table. Plain strings become columns that may wrap; pass a
    :class:`rich.table.Column` for anything that should not."""
    prepared = [column if isinstance(column, Column) else Column(column, overflow="fold") for column in columns]
    return Table(*prepared, title=title, box=None, pad_edge=False, header_style="localmw.muted")


def run_parallel(
    items: Sequence[T],
    worker: Callable[[T], R],
    *,
    jobs: int = 2,
    description: str = "Working",
    show_progress: bool = True,
    on_result: Callable[[T, R], None] | None = None,
) -> list[R]:
    """Map ``worker`` over ``items`` concurrently, returning results in input order.

    ``on_result`` runs on the calling thread as each result lands (so in completion order,
    not input order), which makes it safe to print from.
    """
    total = len(items)
    results: list[R | None] = [None] * total
    if total == 0:
        return []

    workers = max(1, min(jobs, total))
    show_progress = show_progress and not QUIET and total > 1 and console.is_terminal

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[localmw.muted]{task.description}[/]"),
        BarColumn(bar_width=24),
        MofNCompleteColumn(),
        console=console,
        transient=True,
        disable=not show_progress,
    )

    with progress:
        task = progress.add_task(description, total=total)
        if workers == 1:
            for index, item in enumerate(items):
                results[index] = worker(item)
                progress.advance(task)
                if on_result is not None:
                    on_result(item, results[index])  # type: ignore[arg-type]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(worker, item): index for index, item in enumerate(items)}
                for future in as_completed(futures):
                    index = futures[future]
                    results[index] = future.result()
                    progress.advance(task)
                    if on_result is not None:
                        on_result(items[index], results[index])  # type: ignore[arg-type]

    return list(results)  # type: ignore[misc]


def plural(count: int, singular: str, plural_form: str | None = None) -> str:
    """``plural(2, "branch", "branches")`` -> ``"2 branches"``; defaults to adding an *s*."""
    if count == 1:
        return f"{count} {singular}"
    return f"{count} {plural_form or singular + 's'}"


def join_parts(parts: Iterable[str]) -> str:
    return " · ".join(part for part in parts if part)
