"""Configuration file handling.

The config lives in ``~/.config/localmw/config.json`` by default. That directory can be
overridden with ``LOCALMW_CONFIG_DIR``, and (failing that) honours ``XDG_CONFIG_HOME``.

Every setting can also be supplied through the environment, which is handy for secrets you
do not want on disk: the env var name is the dotted key upper-cased with dots replaced by
underscores and prefixed with ``LOCALMW_`` (so ``gerrit.http_password`` becomes
``LOCALMW_GERRIT_HTTP_PASSWORD``). Environment values win over the file.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

APP_NAME = "localmw"
CONFIG_FILENAME = "config.json"
ENV_PREFIX = "LOCALMW_"
ENV_CONFIG_DIR = f"{ENV_PREFIX}CONFIG_DIR"

DEFAULT_GERRIT_URL = "https://gerrit.wikimedia.org/r"
PULL_STRATEGIES = ("ff-only", "rebase", "merge")

#: Extra environment variable aliases, purely for convenience.
ENV_ALIASES: dict[str, tuple[str, ...]] = {
    "mediawiki_dir": (f"{ENV_PREFIX}MW_DIR", f"{ENV_PREFIX}MW"),
}


class ConfigError(ValueError):
    """Raised when a config value is missing, malformed, or out of range."""


def _parse_str(raw: str) -> str:
    return raw.strip()


def _parse_optional_str(raw: str) -> str | None:
    value = raw.strip()
    return value or None


def _parse_bool(raw: str) -> bool:
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "y", "on"):
        return True
    if value in ("0", "false", "no", "n", "off"):
        return False
    raise ConfigError(f"expected a boolean (true/false), got {raw!r}")


def _parse_int(raw: str) -> int:
    try:
        return int(raw.strip())
    except ValueError:
        raise ConfigError(f"expected an integer, got {raw!r}") from None


def _parse_list(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Field:
    """One configurable setting, addressed by its dotted key."""

    key: str
    default: Any
    help: str
    parse: Callable[[str], Any]
    secret: bool = False

    @property
    def env_names(self) -> tuple[str, ...]:
        primary = ENV_PREFIX + self.key.upper().replace(".", "_")
        return (primary, *ENV_ALIASES.get(self.key, ()))


SCHEMA: tuple[Field, ...] = (
    Field(
        "mediawiki_dir",
        None,
        "Default MediaWiki install to operate on when the current directory is not one.",
        _parse_optional_str,
    ),
    Field(
        "gerrit.url",
        DEFAULT_GERRIT_URL,
        "Base URL of the Gerrit instance.",
        _parse_str,
    ),
    Field(
        "gerrit.username",
        None,
        "Gerrit username, required only for authenticated API calls.",
        _parse_optional_str,
    ),
    Field(
        "gerrit.http_password",
        None,
        "Gerrit HTTP password (Settings > HTTP Credentials). Optional; anonymous reads work for public changes.",
        _parse_optional_str,
        secret=True,
    ),
    Field(
        "pull.strategy",
        "ff-only",
        f"How to integrate upstream commits: one of {', '.join(PULL_STRATEGIES)}.",
        _parse_str,
    ),
    Field(
        "pull.submodules",
        False,
        "Run 'git submodule update --init --recursive' after a successful pull.",
        _parse_bool,
    ),
    Field(
        "jobs",
        2,
        "How many repositories to fetch/pull concurrently.",
        _parse_int,
    ),
    Field(
        "default_branches",
        ["master", "main"],
        "Branch names considered 'the default branch' when deciding whether to pull.",
        _parse_list,
    ),
    Field(
        "exclude",
        [],
        "Glob patterns of extension/skin names to always skip.",
        _parse_list,
    ),
    Field(
        "review_branch_prefix",
        "review/",
        "Prefix identifying git-review scratch branches, used by 'localmw cleanup'.",
        _parse_str,
    ),
)

FIELDS: dict[str, Field] = {field.key: field for field in SCHEMA}

REDACTED = "********"


def config_dir() -> Path:
    """Return the directory the config file lives in."""
    override = os.environ.get(ENV_CONFIG_DIR)
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / APP_NAME


def config_path() -> Path:
    """Return the full path of the config file (which may not exist yet)."""
    return config_dir() / CONFIG_FILENAME


def _nested_get(data: dict[str, Any], key: str) -> tuple[bool, Any]:
    node: Any = data
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _nested_set(data: dict[str, Any], key: str, value: Any) -> None:
    parts = key.split(".")
    node = data
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def _nested_unset(data: dict[str, Any], key: str) -> bool:
    parts = key.split(".")
    node = data
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            return False
        node = child
    return node.pop(parts[-1], _MISSING) is not _MISSING


_MISSING = object()


def _flatten(data: dict[str, Any], prefix: str = "") -> list[str]:
    keys: list[str] = []
    for name, value in data.items():
        dotted = f"{prefix}{name}"
        if isinstance(value, dict):
            keys.extend(_flatten(value, f"{dotted}."))
        else:
            keys.append(dotted)
    return keys


class Config:
    """A loaded config, with environment overrides applied on read."""

    def __init__(
        self,
        data: dict[str, Any] | None = None,
        path: Path | None = None,
        warnings: Sequence[str] = (),
    ) -> None:
        self._data: dict[str, Any] = data if data is not None else {}
        self.path = path if path is not None else config_path()
        self.warnings: list[str] = list(warnings)

    # -- loading / saving ------------------------------------------------

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        """Load the config file, tolerating (but reporting) anything odd about it."""
        path = path or config_path()
        warnings: list[str] = []
        data: dict[str, Any] = {}

        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                warnings.append(f"{path} is not valid JSON ({exc}); using defaults")
                raw = None
            except OSError as exc:
                warnings.append(f"could not read {path} ({exc}); using defaults")
                raw = None

            if raw is not None and not isinstance(raw, dict):
                warnings.append(f"{path} should contain a JSON object; using defaults")
            elif isinstance(raw, dict):
                data = raw
                for key in _flatten(raw):
                    if key not in FIELDS:
                        warnings.append(f"unknown config key {key!r} in {path} (ignored)")

        config = cls(data=data, path=path, warnings=warnings)
        config.warnings.extend(config.validate())
        return config

    def save(self) -> Path:
        """Write the config file, creating the directory and locking down permissions."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._data, indent=2, sort_keys=True) + "\n"
        self.path.write_text(payload, encoding="utf-8")
        with contextlib.suppress(OSError):  # pragma: no cover - e.g. exotic filesystems
            self.path.chmod(0o600)
        return self.path

    # -- reading ---------------------------------------------------------

    def get(self, key: str) -> Any:
        """Return a setting, preferring the environment over the file over the default."""
        field = FIELDS.get(key)
        if field is None:
            raise ConfigError(f"unknown config key {key!r}")

        for env_name in field.env_names:
            raw = os.environ.get(env_name)
            if raw is not None and raw != "":
                try:
                    return field.parse(raw)
                except ConfigError as exc:
                    raise ConfigError(f"{env_name}: {exc}") from None

        found, value = _nested_get(self._data, key)
        if found and value is not None:
            return value
        return field.default

    def source_of(self, key: str) -> str:
        """Where the effective value came from: ``env``, ``file``, or ``default``."""
        field = FIELDS[key]
        for env_name in field.env_names:
            if os.environ.get(env_name):
                return "env"
        found, value = _nested_get(self._data, key)
        if found and value is not None:
            return "file"
        return "default"

    def as_dict(self, redact: bool = True) -> dict[str, Any]:
        """Return every effective setting, keyed by dotted name."""
        out: dict[str, Any] = {}
        for field in SCHEMA:
            value = self.get(field.key)
            if redact and field.secret and value:
                value = REDACTED
            out[field.key] = value
        return out

    # -- writing ---------------------------------------------------------

    def set_raw(self, key: str, raw: str) -> Any:
        """Parse and store ``raw`` for ``key``, returning the stored value."""
        field = FIELDS.get(key)
        if field is None:
            raise ConfigError(f"unknown config key {key!r}")
        value = field.parse(raw)

        had_value, previous = _nested_get(self._data, key)
        _nested_set(self._data, key, value)
        errors = self.validate()
        if errors:
            # Leave the config as it was, so a rejected 'config set' changes nothing.
            if had_value:
                _nested_set(self._data, key, previous)
            else:
                _nested_unset(self._data, key)
            raise ConfigError(errors[0])
        return value

    def unset(self, key: str) -> bool:
        """Remove ``key`` from the file, reverting it to its default."""
        if key not in FIELDS:
            raise ConfigError(f"unknown config key {key!r}")
        return _nested_unset(self._data, key)

    # -- validation ------------------------------------------------------

    def validate(self) -> list[str]:
        """Return a list of human-readable problems with the current values."""
        problems: list[str] = []

        strategy = self.get("pull.strategy")
        if strategy not in PULL_STRATEGIES:
            problems.append(f"pull.strategy must be one of {', '.join(PULL_STRATEGIES)} (got {strategy!r})")

        jobs = self.get("jobs")
        if not isinstance(jobs, int) or isinstance(jobs, bool) or jobs < 1:
            problems.append(f"jobs must be a positive integer (got {jobs!r})")

        branches = self.get("default_branches")
        if not isinstance(branches, list) or not all(isinstance(b, str) for b in branches):
            problems.append("default_branches must be a list of branch names")
        elif not branches:
            problems.append("default_branches must not be empty")

        if not isinstance(self.get("exclude"), list):
            problems.append("exclude must be a list of glob patterns")

        url = self.get("gerrit.url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            problems.append(f"gerrit.url must be an http(s) URL (got {url!r})")

        return problems

    # -- typed accessors -------------------------------------------------

    @property
    def mediawiki_dir(self) -> Path | None:
        value = self.get("mediawiki_dir")
        return Path(value).expanduser() if value else None

    @property
    def gerrit_url(self) -> str:
        return str(self.get("gerrit.url")).rstrip("/")

    @property
    def gerrit_username(self) -> str | None:
        return self.get("gerrit.username")

    @property
    def gerrit_http_password(self) -> str | None:
        return self.get("gerrit.http_password")

    @property
    def pull_strategy(self) -> str:
        return str(self.get("pull.strategy"))

    @property
    def pull_submodules(self) -> bool:
        return bool(self.get("pull.submodules"))

    @property
    def jobs(self) -> int:
        return max(1, int(self.get("jobs")))

    @property
    def default_branches(self) -> list[str]:
        return list(self.get("default_branches"))

    @property
    def exclude(self) -> list[str]:
        return list(self.get("exclude"))

    @property
    def review_branch_prefix(self) -> str:
        return str(self.get("review_branch_prefix"))
