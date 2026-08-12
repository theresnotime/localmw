from __future__ import annotations

from pathlib import Path

import click
import fixtures
import pytest

from localmw import gitutil
from localmw.commands.pull import (
    FAILED,
    SKIPPED,
    UP_TO_DATE,
    UPDATED,
    WOULD_UPDATE,
    PullOutcome,
    ask_about,
    pull_repo,
    skip_at_prompt,
)
from localmw.install import KIND_EXTENSION, Repo

DEFAULTS = ["master", "main"]


@pytest.fixture
def repo(echo_repo: Path) -> Repo:
    return Repo(kind=KIND_EXTENSION, name="Echo", path=echo_repo, root=echo_repo.parents[1])


def pull(repo: Repo, **overrides):
    kwargs = {
        "default_branches": DEFAULTS,
        "strategy": "ff-only",
        "allow_dirty": False,
        "allow_branch": False,
        "prune": False,
        "submodules": False,
        "dry_run": False,
    }
    kwargs.update(overrides)
    return pull_repo(repo, **kwargs)


def test_up_to_date(repo: Repo):
    outcome = pull(repo)
    assert outcome.status == UP_TO_DATE
    assert outcome.branch == "master"
    assert outcome.interesting is False


def test_fast_forwards_when_behind(repo: Repo, origins: Path, tmp_path: Path):
    fixtures.advance_origin(origins / "Echo", tmp_path, count=3)

    outcome = pull(repo)
    assert outcome.status == UPDATED
    assert outcome.commits == 3
    assert gitutil.read_state(repo.path).behind == 0
    assert (repo.path / "upstream.txt").exists()


def test_skips_uncommitted_changes(repo: Repo, origins: Path, tmp_path: Path):
    fixtures.advance_origin(origins / "Echo", tmp_path)
    fixtures.write(repo.path, "README.md", "local edit\n")

    outcome = pull(repo)
    assert outcome.status == SKIPPED
    assert "uncommitted change" in outcome.reason
    assert "--allow-dirty" in outcome.hint
    assert not (repo.path / "upstream.txt").exists()

    outcome = pull(repo, allow_dirty=True)
    assert outcome.status == UPDATED
    assert (repo.path / "README.md").read_text() == "local edit\n"


def test_untracked_files_do_not_block_a_pull(repo: Repo, origins: Path, tmp_path: Path):
    fixtures.advance_origin(origins / "Echo", tmp_path)
    fixtures.write(repo.path, "scratch.txt")

    assert pull(repo).status == UPDATED


def test_skips_a_non_default_branch(repo: Repo, origins: Path, tmp_path: Path):
    fixtures.advance_origin(origins / "Echo", tmp_path)
    fixtures.git(repo.path, "checkout", "--quiet", "-b", "review/1104782", "--track", "origin/master")

    outcome = pull(repo)
    assert outcome.status == SKIPPED
    assert outcome.reason == "on branch review/1104782"
    assert "--any-branch" in outcome.hint

    outcome = pull(repo, allow_branch=True)
    assert outcome.status == UPDATED
    assert outcome.branch == "review/1104782"


def test_pulls_main_when_that_is_the_default(tmp_path: Path, origins: Path):
    path = fixtures.make_repo_with_origin(tmp_path / "MainRepo", origins, branch="main")
    repo = Repo(kind=KIND_EXTENSION, name="MainRepo", path=path, root=tmp_path)
    fixtures.advance_origin(origins / "MainRepo", tmp_path)

    assert pull(repo).status == UPDATED


def test_skips_a_detached_head(repo: Repo):
    fixtures.commit(repo.path, "second")
    fixtures.git(repo.path, "checkout", "--quiet", "HEAD~1")

    outcome = pull(repo)
    assert outcome.status == SKIPPED
    assert outcome.reason == "detached HEAD"


def test_skips_a_branch_without_an_upstream(tmp_path: Path, origins: Path):
    path = fixtures.make_repo_with_origin(tmp_path / "NoUpstream", origins)
    fixtures.git(path, "checkout", "--quiet", "-b", "master-local")
    fixtures.git(path, "branch", "--unset-upstream", "master-local", check=False)
    repo = Repo(kind=KIND_EXTENSION, name="NoUpstream", path=path, root=tmp_path)

    outcome = pull(repo, allow_branch=True)
    assert outcome.status == SKIPPED
    assert outcome.reason == "no upstream branch"


def test_ff_only_refuses_to_touch_a_diverged_branch(repo: Repo, origins: Path, tmp_path: Path):
    fixtures.commit(repo.path, "unpushed work", name="local.txt")
    fixtures.advance_origin(origins / "Echo", tmp_path)

    outcome = pull(repo)
    assert outcome.status == SKIPPED
    assert "diverged: 1 local commit" in outcome.reason
    assert "--strategy rebase" in outcome.hint
    assert gitutil.read_state(repo.path).behind == 1


def test_rebase_strategy_replays_local_commits(repo: Repo, origins: Path, tmp_path: Path):
    fixtures.commit(repo.path, "unpushed work", name="local.txt")
    fixtures.advance_origin(origins / "Echo", tmp_path)

    outcome = pull(repo, strategy="rebase")
    assert outcome.status == UPDATED
    state = gitutil.read_state(repo.path)
    assert (state.ahead, state.behind) == (1, 0)
    assert (repo.path / "upstream.txt").exists()


