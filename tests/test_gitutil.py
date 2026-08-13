from __future__ import annotations

from pathlib import Path

import fixtures
import pytest

from localmw import gitutil


@pytest.fixture
def repo(echo_repo: Path) -> Path:
    return echo_repo


def test_is_repo(tmp_path: Path, repo: Path):
    assert gitutil.is_repo(repo)
    assert not gitutil.is_repo(tmp_path)


def test_read_state_of_a_clean_checkout(repo: Path):
    state = gitutil.read_state(repo)
    assert state.error is None
    assert state.branch == "master"
    assert state.detached is False
    assert state.upstream == "origin/master"
    assert (state.ahead, state.behind) == (0, 0)
    assert state.dirty is False
    assert state.worktree_summary() == "clean"
    assert state.last_commit.subject == "initial commit"
    assert state.last_commit.relative_date
    assert state.head


def test_read_state_sees_unstaged_and_staged_changes(repo: Path):
    fixtures.write(repo, "README.md", "changed\n")
    state = gitutil.read_state(repo)
    assert (state.staged, state.unstaged) == (0, 1)
    assert state.dirty is True
    assert state.worktree_summary() == "~1"

    fixtures.git(repo, "add", "README.md")
    state = gitutil.read_state(repo)
    assert (state.staged, state.unstaged) == (1, 0)
    assert state.worktree_summary() == "+1"


def test_untracked_files_do_not_count_as_dirty(repo: Path):
    fixtures.write(repo, "scratch.txt")
    state = gitutil.read_state(repo)
    assert state.untracked == 1
    assert state.dirty is False
    assert state.worktree_summary() == "?1"


def test_read_state_counts_behind_after_fetch(repo: Path, origins: Path, tmp_path: Path):
    fixtures.advance_origin(origins / "Echo", tmp_path, count=2)
    assert gitutil.read_state(repo).behind == 0  # not fetched yet

    gitutil.fetch(repo)
    state = gitutil.read_state(repo)
    assert (state.ahead, state.behind) == (0, 2)


def test_read_state_counts_ahead_and_diverged(repo: Path, origins: Path, tmp_path: Path):
    fixtures.commit(repo, "local work", name="local.txt")
    state = gitutil.read_state(repo)
    assert (state.ahead, state.behind) == (1, 0)

    fixtures.advance_origin(origins / "Echo", tmp_path)
    gitutil.fetch(repo)
    state = gitutil.read_state(repo)
    assert (state.ahead, state.behind) == (1, 1)


def test_read_state_of_a_detached_head(repo: Path):
    fixtures.commit(repo, "second")
    fixtures.git(repo, "checkout", "--quiet", "HEAD~1")
    state = gitutil.read_state(repo)
    assert state.detached is True
    assert state.branch is None


def test_read_state_of_a_repo_without_an_upstream(tmp_path: Path):
    solo = fixtures.init_repo(tmp_path / "solo")
    fixtures.commit(solo, "only commit")
    state = gitutil.read_state(solo)
    assert state.upstream is None
    assert state.has_upstream is False


def test_read_state_reports_an_error_for_a_non_repo(tmp_path: Path):
    state = gitutil.read_state(tmp_path)
    assert state.error
    assert state.worktree_summary() == "?"


def test_default_branch_from_origin_head(repo: Path):
    assert gitutil.default_branch(repo, ["master", "main"]) == "master"


def test_default_branch_falls_back_to_candidates(tmp_path: Path):
    solo = fixtures.init_repo(tmp_path / "solo", branch="main")
    fixtures.commit(solo, "only commit")
    assert gitutil.default_branch(solo, ["master", "main"]) == "main"
    assert gitutil.default_branch(solo, ["nonexistent"]) is None


def test_fetch_failure_raises_git_error(tmp_path: Path):
    solo = fixtures.init_repo(tmp_path / "solo")
    fixtures.commit(solo, "only commit")
    fixtures.git(solo, "remote", "add", "origin", str(tmp_path / "does-not-exist"))
    with pytest.raises(gitutil.GitError) as excinfo:
        gitutil.fetch(solo)
    assert excinfo.value.detail
    assert "git fetch" in excinfo.value.summary


