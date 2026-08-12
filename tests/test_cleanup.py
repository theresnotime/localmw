from __future__ import annotations

import dataclasses
from pathlib import Path

import fixtures
import pytest

from localmw import gerrit, gitutil
from localmw.commands.cleanup import DELETE, KEEP, SKIP, Candidate, _lookup_changes, decide, scan_repo
from localmw.install import KIND_EXTENSION, Repo

DEFAULTS = ["master", "main"]

MERGED_ID = "I" + "1" * 40
OPEN_ID = "I" + "2" * 40
ABANDONED_ID = "I" + "3" * 40
UNKNOWN_ID = "I" + "4" * 40

STATUSES = {
    MERGED_ID: "MERGED",
    OPEN_ID: "NEW",
    ABANDONED_ID: "ABANDONED",
}


@pytest.fixture
def repo(echo_repo: Path) -> Repo:
    return Repo(kind=KIND_EXTENSION, name="Echo", path=echo_repo, root=echo_repo.parents[1])


def add_review_branch(repo: Repo, name: str, *change_ids: str, checkout: bool = False) -> None:
    """Create a review/* branch carrying one commit per Change-Id."""
    fixtures.git(repo.path, "checkout", "--quiet", "-b", name, "master")
    slug = name.replace("/", "-")
    for index, change_id in enumerate(change_ids):
        fixtures.commit_with_change_id(repo.path, change_id, f"work {index}", name=f"{slug}-{index}.txt")
    if not checkout:
        fixtures.git(repo.path, "checkout", "--quiet", "master")


def scan(repo: Repo, prefix: str = "review/"):
    return scan_repo(repo, prefix=prefix, default_branches=DEFAULTS)


def candidate_named(scan_result, name: str) -> Candidate:
    for candidate in scan_result.candidates:
        if candidate.branch.name == name:
            return candidate
    raise AssertionError(f"no candidate for {name}: {[c.branch.name for c in scan_result.candidates]}")


def test_scan_finds_review_branches_and_their_change_ids(repo: Repo):
    add_review_branch(repo, "review/1104782", MERGED_ID)
    add_review_branch(repo, "review/T376215", OPEN_ID, ABANDONED_ID)
    fixtures.git(repo.path, "branch", "wip-not-a-review-branch")

    result = scan(repo)

    assert {c.branch.name for c in result.candidates} == {"review/1104782", "review/T376215"}
    assert result.default_branch == "master"
    assert result.base_ref == "origin/master"
    assert result.project == "mediawiki/extensions/Echo"
    assert candidate_named(result, "review/1104782").change_ids == [MERGED_ID]
    assert candidate_named(result, "review/T376215").change_ids == [ABANDONED_ID, OPEN_ID]


def test_scan_never_offers_the_default_branch(repo: Repo):
    result = scan(repo, prefix="")
    assert "master" not in {c.branch.name for c in result.candidates}


def test_scan_skips_the_checked_out_branch(repo: Repo):
    add_review_branch(repo, "review/current", MERGED_ID, checkout=True)

    candidate = candidate_named(scan(repo), "review/current")
    assert candidate.decision == SKIP
    assert candidate.reason == "checked out right now"


def test_scan_notices_branches_already_merged_locally(repo: Repo):
    fixtures.git(repo.path, "branch", "review/nothing-new")

    candidate = candidate_named(scan(repo), "review/nothing-new")
    assert candidate.merged_locally is True
    assert candidate.change_ids == []


def test_scan_reports_a_broken_repository(tmp_path: Path):
    (tmp_path / "NotARepo").mkdir()
    repo = Repo(kind=KIND_EXTENSION, name="NotARepo", path=tmp_path / "NotARepo", root=tmp_path)
    result = scan(repo)
    assert result.error
    assert result.candidates == []


def test_a_locally_merged_branch_is_deleted_without_force(repo: Repo):
    fixtures.git(repo.path, "branch", "review/nothing-new")
    candidate = candidate_named(scan(repo), "review/nothing-new")

    decide(candidate, default_branch="master", include_abandoned=False, use_gerrit=True)
    assert candidate.decision == DELETE
    assert candidate.reason == "already merged locally"
    assert candidate.force is False


def test_a_merged_change_is_deleted_with_force(repo: Repo, monkeypatch):
    add_review_branch(repo, "review/1104782", MERGED_ID)
    result = scan(repo)
    _lookup_changes(fixtures.FakeGerritClient(STATUSES), [result])

    candidate = candidate_named(result, "review/1104782")
    decide(candidate, default_branch="master", include_abandoned=False, use_gerrit=True)
    assert candidate.decision == DELETE
    assert candidate.reason == "merged in Gerrit"
    assert candidate.force is True
    assert candidate.change_label.startswith("11")


def test_an_open_change_is_kept(repo: Repo):
    add_review_branch(repo, "review/open", OPEN_ID)
    result = scan(repo)
    _lookup_changes(fixtures.FakeGerritClient(STATUSES), [result])

    candidate = candidate_named(result, "review/open")
    decide(candidate, default_branch="master", include_abandoned=False, use_gerrit=True)
    assert candidate.decision == KEEP
    assert candidate.reason == "still open in Gerrit"


def test_an_abandoned_change_needs_the_flag(repo: Repo):
    add_review_branch(repo, "review/abandoned", ABANDONED_ID)
    result = scan(repo)
    _lookup_changes(fixtures.FakeGerritClient(STATUSES), [result])
    candidate = candidate_named(result, "review/abandoned")

    decide(candidate, default_branch="master", include_abandoned=False, use_gerrit=True)
    assert candidate.decision == KEEP
    assert "--include-abandoned" in candidate.reason

    decide(candidate, default_branch="master", include_abandoned=True, use_gerrit=True)
    assert candidate.decision == DELETE
    assert candidate.reason == "abandoned in Gerrit"


