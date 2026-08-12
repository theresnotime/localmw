from __future__ import annotations

from pathlib import Path

import fixtures
import pytest

from localmw import gitutil
from localmw.commands.switch import (
    ALREADY,
    FAILED,
    SKIPPED,
    SWITCHED,
    WOULD_SWITCH,
    perform_switch,
    plan_switch,
)
from localmw.install import KIND_EXTENSION, Repo

DEFAULTS = ["master", "main"]


@pytest.fixture
def repo(echo_repo: Path) -> Repo:
    return Repo(kind=KIND_EXTENSION, name="Echo", path=echo_repo, root=echo_repo.parents[1])


def plan(repo: Repo, *, discard_changes: bool = False):
    return plan_switch(repo, default_branches=DEFAULTS, discard_changes=discard_changes)


def current_branch(path: Path) -> str | None:
    return gitutil.read_state(path).branch


def test_a_repository_already_on_master_is_left_alone(repo: Repo):
    result = plan(repo)
    assert result.status == ALREADY
    assert result.from_branch == "master"
    assert result.interesting is False


def test_switches_off_a_review_branch(repo: Repo):
    fixtures.git(repo.path, "checkout", "--quiet", "-b", "review/1104782")
    fixtures.commit(repo.path, "review work", name="review.txt")

    result = plan(repo)
    assert result.status == WOULD_SWITCH
    assert result.from_branch == "review/1104782"
    assert result.target == "master"

    perform_switch(result)
    assert result.status == SWITCHED
    assert current_branch(repo.path) == "master"
    # The branch we left is still there, with its commit.
    assert "review/1104782" in [branch.name for branch in gitutil.list_branches(repo.path)]
    assert not (repo.path / "review.txt").exists()


def test_switches_out_of_a_detached_head(repo: Repo):
    fixtures.commit(repo.path, "second")
    fixtures.git(repo.path, "checkout", "--quiet", "HEAD~1")

    result = plan(repo)
    assert result.status == WOULD_SWITCH
    assert result.from_branch.startswith("detached @ ")

    perform_switch(result)
    assert result.status == SWITCHED
    assert current_branch(repo.path) == "master"


def test_uncommitted_changes_block_the_switch(repo: Repo):
    fixtures.git(repo.path, "checkout", "--quiet", "-b", "review/1104782")
    fixtures.write(repo.path, "README.md", "local edit\n")

    result = plan(repo)
    assert result.status == SKIPPED
    assert result.reason == "1 uncommitted change"
    assert "--discard-changes" in result.hint
    assert current_branch(repo.path) == "review/1104782"


def test_discard_changes_throws_the_work_away(repo: Repo):
    fixtures.git(repo.path, "checkout", "--quiet", "-b", "review/1104782")
    fixtures.write(repo.path, "README.md", "local edit\n")

    result = plan(repo, discard_changes=True)
    assert result.status == WOULD_SWITCH
    assert result.discard == 1

    perform_switch(result)
    assert result.status == SWITCHED
    assert current_branch(repo.path) == "master"
    assert (repo.path / "README.md").read_text() != "local edit\n"


def test_untracked_files_neither_block_nor_are_discarded(repo: Repo):
    fixtures.git(repo.path, "checkout", "--quiet", "-b", "review/1104782")
    fixtures.write(repo.path, "scratch.txt", "keep me\n")

    result = plan(repo)
    assert result.status == WOULD_SWITCH
    assert result.discard == 0

    perform_switch(result)
    assert result.status == SWITCHED
    assert (repo.path / "scratch.txt").read_text() == "keep me\n"


def test_creates_the_default_branch_from_its_remote_when_missing(tmp_path: Path, origins: Path):
    path = fixtures.make_repo_with_origin(tmp_path / "Echo", origins)
    fixtures.git(path, "checkout", "--quiet", "-b", "review/1104782")
    fixtures.git(path, "branch", "--delete", "--force", "master")
    repo = Repo(kind=KIND_EXTENSION, name="Echo", path=path, root=tmp_path)

    result = plan(repo)
    assert result.status == WOULD_SWITCH
    assert result.create_from == "origin/master"

    perform_switch(result)
    assert result.status == SWITCHED
    assert current_branch(path) == "master"
    assert gitutil.read_state(path).upstream == "origin/master"


def test_main_is_recognised_as_the_default(tmp_path: Path, origins: Path):
    path = fixtures.make_repo_with_origin(tmp_path / "MainRepo", origins, branch="main")
    fixtures.git(path, "checkout", "--quiet", "-b", "review/1104782")
    repo = Repo(kind=KIND_EXTENSION, name="MainRepo", path=path, root=tmp_path)

    result = plan(repo)
    assert result.target == "main"
    perform_switch(result)
    assert current_branch(path) == "main"


def test_reports_a_repository_with_no_default_branch(tmp_path: Path):
    path = fixtures.init_repo(tmp_path / "Odd", branch="wip")
    fixtures.commit(path, "only commit")
    repo = Repo(kind=KIND_EXTENSION, name="Odd", path=path, root=tmp_path)

    result = plan(repo)
    assert result.status == SKIPPED
    assert result.reason == "no master or main branch to switch to"


def test_reports_a_directory_that_is_not_a_repo(tmp_path: Path):
    (tmp_path / "NotARepo").mkdir()
    repo = Repo(kind=KIND_EXTENSION, name="NotARepo", path=tmp_path / "NotARepo", root=tmp_path)

    result = plan(repo)
    assert result.status == FAILED
    assert result.reason


def test_a_failed_checkout_is_reported(repo: Repo, monkeypatch):
    fixtures.git(repo.path, "checkout", "--quiet", "-b", "review/1104782")
    result = plan(repo)

    def boom(*args, **kwargs):
        raise gitutil.GitError(["checkout", "master"], 1, "error: cannot switch branches\n")

    monkeypatch.setattr(gitutil, "checkout", boom)

    perform_switch(result)
    assert result.status == FAILED
    assert result.reason == "error: cannot switch branches"


def test_reports_how_far_behind_the_default_branch_is(repo: Repo, origins: Path, tmp_path: Path):
    fixtures.git(repo.path, "checkout", "--quiet", "-b", "review/1104782")
    fixtures.advance_origin(origins / "Echo", tmp_path, count=2)
    fixtures.git(repo.path, "fetch", "--quiet")

    result = plan(repo)
    perform_switch(result)
    assert result.status == SWITCHED
    assert result.behind == 2