def test_integrate_fast_forwards(repo: Path, origins: Path, tmp_path: Path):
    fixtures.advance_origin(origins / "Echo", tmp_path, count=3)
    gitutil.fetch(repo)
    gitutil.integrate(repo, "origin/master", "ff-only")
    state = gitutil.read_state(repo)
    assert (state.ahead, state.behind) == (0, 0)


def test_integrate_ff_only_refuses_a_diverged_branch(repo: Path, origins: Path, tmp_path: Path):
    fixtures.commit(repo, "local work", name="local.txt")
    fixtures.advance_origin(origins / "Echo", tmp_path)
    gitutil.fetch(repo)
    with pytest.raises(gitutil.GitError):
        gitutil.integrate(repo, "origin/master", "ff-only")


def test_integrate_rebase_replays_local_commits(repo: Path, origins: Path, tmp_path: Path):
    fixtures.commit(repo, "local work", name="local.txt")
    fixtures.advance_origin(origins / "Echo", tmp_path)
    gitutil.fetch(repo)
    gitutil.integrate(repo, "origin/master", "rebase")
    state = gitutil.read_state(repo)
    assert (state.ahead, state.behind) == (1, 0)


def test_count_commits(repo: Path, origins: Path, tmp_path: Path):
    fixtures.advance_origin(origins / "Echo", tmp_path, count=2)
    gitutil.fetch(repo)
    assert gitutil.count_commits(repo, "HEAD", "origin/master") == 2
    assert gitutil.count_commits(repo, "origin/master", "HEAD") == 0
    assert gitutil.count_commits(repo, "HEAD", "refs/heads/nope") == 0


def test_list_branches_filters_by_prefix_and_marks_current(repo: Path):
    fixtures.git(repo, "branch", "review/1104782")
    fixtures.git(repo, "branch", "review/T12345")
    fixtures.git(repo, "branch", "wip-something")

    names = [b.name for b in gitutil.list_branches(repo)]
    assert set(names) == {"master", "review/1104782", "review/T12345", "wip-something"}

    review = gitutil.list_branches(repo, prefix="review/")
    assert {b.name for b in review} == {"review/1104782", "review/T12345"}
    assert all(not b.is_current for b in review)
    assert all(b.sha and b.relative_date for b in review)

    current = [b for b in gitutil.list_branches(repo) if b.is_current]
    assert [b.name for b in current] == ["master"]


def test_change_ids_reads_trailers_from_branch_commits(repo: Path):
    fixtures.git(repo, "checkout", "--quiet", "-b", "review/1104782")
    fixtures.commit_with_change_id(repo, "I1111111111111111111111111111111111111111", "first", name="a.txt")
    fixtures.commit_with_change_id(repo, "I2222222222222222222222222222222222222222", "second", name="b.txt")

    ids = gitutil.change_ids(repo, "review/1104782", "origin/master")
    assert ids == [
        "I2222222222222222222222222222222222222222",
        "I1111111111111111111111111111111111111111",
    ]


def test_change_ids_deduplicates_and_handles_missing_trailers(repo: Path):
    fixtures.git(repo, "checkout", "--quiet", "-b", "review/dup")
    fixtures.commit_with_change_id(repo, "I3333333333333333333333333333333333333333", "amended", name="a.txt")
    fixtures.commit_with_change_id(repo, "I3333333333333333333333333333333333333333", "again", name="b.txt")
    fixtures.commit(repo, "no trailer here", name="c.txt")

    assert gitutil.change_ids(repo, "review/dup", "origin/master") == ["I3333333333333333333333333333333333333333"]


def test_change_ids_without_a_base_only_looks_at_the_tip(repo: Path):
    fixtures.git(repo, "checkout", "--quiet", "-b", "review/tip")
    fixtures.commit_with_change_id(repo, "I4444444444444444444444444444444444444444", "older", name="a.txt")
    fixtures.commit_with_change_id(repo, "I5555555555555555555555555555555555555555", "newer", name="b.txt")

    assert gitutil.change_ids(repo, "review/tip", None) == ["I5555555555555555555555555555555555555555"]


