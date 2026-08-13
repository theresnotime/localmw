from __future__ import annotations

import json
from pathlib import Path

import fixtures
import pytest
from click.testing import CliRunner

from localmw import gitutil
from localmw.cli import cli


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def invoke(runner: CliRunner, config_dir: Path):
    def _invoke(*args: str, **kwargs):
        return runner.invoke(cli, list(args), catch_exceptions=False, **kwargs)

    return _invoke


def test_repo_shows_a_detail_view(invoke, install: Path):
    result = invoke("--mw", str(install), "repo", "extensions/Echo", "--no-fetch")
    assert result.exit_code == 0
    assert "extensions/Echo" in result.output
    assert "mediawiki/extensions/Echo" in result.output  # the Gerrit project
    assert "master" in result.output
    assert "clean" in result.output


def test_repo_selects_by_bare_name(invoke, install: Path):
    result = invoke("--mw", str(install), "repo", "echo", "--no-fetch")
    assert result.exit_code == 0
    assert "extensions/Echo" in result.output


def test_repo_selects_core(invoke, install: Path):
    result = invoke("--mw", str(install), "repo", "core", "--no-fetch")
    assert result.exit_code == 0
    assert "mediawiki/core" in result.output  # the Gerrit project for core


def test_repo_json_reports_the_repository(invoke, install: Path):
    result = invoke("--mw", str(install), "repo", "extensions/Echo", "--no-fetch", "--json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["label"] == "extensions/Echo"
    assert payload["kind"] == "extension"
    assert payload["branch"] == "master"
    assert payload["default_branch"] == "master"
    assert payload["gerrit_project"] == "mediawiki/extensions/Echo"
    assert payload["dirty"] is False


def test_repo_unknown_target_is_an_error(invoke, install: Path):
    result = invoke("--mw", str(install), "repo", "NoSuchThing", "--no-fetch")
    assert result.exit_code != 0
    assert "no repository matching 'NoSuchThing'" in result.output


def test_repo_ambiguous_target_is_an_error(invoke, install: Path):
    result = invoke("--mw", str(install), "repo", "extension", "--no-fetch")
    assert result.exit_code != 0
    assert "matches several repositories" in result.output


def test_repo_json_cannot_be_combined_with_an_action(invoke, install: Path):
    result = invoke("--mw", str(install), "repo", "core", "--no-fetch", "--json", "--reset")
    assert result.exit_code != 0
    assert "--json cannot be combined" in result.output


def test_repo_can_inspect_an_excluded_repository(invoke, install: Path):
    invoke("config", "set", "exclude", "Echo")
    result = invoke("--mw", str(install), "repo", "extensions/Echo", "--no-fetch")
    assert result.exit_code == 0
    assert "extensions/Echo" in result.output


# -- tidy-untracked ---------------------------------------------------------


def test_tidy_untracked_removes_untracked_files_after_confirming(invoke, install: Path):
    echo = install / "extensions" / "Echo"
    fixtures.write(echo, "scratch.txt", "junk\n")
    fixtures.write(echo, "debris/note.txt", "more junk\n")

    result = invoke("--mw", str(install), "repo", "echo", "--no-fetch", "--tidy-untracked", input="y\n")
    assert result.exit_code == 0
    assert "scratch.txt" in result.output
    assert "Remove" in result.output
    assert not (echo / "scratch.txt").exists()
    assert not (echo / "debris").exists()


def test_tidy_untracked_can_be_declined(invoke, install: Path):
    echo = install / "extensions" / "Echo"
    fixtures.write(echo, "scratch.txt", "junk\n")

    result = invoke("--mw", str(install), "repo", "echo", "--no-fetch", "--tidy-untracked", input="n\n")
    assert result.exit_code == 0
    assert "nothing was removed" in result.output
    assert (echo / "scratch.txt").exists()


def test_tidy_untracked_dry_run_changes_nothing(invoke, install: Path):
    echo = install / "extensions" / "Echo"
    fixtures.write(echo, "scratch.txt", "junk\n")

    result = invoke("--mw", str(install), "repo", "echo", "--no-fetch", "--tidy-untracked", "--dry-run")
    assert result.exit_code == 0
    assert "would remove" in result.output
    assert (echo / "scratch.txt").exists()


def test_tidy_untracked_leaves_tracked_changes_alone(invoke, install: Path):
    echo = install / "extensions" / "Echo"
    fixtures.write(echo, "README.md", "local edit\n")  # tracked, modified
    fixtures.write(echo, "scratch.txt", "junk\n")  # untracked

    result = invoke("--mw", str(install), "repo", "echo", "--no-fetch", "--tidy-untracked", "-y")
    assert result.exit_code == 0
    assert not (echo / "scratch.txt").exists()
    assert (echo / "README.md").read_text() == "local edit\n"


def test_tidy_untracked_with_nothing_to_do(invoke, install: Path):
    result = invoke("--mw", str(install), "repo", "echo", "--no-fetch", "--tidy-untracked")
    assert result.exit_code == 0
    assert "no untracked files" in result.output


# -- reset ------------------------------------------------------------------


def test_reset_hard_returns_the_branch_to_origin(invoke, install: Path):
    echo = install / "extensions" / "Echo"
    fixtures.commit(echo, "local only work", name="local.txt")
    fixtures.write(echo, "README.md", "uncommitted\n")
    assert gitutil.read_state(echo).ahead == 1

    result = invoke("--mw", str(install), "repo", "echo", "--no-fetch", "--reset", input="y\n")
    assert result.exit_code == 0
    assert "reset to origin/master" in result.output

    state = gitutil.read_state(echo)
    assert state.ahead == 0
    assert state.dirty is False
    assert not (echo / "local.txt").exists()
    assert (echo / "README.md").read_text() != "uncommitted\n"


def test_reset_can_be_declined(invoke, install: Path):
    echo = install / "extensions" / "Echo"
    fixtures.commit(echo, "local only work", name="local.txt")

    result = invoke("--mw", str(install), "repo", "echo", "--no-fetch", "--reset", input="n\n")
    assert result.exit_code == 0
    assert "nothing was reset" in result.output
    assert gitutil.read_state(echo).ahead == 1


def test_reset_dry_run_changes_nothing(invoke, install: Path):
    echo = install / "extensions" / "Echo"
    fixtures.commit(echo, "local only work", name="local.txt")

    result = invoke("--mw", str(install), "repo", "echo", "--no-fetch", "--reset", "--dry-run")
    assert result.exit_code == 0
    assert "would reset" in result.output
    assert "1 local commit" in result.output
    assert gitutil.read_state(echo).ahead == 1


def test_reset_reports_the_discarded_stakes(invoke, install: Path):
    echo = install / "extensions" / "Echo"
    fixtures.commit(echo, "local only work", name="local.txt")
    fixtures.write(echo, "README.md", "uncommitted\n")

    result = invoke("--mw", str(install), "repo", "echo", "--no-fetch", "--reset", input="n\n")
    assert "1 local commit and 1 uncommitted change" in result.output


def test_reset_and_tidy_together_make_the_tree_pristine(invoke, install: Path):
    echo = install / "extensions" / "Echo"
    fixtures.commit(echo, "local only work", name="local.txt")
    fixtures.write(echo, "scratch.txt", "junk\n")

    result = invoke("--mw", str(install), "repo", "echo", "--no-fetch", "--reset", "--tidy-untracked", "-y")
    assert result.exit_code == 0
    state = gitutil.read_state(echo)
    assert state.ahead == 0
    assert not (echo / "scratch.txt").exists()
    assert not (echo / "local.txt").exists()
    assert state.worktree_summary() == "clean"
