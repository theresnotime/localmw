from __future__ import annotations

from pathlib import Path

import fixtures
import pytest

from localmw.install import (
    ALL_KINDS,
    KIND_CORE,
    KIND_EXTENSION,
    KIND_SKIN,
    KIND_VENDOR,
    InstallError,
    Repo,
    discover,
    find_install,
    looks_like_install,
)


def test_looks_like_install(tmp_path: Path):
    assert not looks_like_install(tmp_path)
    root = fixtures.make_install(tmp_path / "mw")
    assert looks_like_install(root)


def test_looks_like_install_needs_a_strong_marker(tmp_path: Path):
    root = tmp_path / "mw"
    (root / "includes").mkdir(parents=True)
    (root / "maintenance").mkdir()
    assert not looks_like_install(root)

    (root / "mw-config").mkdir()
    assert looks_like_install(root)


def test_looks_like_install_rejects_a_plain_directory(tmp_path: Path):
    (tmp_path / "includes").mkdir()
    assert not looks_like_install(tmp_path)
    assert not looks_like_install(tmp_path / "nope")


def test_find_install_walks_up_from_a_subdirectory(install: Path):
    deep = install / "extensions" / "Echo"
    assert find_install(start=deep).resolve() == install.resolve()


def test_find_install_prefers_explicit_over_cwd(install: Path, tmp_path: Path, origins: Path):
    other = fixtures.build_install(tmp_path / "other", origins, extensions=(), skins=(), with_vendor=False)
    assert find_install(explicit=other, start=install).resolve() == other.resolve()


def test_find_install_falls_back_to_configured(tmp_path: Path, install: Path):
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    assert find_install(configured=install, start=elsewhere).resolve() == install.resolve()


def test_find_install_prefers_cwd_over_configured(tmp_path: Path, install: Path, origins: Path):
    configured = fixtures.build_install(tmp_path / "configured", origins, extensions=(), skins=(), with_vendor=False)
    assert find_install(configured=configured, start=install).resolve() == install.resolve()


def test_find_install_errors_when_nothing_matches(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(InstallError, match="not a MediaWiki install"):
        find_install(start=empty)


def test_find_install_rejects_a_bad_explicit_path(tmp_path: Path):
    with pytest.raises(InstallError, match="does not exist"):
        find_install(explicit=tmp_path / "missing")

    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(InstallError, match="does not look like"):
        find_install(explicit=plain)


def test_find_install_rejects_a_bad_configured_path(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(InstallError, match="configured mediawiki_dir"):
        find_install(configured=tmp_path / "missing", start=empty)


def test_discover_finds_every_kind(install: Path):
    discovery = discover(install)
    by_kind = {}
    for repo in discovery:
        by_kind.setdefault(repo.kind, []).append(repo.name)

    assert by_kind[KIND_CORE] == ["core"]
    assert by_kind[KIND_VENDOR] == ["vendor"]
    assert by_kind[KIND_EXTENSION] == ["AbuseFilter", "Echo"]
    assert by_kind[KIND_SKIN] == ["Vector"]
    assert len(discovery) == 5


def test_discover_orders_core_first(install: Path):
    labels = [repo.label for repo in discover(install)]
    assert labels == ["core", "vendor", "extensions/AbuseFilter", "extensions/Echo", "skins/Vector"]


def test_discover_filters_by_kind(install: Path):
    assert [r.label for r in discover(install, kinds=(KIND_SKIN,))] == ["skins/Vector"]
    assert [r.label for r in discover(install, kinds=(KIND_CORE, KIND_EXTENSION))] == [
        "core",
        "extensions/AbuseFilter",
        "extensions/Echo",
    ]


def test_discover_only_is_a_case_insensitive_glob(install: Path):
    assert [r.name for r in discover(install, only=["echo"])] == ["Echo"]
    assert [r.name for r in discover(install, only=["A*"])] == ["AbuseFilter"]
    assert [r.name for r in discover(install, only=["Echo", "Vector"])] == ["Echo", "Vector"]


def test_discover_exclude_wins_over_only(install: Path):
    repos = discover(install, only=["*"], exclude=["Echo", "vendor"])
    assert [r.name for r in repos] == ["core", "AbuseFilter", "Vector"]


def test_discover_reports_directories_without_git(install: Path):
    (install / "extensions" / "TarballExtension").mkdir()
    discovery = discover(install)
    assert discovery.non_git == ["extensions/TarballExtension"]
    assert "TarballExtension" not in [r.name for r in discovery]


def test_discover_skips_dotfiles_and_files(install: Path):
    (install / "extensions" / ".DS_Store").write_text("junk")
    (install / "extensions" / ".hidden").mkdir()
    discovery = discover(install)
    assert discovery.non_git == []


def test_discover_handles_core_not_being_a_checkout(tmp_path: Path, origins: Path):
    root = fixtures.make_install(tmp_path / "tarball")
    fixtures.make_repo_with_origin(root / "extensions" / "Echo", origins)
    discovery = discover(root)
    assert discovery.core_is_git is False
    assert [r.label for r in discovery] == ["extensions/Echo"]


def test_repo_matches(tmp_path: Path):
    repo = Repo(kind=KIND_EXTENSION, name="Wikibase", path=tmp_path / "extensions" / "Wikibase", root=tmp_path)
    assert repo.matches("wikibase")
    assert repo.matches("Wiki*")
    assert repo.matches("extension")
    assert repo.matches("extensions/Wikibase")
    assert not repo.matches("Echo")
    assert not repo.matches("")


def test_all_kinds_is_the_default(install: Path):
    assert len(discover(install, kinds=ALL_KINDS)) == len(discover(install))
    assert len(discover(install, kinds=())) == len(discover(install))
