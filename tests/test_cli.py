from __future__ import annotations

import json
from pathlib import Path

import fixtures
import pytest
from click.testing import CliRunner

from localmw import gitutil
from localmw.cli import cli

MERGED_ID = "I" + "1" * 40
OPEN_ID = "I" + "2" * 40


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def invoke(runner: CliRunner, config_dir: Path):
    """Run the CLI with an isolated config directory."""

    def _invoke(*args: str, **kwargs):
        return runner.invoke(cli, list(args), catch_exceptions=False, **kwargs)

    return _invoke


def test_help_lists_the_commands(invoke):
    result = invoke("--help")
    assert result.exit_code == 0
    for command in ("status", "list", "pull", "switch", "cleanup", "config"):
        assert command in result.output


def test_status_json_reports_every_repository(invoke, install: Path):
    result = invoke("--mw", str(install), "status", "--no-fetch", "--json")
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert Path(payload["root"]) == install
    assert payload["fetched"] is False
    assert payload["summary"]["total"] == 5
    assert [repo["label"] for repo in payload["repositories"]] == [
        "core",
        "vendor",
        "extensions/AbuseFilter",
        "extensions/Echo",
        "skins/Vector",
    ]
    assert all(repo["branch"] == "master" for repo in payload["repositories"])
    assert all(repo["on_default_branch"] for repo in payload["repositories"])
    assert payload["summary"]["behind"] == 0
    assert payload["summary"]["dirty"] == 0


