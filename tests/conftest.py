from __future__ import annotations

import os
from pathlib import Path

import fixtures
import pytest


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    """Keep tests away from the developer's own git and localmw configuration."""
    for key, value in fixtures.GIT_ENV.items():
        monkeypatch.setenv(key, value)
    for key in list(os.environ):
        if key.startswith("LOCALMW_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


@pytest.fixture(scope="session")
def snapshots(tmp_path_factory) -> fixtures.Snapshots:
    """Layouts that are expensive to build with git and cheap to copy."""
    return fixtures.Snapshots(tmp_path_factory.mktemp("snapshots"))


@pytest.fixture
def origins(tmp_path: Path) -> Path:
    path = tmp_path / "origins"
    path.mkdir(exist_ok=True)
    return path


@pytest.fixture
def install(tmp_path: Path, snapshots: fixtures.Snapshots) -> Path:
    """A MediaWiki install with core, vendor, two extensions and one skin."""
    source = snapshots.get(
        "install",
        lambda base: fixtures.build_install(base / "mediawiki", base / "origins"),
    )
    fixtures.clone_snapshot(source, tmp_path)
    return tmp_path / "mediawiki"


@pytest.fixture
def echo_repo(tmp_path: Path, snapshots: fixtures.Snapshots) -> Path:
    """A single extension checkout, with a bare origin at ``origins/Echo``."""
    source = snapshots.get(
        "echo",
        lambda base: fixtures.make_repo_with_origin(
            base / "mediawiki" / "extensions" / "Echo",
            base / "origins",
            gitreview_project="mediawiki/extensions/Echo",
        ),
    )
    fixtures.clone_snapshot(source, tmp_path)
    return tmp_path / "mediawiki" / "extensions" / "Echo"


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "config"
    monkeypatch.setenv("LOCALMW_CONFIG_DIR", str(path))
    return path
