from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from localmw.config import REDACTED, Config, ConfigError, config_dir, config_path


def test_config_dir_honours_the_override(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("LOCALMW_CONFIG_DIR", str(tmp_path / "custom"))
    assert config_dir() == tmp_path / "custom"
    assert config_path() == tmp_path / "custom" / "config.json"


def test_config_dir_honours_xdg(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config_dir() == tmp_path / "xdg" / "localmw"


def test_config_dir_defaults_to_dot_config(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert config_dir() == tmp_path / "home" / ".config" / "localmw"


def test_missing_file_yields_defaults(config_dir: Path):
    config = Config.load()
    assert config.warnings == []
    assert config.gerrit_url == "https://gerrit.wikimedia.org/r"
    assert config.pull_strategy == "ff-only"
    assert config.jobs == 2
    assert config.default_branches == ["master", "main"]
    assert config.mediawiki_dir is None
    assert config.gerrit_http_password is None
    assert config.review_branch_prefix == "review/"


def test_save_and_reload_roundtrip(config_dir: Path):
    config = Config.load()
    config.set_raw("mediawiki_dir", "~/git/mediawiki")
    config.set_raw("gerrit.username", "sammy")
    config.set_raw("jobs", "4")
    config.set_raw("pull.submodules", "yes")
    config.set_raw("default_branches", "master, main, production")
    path = config.save()

    assert json.loads(path.read_text()) == {
        "default_branches": ["master", "main", "production"],
        "gerrit": {"username": "sammy"},
        "jobs": 4,
        "mediawiki_dir": "~/git/mediawiki",
        "pull": {"submodules": True},
    }

    reloaded = Config.load()
    assert reloaded.jobs == 4
    assert reloaded.gerrit_username == "sammy"
    assert reloaded.pull_submodules is True
    assert reloaded.default_branches == ["master", "main", "production"]
    assert reloaded.mediawiki_dir == Path.home() / "git" / "mediawiki"


def test_saved_file_is_not_world_readable(config_dir: Path):
    config = Config.load()
    config.set_raw("gerrit.http_password", "hunter2")
    path = config.save()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_unknown_keys_are_reported_and_ignored(config_dir: Path):
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(json.dumps({"jobs": 6, "nonsense": True, "gerrit": {"nope": 1}}))

    config = Config.load()
    assert config.jobs == 6
    assert any("nonsense" in warning for warning in config.warnings)
    assert any("gerrit.nope" in warning for warning in config.warnings)


def test_invalid_json_falls_back_to_defaults(config_dir: Path):
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text("{not json")

    config = Config.load()
    assert config.jobs == 2
    assert any("not valid JSON" in warning for warning in config.warnings)


def test_non_object_json_falls_back_to_defaults(config_dir: Path):
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text("[1, 2, 3]")

    config = Config.load()
    assert any("JSON object" in warning for warning in config.warnings)


def test_environment_overrides_the_file(config_dir: Path, monkeypatch):
    config = Config.load()
    config.set_raw("jobs", "4")
    config.set_raw("gerrit.username", "from-file")
    config.save()

    monkeypatch.setenv("LOCALMW_JOBS", "16")
    monkeypatch.setenv("LOCALMW_GERRIT_HTTP_PASSWORD", "from-env")

    config = Config.load()
    assert config.jobs == 16
    assert config.gerrit_http_password == "from-env"
    assert config.source_of("jobs") == "env"
    assert config.source_of("gerrit.http_password") == "env"
    assert config.source_of("gerrit.username") == "file"
    assert config.source_of("pull.strategy") == "default"


def test_mediawiki_dir_environment_aliases(config_dir: Path, monkeypatch):
    monkeypatch.setenv("LOCALMW_MW_DIR", "/srv/mediawiki")
    assert Config.load().mediawiki_dir == Path("/srv/mediawiki")


def test_bad_environment_value_is_reported(config_dir: Path, monkeypatch):
    monkeypatch.setenv("LOCALMW_JOBS", "lots")
    with pytest.raises(ConfigError, match="LOCALMW_JOBS"):
        assert Config.load().jobs


def test_set_raw_parses_each_type(config_dir: Path):
    config = Config.load()
    assert config.set_raw("jobs", " 12 ") == 12
    assert config.set_raw("pull.submodules", "off") is False
    assert config.set_raw("exclude", "Wikibase, Flow,") == ["Wikibase", "Flow"]
    assert config.set_raw("gerrit.username", "  sammy ") == "sammy"
    assert config.set_raw("mediawiki_dir", "") is None


def test_set_raw_rejects_unknown_keys_and_bad_values(config_dir: Path):
    config = Config.load()
    with pytest.raises(ConfigError, match="unknown config key"):
        config.set_raw("nope", "1")
    with pytest.raises(ConfigError, match="integer"):
        config.set_raw("jobs", "many")
    with pytest.raises(ConfigError, match="boolean"):
        config.set_raw("pull.submodules", "maybe")
    with pytest.raises(ConfigError, match="pull.strategy"):
        config.set_raw("pull.strategy", "yolo")
    with pytest.raises(ConfigError, match="positive integer"):
        config.set_raw("jobs", "0")
    with pytest.raises(ConfigError, match="http"):
        config.set_raw("gerrit.url", "gerrit.example.org")


def test_validation_of_a_hand_edited_file(config_dir: Path):
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"jobs": -1, "pull": {"strategy": "sideways"}, "default_branches": []})
    )
    warnings = Config.load().warnings
    assert any("jobs" in warning for warning in warnings)
    assert any("pull.strategy" in warning for warning in warnings)
    assert any("default_branches" in warning for warning in warnings)


def test_as_dict_redacts_secrets(config_dir: Path):
    config = Config.load()
    config.set_raw("gerrit.http_password", "hunter2")
    assert config.as_dict()["gerrit.http_password"] == REDACTED
    assert config.as_dict(redact=False)["gerrit.http_password"] == "hunter2"
    assert config.as_dict()["gerrit.username"] is None


def test_unset(config_dir: Path):
    config = Config.load()
    config.set_raw("jobs", "3")
    assert config.unset("jobs") is True
    assert config.jobs == 2
    assert config.unset("jobs") is False
    with pytest.raises(ConfigError):
        config.unset("nope")


def test_gerrit_url_loses_a_trailing_slash(config_dir: Path):
    config = Config.load()
    config.set_raw("gerrit.url", "https://gerrit.example.org/r/")
    assert config.gerrit_url == "https://gerrit.example.org/r"