def test_status_notices_behind_dirty_and_off_branch(invoke, install: Path, origins: Path, tmp_path: Path):
    fixtures.advance_origin(origins / "Echo", tmp_path, count=2)
    fixtures.write(install / "skins" / "Vector", "README.md", "local edit\n")
    fixtures.git(install / "extensions" / "AbuseFilter", "checkout", "--quiet", "-b", "review/1104782")

    result = invoke("--mw", str(install), "status", "--json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)

    by_label = {repo["label"]: repo for repo in payload["repositories"]}
    assert by_label["extensions/Echo"]["behind"] == 2
    assert by_label["extensions/Echo"]["needs_update"] is True
    assert by_label["skins/Vector"]["dirty"] is True
    assert by_label["skins/Vector"]["unstaged"] == 1
    assert by_label["extensions/AbuseFilter"]["on_default_branch"] is False

    assert payload["summary"]["behind"] == 1
    assert payload["summary"]["dirty"] == 1
    assert payload["summary"]["off_default_branch"] == 1


def test_status_table_output(invoke, install: Path, origins: Path, tmp_path: Path):
    fixtures.advance_origin(origins / "Echo", tmp_path)
    result = invoke("--mw", str(install), "status")
    assert result.exit_code == 0
    assert "extensions/Echo" in result.output
    assert "1 behind" in result.output
    assert "run 'localmw pull' to update" in result.output


def test_status_verbose_logs_every_repository_to_stderr(invoke, install: Path, origins: Path, tmp_path: Path):
    fixtures.advance_origin(origins / "Echo", tmp_path, count=2)
    fixtures.write(install / "skins" / "Vector", "README.md", "local edit\n")

    result = invoke("--mw", str(install), "status", "--verbose")
    assert result.exit_code == 0

    log = result.stderr
    assert "Fetching 5 repositories" in log
    for label in ("core", "vendor", "extensions/AbuseFilter", "extensions/Echo", "skins/Vector"):
        assert label in log
    assert "2 behind" in log
    # A clean repo is flagged as fine, ones needing attention are not.
    assert "✓ vendor" in log
    assert "! extensions/Echo" in log
    assert "! skins/Vector" in log

    # The table still goes to stdout, so redirecting it keeps the log out of the way.
    assert "Repository" in result.stdout
    assert "Fetching 5 repositories" not in result.stdout


def test_verbose_is_accepted_before_the_subcommand(invoke, install: Path):
    before = invoke("--mw", str(install), "--verbose", "status", "--no-fetch")
    after = invoke("--mw", str(install), "status", "--no-fetch", "--verbose")
    assert before.exit_code == after.exit_code == 0
    assert "Reading 5 repositories" in before.stderr
    assert "Reading 5 repositories" in after.stderr


def test_verbose_keeps_json_parseable(invoke, install: Path):
    result = invoke("--mw", str(install), "status", "--no-fetch", "--json", "--verbose")
    assert result.exit_code == 0
    assert json.loads(result.stdout)["summary"]["total"] == 5
    assert "Reading 5 repositories" in result.stderr


def test_pull_verbose_logs_and_lists_everything(invoke, install: Path, origins: Path, tmp_path: Path):
    fixtures.advance_origin(origins / "Echo", tmp_path)

    result = invoke("--mw", str(install), "pull", "--verbose")
    assert result.exit_code == 0
    assert "Pulling 5 repositories" in result.stderr
    assert "✓ extensions/Echo" in result.stderr
    # -v also means "list every repository", so up-to-date rows appear in the table.
    assert "extensions/AbuseFilter" in result.stdout
    assert "up to date" in result.stdout


def test_cleanup_verbose_logs_each_repository(invoke, install: Path, monkeypatch):
    echo = install / "extensions" / "Echo"
    fixtures.git(echo, "checkout", "--quiet", "-b", "review/1104782")
    fixtures.commit_with_change_id(echo, MERGED_ID, "merged work", name="merged.txt")
    fixtures.git(echo, "checkout", "--quiet", "master")
    fixtures.install_fake_gerrit(monkeypatch, {MERGED_ID: "MERGED"})

    result = invoke("--mw", str(install), "cleanup", "--verbose", "--dry-run")
    assert result.exit_code == 0

    log = result.stderr
    assert "Scanning 5 repositories" in log
    assert "! extensions/Echo" in log
    assert "1 branch" in log
    assert "✓ skins/Vector" in log
    assert "no matching branches" in log
    assert "asking Gerrit about 1 change in mediawiki/extensions/Echo" in log


def test_quiet_and_verbose_both_stay_silent_about_progress(invoke, install: Path):
    result = invoke("--mw", str(install), "-q", "status", "--no-fetch")
    assert result.exit_code == 0
    assert "Reading" not in result.stderr
    assert str(install) not in result.stdout


def test_status_attention_only(invoke, install: Path, origins: Path, tmp_path: Path):
    fixtures.advance_origin(origins / "Echo", tmp_path)
    result = invoke("--mw", str(install), "status", "--attention")
    assert result.exit_code == 0
    assert "extensions/Echo" in result.output
    assert "extensions/AbuseFilter" not in result.output


def test_status_finds_the_install_from_the_current_directory(invoke, install: Path, monkeypatch):
    monkeypatch.chdir(install / "extensions" / "Echo")
    result = invoke("status", "--no-fetch", "--json")
    assert result.exit_code == 0
    assert Path(json.loads(result.stdout)["root"]) == install


def test_a_directory_that_is_not_an_install_is_an_error(invoke, tmp_path: Path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)

    result = invoke("status")
    assert result.exit_code != 0
    assert "not a MediaWiki install" in result.output


def test_list_reports_state_without_touching_the_network(invoke, install: Path, origins: Path, tmp_path: Path):
    fixtures.advance_origin(origins / "Echo", tmp_path, count=2)

    result = invoke("--mw", str(install), "list", "--json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["fetched"] is False
    assert payload["summary"]["total"] == 5
    # Nothing was fetched, so the new upstream commits are not known about yet.
    assert payload["summary"]["behind"] == 0

    assert json.loads(invoke("--mw", str(install), "status", "--json").stdout)["summary"]["behind"] == 1


def test_list_selects_by_kind(invoke, install: Path):
    result = invoke("--mw", str(install), "list", "--skins")
    assert result.exit_code == 0
    assert "skins/Vector" in result.output
    assert "extensions/Echo" not in result.output
    assert "1 repository" in result.output


def test_list_table_mentions_that_nothing_was_fetched(invoke, install: Path):
    result = invoke("--mw", str(install), "list")
    assert result.exit_code == 0
    assert "as of your last fetch" in result.output


def test_switch_moves_repositories_back_to_the_default_branch(invoke, install: Path):
    echo = install / "extensions" / "Echo"
    vector = install / "skins" / "Vector"
    fixtures.git(echo, "checkout", "--quiet", "-b", "review/1104782")
    fixtures.git(vector, "checkout", "--quiet", "-b", "review/1109334")

    result = invoke("--mw", str(install), "switch")
    assert result.exit_code == 0
    assert "2 switched" in result.output
    assert "3 already there" in result.output

    assert gitutil.read_state(echo).branch == "master"
    assert gitutil.read_state(vector).branch == "master"
    # The branches themselves are left in place.
    assert "review/1104782" in [b.name for b in gitutil.list_branches(echo)]


def test_switch_selects_one_repository(invoke, install: Path):
    echo = install / "extensions" / "Echo"
    vector = install / "skins" / "Vector"
    fixtures.git(echo, "checkout", "--quiet", "-b", "review/1104782")
    fixtures.git(vector, "checkout", "--quiet", "-b", "review/1109334")

    result = invoke("--mw", str(install), "switch", "-o", "echo")
    assert result.exit_code == 0
    assert gitutil.read_state(echo).branch == "master"
    assert gitutil.read_state(vector).branch == "review/1109334"


def test_switch_skips_uncommitted_work(invoke, install: Path):
    echo = install / "extensions" / "Echo"
    fixtures.git(echo, "checkout", "--quiet", "-b", "review/1104782")
    fixtures.write(echo, "README.md", "local edit\n")

    result = invoke("--mw", str(install), "switch")
    assert result.exit_code == 0
    assert "1 skipped" in result.output
    assert "uncommitted change" in result.output
    assert "--discard-changes" in result.output
    assert gitutil.read_state(echo).branch == "review/1104782"


def test_switch_asks_before_discarding_changes(invoke, install: Path):
    echo = install / "extensions" / "Echo"
    fixtures.git(echo, "checkout", "--quiet", "-b", "review/1104782")
    fixtures.write(echo, "README.md", "local edit\n")

    result = invoke("--mw", str(install), "switch", "--discard-changes", input="n\n")
    assert result.exit_code == 0
    assert "Discard 1 uncommitted change in 1 repository and switch?" in result.output
    assert "nothing was changed" in result.output
    assert gitutil.read_state(echo).branch == "review/1104782"
    assert (echo / "README.md").read_text() == "local edit\n"

    result = invoke("--mw", str(install), "switch", "--discard-changes", input="y\n")
    assert result.exit_code == 0
    assert "1 switched" in result.output
    assert gitutil.read_state(echo).branch == "master"
    assert (echo / "README.md").read_text() != "local edit\n"


def test_switch_dry_run_changes_nothing(invoke, install: Path):
    echo = install / "extensions" / "Echo"
    fixtures.git(echo, "checkout", "--quiet", "-b", "review/1104782")

    result = invoke("--mw", str(install), "switch", "--dry-run")
    assert result.exit_code == 0
    assert "would switch" in result.output
    assert "nothing was changed" in result.output
    assert gitutil.read_state(echo).branch == "review/1104782"


def test_switch_with_nothing_to_do(invoke, install: Path):
    result = invoke("--mw", str(install), "switch")
    assert result.exit_code == 0
    assert "already on its default branch" in result.output


def test_switch_verbose_logs_every_repository(invoke, install: Path):
    echo = install / "extensions" / "Echo"
    fixtures.git(echo, "checkout", "--quiet", "-b", "review/1104782")

    result = invoke("--mw", str(install), "switch", "--verbose")
    assert result.exit_code == 0
    assert "Reading 5 repositories" in result.stderr
    assert "Switching 1 repository" in result.stderr
    assert "review/1104782 → master" in result.stderr
    assert "✓ vendor" in result.stderr
    # -v also lists the repositories that had nothing to do.
    assert "already there" in result.stdout


def test_pull_updates_everything_that_is_safe(invoke, install: Path, origins: Path, tmp_path: Path):
    fixtures.advance_origin(origins / "Echo", tmp_path, count=2)
    fixtures.advance_origin(origins / "Vector", tmp_path)
    fixtures.write(install / "extensions" / "AbuseFilter", "README.md", "local edit\n")

    result = invoke("--mw", str(install), "pull")
    assert result.exit_code == 0
    assert "2 updated" in result.output
    assert "1 skipped" in result.output
    assert "uncommitted change" in result.output

    assert gitutil.read_state(install / "extensions" / "Echo").behind == 0
    assert gitutil.read_state(install / "skins" / "Vector").behind == 0


def test_pull_selects_by_kind(invoke, install: Path, origins: Path, tmp_path: Path):
    fixtures.advance_origin(origins / "Echo", tmp_path)
    fixtures.advance_origin(origins / "Vector", tmp_path)

    result = invoke("--mw", str(install), "pull", "--skins")
    assert result.exit_code == 0
    assert "skins/Vector" in result.output
    assert "extensions/Echo" not in result.output
    assert gitutil.read_state(install / "skins" / "Vector").behind == 0
    assert not (install / "extensions" / "Echo" / "upstream.txt").exists()


def test_pull_selects_by_name(invoke, install: Path, origins: Path, tmp_path: Path):
    fixtures.advance_origin(origins / "Echo", tmp_path)
    fixtures.advance_origin(origins / "AbuseFilter", tmp_path)

    result = invoke("--mw", str(install), "pull", "-o", "echo")
    assert result.exit_code == 0
    assert "1 repository" in result.output
    assert (install / "extensions" / "Echo" / "upstream.txt").exists()
    assert not (install / "extensions" / "AbuseFilter" / "upstream.txt").exists()


def test_pull_dry_run_changes_nothing(invoke, install: Path, origins: Path, tmp_path: Path):
    fixtures.advance_origin(origins / "Echo", tmp_path)

    result = invoke("--mw", str(install), "pull", "--dry-run")
    assert result.exit_code == 0
    assert "would update" in result.output
    assert "nothing was changed" in result.output
    assert not (install / "extensions" / "Echo" / "upstream.txt").exists()


def test_pull_interactive_asks_about_each_repository(invoke, install: Path, origins: Path, tmp_path: Path):
    fixtures.advance_origin(origins / "Echo", tmp_path, count=2)
    fixtures.advance_origin(origins / "Vector", tmp_path)

    # Prompted in discovery order: extensions/Echo, then skins/Vector.
    result = invoke("--mw", str(install), "pull", "--interactive", input="p\ns\n")
    assert result.exit_code == 0
    assert "2 repositories have upstream commits" in result.output
    assert "extensions/Echo" in result.output
    assert "2 commits behind" in result.output
    assert "[P]ull / [s]kip" in result.output

    assert "1 updated" in result.output
    assert "1 skipped" in result.output
    # The results table wraps its Detail column at 80 columns under the test console.
    assert "skipped at the prompt" in " ".join(result.output.split())

    assert (install / "extensions" / "Echo" / "upstream.txt").exists()
    assert not (install / "skins" / "Vector" / "upstream.txt").exists()
    assert gitutil.read_state(install / "skins" / "Vector").behind == 1


def test_pull_interactive_pulls_when_you_just_press_enter(invoke, install: Path, origins: Path, tmp_path: Path):
    fixtures.advance_origin(origins / "Echo", tmp_path)

    result = invoke("--mw", str(install), "pull", "-i", input="\n")
    assert result.exit_code == 0
    assert "1 updated" in result.output
    assert (install / "extensions" / "Echo" / "upstream.txt").exists()


def test_pull_interactive_can_be_abandoned(invoke, install: Path, origins: Path, tmp_path: Path):
    fixtures.advance_origin(origins / "Echo", tmp_path)

    # Closing stdin part way through is an abort, and nothing has been pulled by then.
    result = invoke("--mw", str(install), "pull", "-i", input="")
    assert result.exit_code == 0
    assert "nothing was pulled" in result.output
    assert not (install / "extensions" / "Echo" / "upstream.txt").exists()


def test_pull_interactive_does_not_ask_when_there_is_nothing_to_pull(invoke, install: Path):
    result = invoke("--mw", str(install), "pull", "--interactive")
    assert result.exit_code == 0
    assert "upstream commits" not in result.output
    assert "5 up to date" in result.output


def test_pull_interactive_still_reports_what_it_would_not_touch(invoke, install: Path, origins: Path, tmp_path: Path):
    fixtures.advance_origin(origins / "Echo", tmp_path)
    fixtures.write(install / "skins" / "Vector", "README.md", "local edit\n")

    result = invoke("--mw", str(install), "pull", "-i", input="s\n")
    assert result.exit_code == 0
    # Vector is dirty, so it was never a candidate and is not asked about.
    assert "1 repository has upstream commits" in result.output
    assert "2 skipped" in result.output
    assert "uncommitted change" in result.output


def test_pull_interactive_and_dry_run_are_mutually_exclusive(invoke, install: Path):
    result = invoke("--mw", str(install), "pull", "-i", "-n")
    assert result.exit_code != 0
    assert "--interactive cannot be combined with --dry-run" in result.output


def test_pull_exits_non_zero_on_failure(invoke, install: Path):
    fixtures.git(install / "extensions" / "Echo", "remote", "set-url", "origin", str(install / "gone"))
    result = invoke("--mw", str(install), "pull")
    assert result.exit_code == 1
    assert "failed" in result.output


def test_pull_rejects_an_unknown_selection(invoke, install: Path):
    result = invoke("--mw", str(install), "pull", "-o", "NoSuchExtension")
    assert result.exit_code != 0
    assert "no matching git repositories" in result.output


def test_cleanup_deletes_merged_review_branches(invoke, install: Path, monkeypatch):
    echo = install / "extensions" / "Echo"
    fixtures.git(echo, "checkout", "--quiet", "-b", "review/1104782")
    fixtures.commit_with_change_id(echo, MERGED_ID, "merged work", name="merged.txt")
    fixtures.git(echo, "checkout", "--quiet", "master")

    vector = install / "skins" / "Vector"
    fixtures.git(vector, "checkout", "--quiet", "-b", "review/1109334")
    fixtures.commit_with_change_id(vector, OPEN_ID, "open work", name="open.txt")
    fixtures.git(vector, "checkout", "--quiet", "master")

    fixtures.install_fake_gerrit(monkeypatch, {MERGED_ID: "MERGED", OPEN_ID: "NEW"})

    result = invoke("--mw", str(install), "cleanup", "--yes")
    assert result.exit_code == 0
    assert "1 branch deleted" in result.output

    assert "review/1104782" not in [b.name for b in gitutil.list_branches(echo)]
    assert "review/1109334" in [b.name for b in gitutil.list_branches(vector)]


def test_cleanup_asks_before_deleting(invoke, install: Path, monkeypatch):
    echo = install / "extensions" / "Echo"
    fixtures.git(echo, "checkout", "--quiet", "-b", "review/1104782")
    fixtures.commit_with_change_id(echo, MERGED_ID, "merged work", name="merged.txt")
    fixtures.git(echo, "checkout", "--quiet", "master")
    fixtures.install_fake_gerrit(monkeypatch, {MERGED_ID: "MERGED"})

    result = invoke("--mw", str(install), "cleanup", input="n\n")
    assert result.exit_code == 0
    assert "Delete 1 branch?" in result.output
    assert "nothing was deleted" in result.output
    assert "review/1104782" in [b.name for b in gitutil.list_branches(echo)]


def test_cleanup_dry_run(invoke, install: Path, monkeypatch):
    echo = install / "extensions" / "Echo"
    fixtures.git(echo, "checkout", "--quiet", "-b", "review/1104782")
    fixtures.commit_with_change_id(echo, MERGED_ID, "merged work", name="merged.txt")
    fixtures.git(echo, "checkout", "--quiet", "master")
    fixtures.install_fake_gerrit(monkeypatch, {MERGED_ID: "MERGED"})

    result = invoke("--mw", str(install), "cleanup", "--dry-run")
    assert result.exit_code == 0
    assert "1 branch would be deleted" in result.output
    assert "review/1104782" in [b.name for b in gitutil.list_branches(echo)]


def test_cleanup_with_nothing_to_do(invoke, install: Path, monkeypatch):
    fixtures.install_fake_gerrit(monkeypatch, {})
    result = invoke("--mw", str(install), "cleanup")
    assert result.exit_code == 0
    assert "No review/ branches to clean up." in result.output


def test_cleanup_offline_only_removes_locally_merged_branches(invoke, install: Path):
    echo = install / "extensions" / "Echo"
    fixtures.git(echo, "branch", "review/already-merged")
    fixtures.git(echo, "checkout", "--quiet", "-b", "review/not-merged")
    fixtures.commit_with_change_id(echo, MERGED_ID, "work", name="work.txt")
    fixtures.git(echo, "checkout", "--quiet", "master")

    result = invoke("--mw", str(install), "cleanup", "--no-gerrit", "--yes")
    assert result.exit_code == 0

    branches = [b.name for b in gitutil.list_branches(echo)]
    assert "review/already-merged" not in branches
    assert "review/not-merged" in branches


def test_config_init_show_set_get_unset(invoke, config_dir: Path, install: Path):
    result = invoke("config", "path")
    assert result.exit_code == 0
    assert str(config_dir / "config.json") in result.output
    assert "does not exist yet" in result.output

    result = invoke("config", "init")
    assert result.exit_code == 0
    assert (config_dir / "config.json").exists()

    result = invoke("config", "init")
    assert result.exit_code != 0
    assert "already exists" in result.output

    result = invoke("config", "set", "mediawiki_dir", str(install))
    assert result.exit_code == 0

    result = invoke("config", "get", "mediawiki_dir")
    assert result.exit_code == 0
    assert result.output.strip() == str(install)

    result = invoke("config", "show")
    assert result.exit_code == 0
    assert "gerrit.url" in result.output
    assert "https://gerrit.wikimedia.org/r" in result.output

    result = invoke("config", "unset", "mediawiki_dir")
    assert result.exit_code == 0
    assert invoke("config", "get", "mediawiki_dir").output.strip() == ""


def test_config_set_hides_and_redacts_a_secret(invoke, config_dir: Path):
    result = invoke("config", "set", "gerrit.http_password", input="hunter2\n")
    assert result.exit_code == 0
    assert "hunter2" not in result.output
    assert "********" in result.output

    assert json.loads((config_dir / "config.json").read_text())["gerrit"]["http_password"] == "hunter2"

    result = invoke("config", "show")
    assert "hunter2" not in result.output
    assert "********" in result.output

    result = invoke("config", "show", "--reveal")
    assert "hunter2" in result.output


def test_config_set_rejects_bad_input(invoke):
    result = invoke("config", "set", "nonsense", "1")
    assert result.exit_code != 0
    assert "unknown config key" in result.output

    result = invoke("config", "set", "jobs", "many")
    assert result.exit_code != 0
    assert "integer" in result.output


def test_config_keys_documents_every_setting(invoke):
    result = invoke("config", "keys")
    assert result.exit_code == 0
    for key in ("mediawiki_dir", "gerrit.http_password", "pull.strategy", "review_branch_prefix"):
        assert key in result.output


def test_config_check_reports_a_healthy_setup(invoke, install: Path, monkeypatch):
    fixtures.install_fake_gerrit(monkeypatch, {})
    invoke("config", "set", "mediawiki_dir", str(install))

    result = invoke("config", "check")
    assert result.exit_code == 0
    assert "config is valid" in result.output
    assert "Gerrit" in result.output


def test_config_check_reports_problems(invoke, tmp_path: Path, monkeypatch):
    fixtures.install_fake_gerrit(monkeypatch, {}, error="could not reach gerrit")
    invoke("config", "set", "mediawiki_dir", str(tmp_path))

    result = invoke("config", "check")
    assert result.exit_code == 1
    assert "not a MediaWiki install" in result.output
    assert "could not reach gerrit" in result.output


def test_a_configured_default_install_is_used(invoke, install: Path, tmp_path: Path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    invoke("config", "set", "mediawiki_dir", str(install))
    result = invoke("status", "--no-fetch", "--json")
    assert result.exit_code == 0
    assert Path(json.loads(result.stdout)["root"]) == install


def test_environment_can_supply_the_install(invoke, install: Path, tmp_path: Path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("LOCALMW_MEDIAWIKI_DIR", str(install))

    result = invoke("status", "--no-fetch", "--json")
    assert result.exit_code == 0
    assert Path(json.loads(result.stdout)["root"]) == install


def test_a_malformed_environment_override_is_a_clean_error(runner, config_dir: Path, install: Path, monkeypatch):
    monkeypatch.setenv("LOCALMW_JOBS", "lots")
    result = runner.invoke(cli, ["--mw", str(install), "status", "--no-fetch"])
    assert result.exit_code != 0
    assert "LOCALMW_JOBS" in result.output
    # A clean 'Error:' line, not a traceback.
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_a_hand_broken_config_is_warned_about_but_still_runs(invoke, config_dir: Path, install: Path):
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text('{"jobs": 0, "typo": true}')

    result = invoke("--mw", str(install), "status", "--no-fetch", "--json")
    assert result.exit_code == 0
    assert "warning:" in result.output
    assert "typo" in result.output