def test_a_branch_gerrit_does_not_know_about_is_kept(repo: Repo):
    add_review_branch(repo, "review/unknown", UNKNOWN_ID)
    result = scan(repo)
    _lookup_changes(fixtures.FakeGerritClient(STATUSES), [result])

    candidate = candidate_named(result, "review/unknown")
    decide(candidate, default_branch="master", include_abandoned=False, use_gerrit=True)
    assert candidate.decision == KEEP
    assert candidate.reason == "change not found in Gerrit"


def test_a_branch_without_a_change_id_is_kept(repo: Repo):
    fixtures.git(repo.path, "checkout", "--quiet", "-b", "review/no-trailer", "master")
    fixtures.commit(repo.path, "no Change-Id here", name="x.txt")
    fixtures.git(repo.path, "checkout", "--quiet", "master")

    candidate = candidate_named(scan(repo), "review/no-trailer")
    decide(candidate, default_branch="master", include_abandoned=False, use_gerrit=True)
    assert candidate.decision == KEEP
    assert candidate.reason == "no Change-Id in its commits"


def test_a_gerrit_failure_keeps_everything(repo: Repo):
    add_review_branch(repo, "review/1104782", MERGED_ID)
    result = scan(repo)
    _lookup_changes(fixtures.FakeGerritClient(STATUSES, error="could not reach gerrit"), [result])

    candidate = candidate_named(result, "review/1104782")
    decide(candidate, default_branch="master", include_abandoned=False, use_gerrit=True)
    assert candidate.decision == KEEP
    assert "could not reach gerrit" in candidate.reason


def test_without_gerrit_only_locally_merged_branches_go(repo: Repo):
    add_review_branch(repo, "review/1104782", MERGED_ID)
    result = scan(repo)

    candidate = candidate_named(result, "review/1104782")
    decide(candidate, default_branch="master", include_abandoned=False, use_gerrit=False)
    assert candidate.decision == KEEP
    assert candidate.reason == "not merged locally (Gerrit lookup disabled)"


def test_a_branch_is_only_deleted_when_every_change_on_it_merged(repo: Repo):
    add_review_branch(repo, "review/partly", MERGED_ID, OPEN_ID)
    add_review_branch(repo, "review/fully", MERGED_ID, ABANDONED_ID)
    result = scan(repo)
    _lookup_changes(fixtures.FakeGerritClient(STATUSES), [result])

    partly = candidate_named(result, "review/partly")
    decide(partly, default_branch="master", include_abandoned=False, use_gerrit=True)
    assert partly.decision == KEEP

    fully = candidate_named(result, "review/fully")
    decide(fully, default_branch="master", include_abandoned=False, use_gerrit=True)
    assert fully.decision == KEEP
    decide(fully, default_branch="master", include_abandoned=True, use_gerrit=True)
    assert fully.decision == DELETE


def test_a_change_merged_on_another_branch_still_counts_as_merged(repo: Repo):
    add_review_branch(repo, "review/backport", MERGED_ID)
    result = scan(repo)
    _lookup_changes(fixtures.FakeGerritClient({MERGED_ID: ("MERGED", "REL1_43")}), [result])

    candidate = candidate_named(result, "review/backport")
    decide(candidate, default_branch="master", include_abandoned=False, use_gerrit=True)
    assert candidate.decision == DELETE


def test_lookup_asks_once_per_repository_and_scopes_by_project(repo: Repo):
    add_review_branch(repo, "review/a", MERGED_ID)
    add_review_branch(repo, "review/b", OPEN_ID)
    result = scan(repo)

    client = fixtures.FakeGerritClient(STATUSES)
    _lookup_changes(client, [result])

    assert len(client.queries) == 1
    assert client.queries[0]["project"] == "mediawiki/extensions/Echo"
    assert set(client.queries[0]["change_ids"]) == {MERGED_ID, OPEN_ID}


def test_lookup_is_skipped_when_there_is_nothing_to_ask_about(repo: Repo):
    fixtures.git(repo.path, "branch", "review/nothing-new")
    result = scan(repo)

    client = fixtures.FakeGerritClient(STATUSES)
    _lookup_changes(client, [result])
    assert client.queries == []


def test_deleting_a_gerrit_merged_branch_needs_force(repo: Repo):
    """The branch is not an ancestor of master, so plain 'git branch -d' refuses."""
    add_review_branch(repo, "review/1104782", MERGED_ID)

    with pytest.raises(gitutil.GitError):
        gitutil.delete_branch(repo.path, "review/1104782")
    gitutil.delete_branch(repo.path, "review/1104782", force=True)
    assert "review/1104782" not in [b.name for b in gitutil.list_branches(repo.path)]


def test_change_label_falls_back_to_the_change_id(repo: Repo):
    add_review_branch(repo, "review/unknown", UNKNOWN_ID)
    candidate = candidate_named(scan(repo), "review/unknown")
    assert candidate.change_label == UNKNOWN_ID[:9]
    assert candidate.primary_change is None


def test_change_label_counts_extra_changes(repo: Repo):
    add_review_branch(repo, "review/stack", MERGED_ID, OPEN_ID)
    result = scan(repo)
    _lookup_changes(fixtures.FakeGerritClient(STATUSES), [result])
    candidate = candidate_named(result, "review/stack")
    assert candidate.change_label.endswith("(+1)")


def test_gerrit_change_dataclass_is_immutable():
    change = gerrit.Change(MERGED_ID, 1, "p", "master", "MERGED", "s", "u")
    with pytest.raises(dataclasses.FrozenInstanceError):
        change.status = "NEW"  # type: ignore[misc]