def test_is_ancestor_and_delete_branch(repo: Path):
    fixtures.git(repo, "branch", "review/merged")
    assert gitutil.is_ancestor(repo, "review/merged", "master")

    fixtures.git(repo, "checkout", "--quiet", "-b", "review/unmerged")
    fixtures.commit(repo, "unmerged work", name="x.txt")
    fixtures.git(repo, "checkout", "--quiet", "master")
    assert not gitutil.is_ancestor(repo, "review/unmerged", "master")

    gitutil.delete_branch(repo, "review/merged")
    assert "review/merged" not in [b.name for b in gitutil.list_branches(repo)]

    with pytest.raises(gitutil.GitError):
        gitutil.delete_branch(repo, "review/unmerged")
    gitutil.delete_branch(repo, "review/unmerged", force=True)
    assert "review/unmerged" not in [b.name for b in gitutil.list_branches(repo)]


def test_ref_exists(repo: Path):
    assert gitutil.ref_exists(repo, "refs/heads/master")
    assert gitutil.ref_exists(repo, "origin/master")
    assert not gitutil.ref_exists(repo, "refs/heads/nope")


def test_gerrit_project_from_gitreview(repo: Path):
    assert gitutil.gerrit_project(repo) == "mediawiki/extensions/Echo"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("ssh://sammy@gerrit.wikimedia.org:29418/mediawiki/core", "mediawiki/core"),
        ("ssh://sammy@gerrit.wikimedia.org:29418/mediawiki/extensions/Echo.git", "mediawiki/extensions/Echo"),
        ("https://gerrit.wikimedia.org/r/mediawiki/skins/Vector", "mediawiki/skins/Vector"),
        ("git@github.com:theresnotime/localmw.git", "theresnotime/localmw"),
    ],
)
def test_gerrit_project_from_remote_url(tmp_path: Path, url: str, expected: str):
    solo = fixtures.init_repo(tmp_path / f"repo-{abs(hash(url))}")
    fixtures.commit(solo, "only commit")
    fixtures.git(solo, "remote", "add", "origin", url)
    assert gitutil.gerrit_project(solo) == expected


def test_gerrit_project_is_none_without_a_remote(tmp_path: Path):
    solo = fixtures.init_repo(tmp_path / "solo")
    fixtures.commit(solo, "only commit")
    assert gitutil.gerrit_project(solo) is None


def test_git_error_detail_skips_hints():
    error = gitutil.GitError(["merge", "--ff-only"], 128, "hint: something\nfatal: Not possible\n")
    assert error.detail == "fatal: Not possible"

    bare = gitutil.GitError(["merge"], 1, "")
    assert bare.detail == "git exited with status 1"


def test_run_reports_a_timeout(repo: Path):
    with pytest.raises(gitutil.GitError, match="timed out"):
        gitutil.run(repo, ["-c", "core.pager=cat", "log", "--all"], timeout=0)


def test_clean_lists_then_removes_untracked_files(repo: Path):
    fixtures.write(repo, "scratch.txt", "junk\n")
    fixtures.write(repo, "debris/note.txt", "more junk\n")

    would = gitutil.clean(repo, dry_run=True)
    assert "scratch.txt" in would
    assert any(entry.startswith("debris/") for entry in would)
    assert (repo / "scratch.txt").exists()

    removed = gitutil.clean(repo, dry_run=False)
    assert "scratch.txt" in removed
    assert not (repo / "scratch.txt").exists()
    assert not (repo / "debris").exists()


def test_clean_leaves_tracked_changes_alone(repo: Path):
    fixtures.write(repo, "README.md", "local edit\n")
    assert gitutil.clean(repo, dry_run=True) == []
    assert (repo / "README.md").read_text() == "local edit\n"


def test_reset_hard_moves_the_branch_and_drops_changes(repo: Path):
    fixtures.commit(repo, "local only", name="local.txt")
    fixtures.write(repo, "README.md", "uncommitted\n")
    assert gitutil.read_state(repo).ahead == 1

    gitutil.reset_hard(repo, "origin/master")

    state = gitutil.read_state(repo)
    assert state.ahead == 0
    assert state.dirty is False
    assert not (repo / "local.txt").exists()


def test_remote_url_reads_origin(repo: Path):
    assert gitutil.remote_url(repo).endswith("Echo")
    solo = fixtures.init_repo(repo.parent / "solo")
    assert gitutil.remote_url(solo) is None
