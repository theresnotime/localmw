"""A small Gerrit REST client, used to find out whether a change has been merged.

Anonymous access is enough for public changes on gerrit.wikimedia.org; supplying a username
and HTTP password (Gerrit > Settings > HTTP Credentials) switches to the authenticated ``/a``
endpoints so private/WIP changes resolve too.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

#: Gerrit prefixes JSON responses with this to defeat cross-site script inclusion.
MAGIC_PREFIX = ")]}'"

STATUS_MERGED = "MERGED"
STATUS_ABANDONED = "ABANDONED"

#: How many Change-Ids to pack into a single query.
QUERY_BATCH_SIZE = 20


class GerritError(RuntimeError):
    """Talking to Gerrit failed (network, auth, or an unexpected response)."""


@dataclass(frozen=True)
class Change:
    """The bits of Gerrit's ChangeInfo we care about."""

    change_id: str
    number: int | None
    project: str
    branch: str
    status: str
    subject: str
    url: str

    @property
    def is_merged(self) -> bool:
        return self.status == STATUS_MERGED

    @property
    def is_abandoned(self) -> bool:
        return self.status == STATUS_ABANDONED

    @property
    def is_open(self) -> bool:
        return not self.is_merged and not self.is_abandoned


def _parse_response(text: str) -> Any:
    body = text.lstrip()
    if body.startswith(MAGIC_PREFIX):
        body = body[len(MAGIC_PREFIX) :]
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise GerritError(f"unexpected response from Gerrit: {exc}") from None


def _quote_query_value(value: str) -> str:
    return value.replace("\\", "").replace('"', "")


class GerritClient:
    """Read-only client for the handful of queries localmw makes."""

    def __init__(
        self,
        base_url: str,
        username: str | None = None,
        password: str | None = None,
        timeout: int = 20,
        session: Any = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username or None
        self.password = password or None
        self.timeout = timeout
        self._session = session
        self._digest_fallback = False

    @property
    def authenticated(self) -> bool:
        return bool(self.username and self.password)

    # -- plumbing --------------------------------------------------------

    def _requests(self):
        # Imported lazily so commands that never touch Gerrit stay fast to start.
        try:
            import requests
        except ImportError:  # pragma: no cover - requests is a hard dependency
            raise GerritError("the 'requests' package is required for Gerrit access") from None
        return requests

    def _get_session(self):
        if self._session is None:
            self._session = self._requests().Session()
            self._session.headers.update({"Accept": "application/json"})
        return self._session

    def _auth(self):
        if not self.authenticated:
            return None
        requests = self._requests()
        if self._digest_fallback:
            return requests.auth.HTTPDigestAuth(self.username, self.password)
        return requests.auth.HTTPBasicAuth(self.username, self.password)

    def url_for(self, path: str) -> str:
        path = path.lstrip("/")
        prefix = "/a/" if self.authenticated else "/"
        return f"{self.base_url}{prefix}{path}"

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET a REST endpoint and return the decoded JSON."""
        requests = self._requests()
        session = self._get_session()

        for attempt in (1, 2):
            try:
                response = session.get(
                    self.url_for(path),
                    params=params,
                    auth=self._auth(),
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                raise GerritError(f"could not reach {self.base_url}: {exc}") from None

            if response.status_code == 401 and self.authenticated and attempt == 1 and not self._digest_fallback:
                # Older Gerrit deployments want digest auth for HTTP passwords.
                self._digest_fallback = True
                continue

            if response.status_code == 401:
                hint = (
                    "check gerrit.username and gerrit.http_password"
                    if self.authenticated
                    else "this change may need authentication; set gerrit.username and gerrit.http_password"
                )
                raise GerritError(f"Gerrit rejected the request (401 Unauthorized) - {hint}")
            if response.status_code == 403:
                raise GerritError("Gerrit denied access (403 Forbidden)")
            if response.status_code >= 400:
                raise GerritError(f"Gerrit returned HTTP {response.status_code} for /{path.lstrip('/')}")

            return _parse_response(response.text)

        raise GerritError("Gerrit authentication failed")  # pragma: no cover - loop always returns

    # -- queries ---------------------------------------------------------

    def version(self) -> str:
        """Server version; a cheap way to check connectivity and credentials."""
        value = self.get("config/server/version")
        return str(value)

    def _change_url(self, project: str, number: int | None) -> str:
        if number is None:
            return self.base_url
        if project:
            return f"{self.base_url}/c/{project}/+/{number}"
        return f"{self.base_url}/{number}"

    def _to_change(self, raw: dict[str, Any]) -> Change:
        project = str(raw.get("project", ""))
        number = raw.get("_number")
        return Change(
            change_id=str(raw.get("change_id", "")),
            number=int(number) if isinstance(number, int) else None,
            project=project,
            branch=str(raw.get("branch", "")),
            status=str(raw.get("status", "")).upper(),
            subject=str(raw.get("subject", "")),
            url=self._change_url(project, number if isinstance(number, int) else None),
        )

    def changes_by_change_id(
        self,
        change_ids: Iterable[str],
        project: str | None = None,
    ) -> dict[str, list[Change]]:
        """Look up changes by Change-Id, optionally scoped to one project.

        A Change-Id can legitimately match several changes (backports to release branches, or
        the same trailer reused across projects), so every match is returned.
        """
        wanted = [cid for cid in dict.fromkeys(change_ids) if cid]
        found: dict[str, list[Change]] = {cid: [] for cid in wanted}

        for batch in _chunks(wanted, QUERY_BATCH_SIZE):
            terms = " OR ".join(f"change:{_quote_query_value(cid)}" for cid in batch)
            query = f"({terms})"
            if project:
                query = f'project:"{_quote_query_value(project)}" AND {query}'

            raw = self.get("changes/", params={"q": query, "n": len(batch) * 5})
            if not isinstance(raw, list):  # pragma: no cover - defensive
                raise GerritError("expected a list of changes from Gerrit")

            for item in raw:
                if not isinstance(item, dict):
                    continue
                change = self._to_change(item)
                if change.change_id in found:
                    found[change.change_id].append(change)

        return found


def _chunks(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def client_from_config(config, timeout: int = 20) -> GerritClient:
    """Build a client from a :class:`localmw.config.Config`."""
    return GerritClient(
        base_url=config.gerrit_url,
        username=config.gerrit_username,
        password=config.gerrit_http_password,
        timeout=timeout,
    )
