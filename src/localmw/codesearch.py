"""A small client for Wikimedia Codesearch (https://codesearch.wmcloud.org), a set of Hound instances.

Each "backend" (``search``, ``core``, ``extensions``, ...) is its own Hound index with the usual
``/api/v1/search`` endpoint under ``{base_url}/{backend}/``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from . import __version__

DEFAULT_URL = "https://codesearch.wmcloud.org"
DEFAULT_BACKEND = "search"

#: The backends linked from the Codesearch front page.
BACKENDS: tuple[str, ...] = (
    "search",
    "core",
    "bundled",
    "deployed",
    "libraries",
    "operations",
    "puppet",
    "analytics",
    "apps",
    "devtools",
    "pywikibot",
    "things",
    "wmcs",
)

USER_AGENT = f"localmw/{__version__} (https://github.com/theresnotime/localmw)"

GITILES_URL = "https://gerrit.wikimedia.org/g"

#: Codesearch repository-name prefixes that map onto a MediaWiki install's layout.
_PREFIXES = {"Extension:": "extensions", "Skin:": "skins"}
CORE_REPO = "MediaWiki core"


class CodesearchError(RuntimeError):
    """Talking to Codesearch failed (network, a bad query, or an unexpected response)."""


@dataclass(frozen=True)
class LineMatch:
    line: str
    number: int
    before: list[str] = field(default_factory=list)
    after: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FileMatch:
    path: str
    lines: list[LineMatch]


@dataclass(frozen=True)
class RepoResult:
    repo: str
    revision: str
    files_with_match: int
    files: list[FileMatch]

    @property
    def match_count(self) -> int:
        return sum(len(file.lines) for file in self.files)

    @property
    def local_rel_path(self) -> str | None:
        """Where this repository lives in a MediaWiki install, e.g. ``extensions/Echo`` or ``.``."""
        if self.repo == CORE_REPO:
            return "."
        for prefix, directory in _PREFIXES.items():
            if self.repo.startswith(prefix):
                return f"{directory}/{self.repo[len(prefix) :]}"
        return None

    def file_url(self, path: str, line: int | None = None) -> str | None:
        """A Gitiles link for MediaWiki core, extensions and skins; None for anything else."""
        rel = self.local_rel_path
        if rel is None or not self.revision:
            return None
        project = "mediawiki/core" if rel == "." else f"mediawiki/{rel}"
        anchor = f"#{line}" if line else ""
        return f"{GITILES_URL}/{project}/+/{self.revision}/{path}{anchor}"


def _to_result(name: str, raw: dict[str, Any]) -> RepoResult:
    files = []
    for file in raw.get("Matches") or []:
        lines = [
            LineMatch(
                line=str(match.get("Line", "")),
                number=int(match.get("LineNumber", 0)),
                before=list(match.get("Before") or []),
                after=list(match.get("After") or []),
            )
            for match in file.get("Matches") or []
        ]
        files.append(FileMatch(path=str(file.get("Filename", "")), lines=lines))
    return RepoResult(
        repo=name,
        revision=str(raw.get("Revision", "")),
        files_with_match=int(raw.get("FilesWithMatch", len(files))),
        files=files,
    )


class CodesearchClient:
    """Read-only client for one Codesearch deployment."""

    def __init__(self, base_url: str = DEFAULT_URL, timeout: int = 30, session: Any = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = session

    def _get_session(self):
        if self._session is None:
            # Imported lazily so commands that never touch the network stay fast to start.
            import requests

            self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json", "User-Agent": USER_AGENT})
        return self._session

    @staticmethod
    def params(
        query: str,
        *,
        repos: str = "*",
        files: str = "",
        exclude_files: str = "",
        ignore_case: bool = False,
        context: int = 0,
        max_files: int | None = None,
    ) -> dict[str, str]:
        params = {
            "q": query,
            "repos": repos or "*",
            "files": files,
            "excludeFiles": exclude_files,
            # Hound's own spelling of true/false for this parameter.
            "i": "fosho" if ignore_case else "nope",
            "ctx": str(context),
        }
        if max_files is not None:
            params["rng"] = f":{max_files}"
        return params

    def ui_url(self, backend: str, params: dict[str, str]) -> str:
        """The same search in the Codesearch web UI."""
        ui_params = {key: params[key] for key in ("q", "files", "excludeFiles", "repos", "i") if key in params}
        return f"{self.base_url}/{backend}/?{urlencode(ui_params)}"

    def search(self, backend: str, params: dict[str, str]) -> list[RepoResult]:
        import requests

        url = f"{self.base_url}/{backend}/api/v1/search"
        try:
            response = self._get_session().get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise CodesearchError(f"could not reach {self.base_url}: {exc}") from None

        if response.status_code >= 400:
            raise CodesearchError(f"Codesearch returned HTTP {response.status_code} for /{backend}/")
        try:
            payload = response.json()
        except ValueError:
            raise CodesearchError(
                f"unexpected response from /{backend}/ (is '{backend}' a Codesearch backend?)"
            ) from None
        if not isinstance(payload, dict):
            raise CodesearchError(f"unexpected response from /{backend}/")
        if payload.get("Error"):
            raise CodesearchError(str(payload["Error"]))

        results = [_to_result(name, raw) for name, raw in (payload.get("Results") or {}).items()]
        results.sort(key=_sort_key)
        return results


def _sort_key(result: RepoResult) -> tuple[int, str]:
    # Core first, then the rest by name, matching the order the other commands list things in.
    return (0 if result.repo == CORE_REPO else 1, result.repo.lower())