def test_merge_strategy_creates_a_merge(repo: Repo, origins: Path, tmp_path: Path):
    fixtures.commit(repo.path, "unpushed work", name="local.txt")
    fixtures.advance_origin(origins / "Echo", tmp_path)

    outcome = pull(repo, strategy="merge")
    assert outcome.status == UPDATED
    assert gitutil.read_state(repo.path).behind == 0


def test_reports_unpushed_commits_when_already_up_to_date(repo: Repo):
    fixtures.commit(repo.path, "unpushed work", name="local.txt")

    outcome = pull(repo)
    assert outcome.status == UP_TO_DATE
    assert outcome.ahead == 1
    assert outcome.interesting is True


def test_dry_run_changes_nothing(repo: Repo, origins: Path, tmp_path: Path):
    fixtures.advance_origin(origins / "Echo", tmp_path, count=2)

    outcome = pull(repo, dry_run=True)
    assert outcome.status == WOULD_UPDATE
    assert outcome.commits == 2
    assert gitutil.read_state(repo.path).behind == 2
    assert not (repo.path / "upstream.txt").exists()


def test_pulling_without_fetching_uses_what_the_last_fetch_saw(repo: Repo, origins: Path, tmp_path: Path):
    """How --interactive pulls: check (which fetches), ask, then pull without fetching again."""
    fixtures.advance_origin(origins / "Echo", tmp_path, count=2)

    assert pull(repo, dry_run=True).status == WOULD_UPDATE  # fetches
    outcome = pull(repo, do_fetch=False)
    assert outcome.status == UPDATED
    assert outcome.commits == 2
    assert (repo.path / "upstream.txt").exists()


def test_without_fetching_an_unfetched_repository_looks_up_to_date(repo: Repo, origins: Path, tmp_path: Path):
    fixtures.advance_origin(origins / "Echo", tmp_path)

    assert pull(repo, do_fetch=False).status == UP_TO_DATE
    assert not (repo.path / "upstream.txt").exists()


def test_the_prompt_takes_pull_skip_or_a_bare_letter(repo: Repo, monkeypatch):
    outcome = PullOutcome(repo, WOULD_UPDATE, branch="master", commits=2)

    def answering(value: str):
        monkeypatch.setattr(click, "prompt", lambda *a, _value=value, **kw: _value)

    for answer in ("p", "P", "pull", "y", " yes "):
        answering(answer)
        assert ask_about(outcome) is True
    for answer in ("s", "skip", "n", "NO"):
        answering(answer)
        assert ask_about(outcome) is False


def test_the_prompt_asks_again_after_an_answer_it_does_not_understand(repo: Repo, monkeypatch):
    answers = iter(["maybe", "", "s"])
    asked = []

    def fake_prompt(text, **kwargs):
        asked.append(text)
        return next(answers)

    monkeypatch.setattr(click, "prompt", fake_prompt)

    outcome = PullOutcome(repo, WOULD_UPDATE, branch="master", commits=1)
    assert ask_about(outcome) is False
    assert len(asked) == 3
    assert "1 commit behind" in asked[0]
    assert "[P]ull / [s]kip" in asked[0]


def test_skipping_at_the_prompt_says_so_in_the_results(repo: Repo):
    outcome = PullOutcome(repo, WOULD_UPDATE, branch="master", commits=3)
    skip_at_prompt(outcome)
    assert outcome.status == SKIPPED
    assert outcome.reason == "3 commits behind"
    assert outcome.hint == "skipped at the prompt"


def test_reports_a_failed_fetch(tmp_path: Path, origins: Path):
    path = fixtures.make_repo_with_origin(tmp_path / "Broken", origins)
    fixtures.git(path, "remote", "set-url", "origin", str(tmp_path / "gone"))
    repo = Repo(kind=KIND_EXTENSION, name="Broken", path=path, root=tmp_path)

    outcome = pull(repo)
    assert outcome.status == FAILED
    assert outcome.reason.startswith("fetch:")


def test_reports_a_directory_that_is_not_a_repo(tmp_path: Path):
    (tmp_path / "NotARepo").mkdir()
    repo = Repo(kind=KIND_EXTENSION, name="NotARepo", path=tmp_path / "NotARepo", root=tmp_path)

    outcome = pull(repo)
    assert outcome.status == FAILED
    assert outcome.reason


def test_submodules_are_updated_only_when_asked_for(repo: Repo, origins: Path, tmp_path: Path, monkeypatch):
    fixtures.advance_origin(origins / "Echo", tmp_path, count=2)
    calls = []
    monkeypatch.setattr(gitutil, "update_submodules", lambda path: calls.append(path))

    assert pull(repo).status == UPDATED
    assert calls == []

    fixtures.advance_origin(origins / "Echo", tmp_path, message="more", count=1)
    assert pull(repo, submodules=True).status == UPDATED
    assert calls == [repo.path]


def test_a_submodule_failure_is_noted_but_the_pull_still_counts(repo: Repo, origins: Path, tmp_path: Path, monkeypatch):
    fixtures.advance_origin(origins / "Echo", tmp_path)

    def boom(path):
        raise gitutil.GitError(["submodule", "update"], 1, "fatal: no such remote\n")

    monkeypatch.setattr(gitutil, "update_submodules", boom)

    outcome = pull(repo, submodules=True)
    assert outcome.status == UPDATED
    assert outcome.hint == "submodules: fatal: no such remote"
