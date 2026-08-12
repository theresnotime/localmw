from __future__ import annotations

import json

import pytest
import requests

from localmw.gerrit import MAGIC_PREFIX, Change, GerritClient, GerritError


class FakeResponse:
    def __init__(self, status_code: int = 200, text: str = ""):
        self.status_code = status_code
        self.text = text


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}
        self.raises = None

    def get(self, url, params=None, auth=None, timeout=None):
        self.calls.append({"url": url, "params": params, "auth": auth, "timeout": timeout})
        if self.raises is not None:
            raise self.raises
        return self.responses.pop(0)


def body(payload) -> str:
    return MAGIC_PREFIX + "\n" + json.dumps(payload)


def change_payload(change_id: str, number: int, status: str = "MERGED", branch: str = "master") -> dict:
    return {
        "change_id": change_id,
        "_number": number,
        "project": "mediawiki/core",
        "branch": branch,
        "status": status,
        "subject": "Fix a thing",
    }


def test_strips_the_anti_xssi_prefix():
    session = FakeSession(FakeResponse(text=body({"hello": "world"})))
    client = GerritClient("https://gerrit.example.org/r", session=session)
    assert client.get("config/server/version") == {"hello": "world"}


def test_anonymous_requests_do_not_use_the_a_prefix():
    session = FakeSession(FakeResponse(text=body([])))
    client = GerritClient("https://gerrit.example.org/r/", session=session)
    client.get("changes/", params={"q": "status:open"})
    assert session.calls[0]["url"] == "https://gerrit.example.org/r/changes/"
    assert session.calls[0]["auth"] is None


def test_authenticated_requests_use_the_a_prefix_and_basic_auth():
    session = FakeSession(FakeResponse(text=body([])))
    client = GerritClient("https://gerrit.example.org/r", username="sammy", password="hunter2", session=session)
    assert client.authenticated is True
    client.get("changes/")
    assert session.calls[0]["url"] == "https://gerrit.example.org/r/a/changes/"
    assert isinstance(session.calls[0]["auth"], requests.auth.HTTPBasicAuth)


def test_falls_back_to_digest_auth_on_401():
    session = FakeSession(FakeResponse(401), FakeResponse(text=body({"ok": True})))
    client = GerritClient("https://gerrit.example.org/r", username="sammy", password="hunter2", session=session)
    assert client.get("changes/") == {"ok": True}
    assert isinstance(session.calls[0]["auth"], requests.auth.HTTPBasicAuth)
    assert isinstance(session.calls[1]["auth"], requests.auth.HTTPDigestAuth)


def test_persistent_401_is_reported_with_a_hint():
    session = FakeSession(FakeResponse(401), FakeResponse(401))
    client = GerritClient("https://gerrit.example.org/r", username="sammy", password="wrong", session=session)
    with pytest.raises(GerritError, match="gerrit.http_password"):
        client.get("changes/")


def test_anonymous_401_suggests_credentials():
    session = FakeSession(FakeResponse(401))
    client = GerritClient("https://gerrit.example.org/r", session=session)
    with pytest.raises(GerritError, match="set gerrit.username"):
        client.get("changes/")


def test_http_errors_are_reported():
    client = GerritClient("https://gerrit.example.org/r", session=FakeSession(FakeResponse(403)))
    with pytest.raises(GerritError, match="403"):
        client.get("changes/")

    client = GerritClient("https://gerrit.example.org/r", session=FakeSession(FakeResponse(500)))
    with pytest.raises(GerritError, match="HTTP 500"):
        client.get("changes/")


def test_network_failures_are_reported():
    session = FakeSession()
    session.raises = requests.ConnectionError("no route to host")
    client = GerritClient("https://gerrit.example.org/r", session=session)
    with pytest.raises(GerritError, match="could not reach"):
        client.get("changes/")


def test_garbage_responses_are_reported():
    client = GerritClient("https://gerrit.example.org/r", session=FakeSession(FakeResponse(text="<html>oops")))
    with pytest.raises(GerritError, match="unexpected response"):
        client.get("changes/")


def test_version():
    client = GerritClient("https://gerrit.example.org/r", session=FakeSession(FakeResponse(text=body("3.9.0"))))
    assert client.version() == "3.9.0"


def test_changes_by_change_id_builds_a_scoped_query():
    ids = ["I" + "1" * 40, "I" + "2" * 40]
    session = FakeSession(FakeResponse(text=body([change_payload(ids[0], 1104782)])))
    client = GerritClient("https://gerrit.example.org/r", session=session)

    found = client.changes_by_change_id(ids, project="mediawiki/core")

    query = session.calls[0]["params"]["q"]
    assert query == f'project:"mediawiki/core" AND (change:{ids[0]} OR change:{ids[1]})'

    assert len(found[ids[0]]) == 1
    assert found[ids[1]] == []

    change = found[ids[0]][0]
    assert change.number == 1104782
    assert change.is_merged
    assert change.url == "https://gerrit.example.org/r/c/mediawiki/core/+/1104782"


def test_changes_by_change_id_without_a_project():
    change_id = "I" + "3" * 40
    session = FakeSession(FakeResponse(text=body([change_payload(change_id, 1, status="NEW")])))
    client = GerritClient("https://gerrit.example.org/r", session=session)

    found = client.changes_by_change_id([change_id])
    assert session.calls[0]["params"]["q"] == f"(change:{change_id})"
    assert found[change_id][0].is_open


def test_changes_by_change_id_batches_large_queries():
    ids = [f"I{index:040x}" for index in range(45)]
    session = FakeSession(*[FakeResponse(text=body([])) for _ in range(3)])
    client = GerritClient("https://gerrit.example.org/r", session=session)

    found = client.changes_by_change_id(ids)
    assert len(session.calls) == 3
    assert len(found) == 45
    assert all(matches == [] for matches in found.values())


def test_changes_by_change_id_deduplicates_input():
    change_id = "I" + "4" * 40
    session = FakeSession(FakeResponse(text=body([])))
    client = GerritClient("https://gerrit.example.org/r", session=session)

    found = client.changes_by_change_id([change_id, change_id, ""])
    assert list(found) == [change_id]
    assert session.calls[0]["params"]["q"] == f"(change:{change_id})"


def test_query_values_cannot_break_out_of_their_quoting():
    session = FakeSession(FakeResponse(text=body([])))
    client = GerritClient("https://gerrit.example.org/r", session=session)
    client.changes_by_change_id(['I1"x'], project='evil"project')
    assert session.calls[0]["params"]["q"] == 'project:"evilproject" AND (change:I1x)'


def test_change_status_helpers():
    merged = Change("I1", 1, "p", "master", "MERGED", "s", "u")
    abandoned = Change("I2", 2, "p", "master", "ABANDONED", "s", "u")
    open_change = Change("I3", 3, "p", "master", "NEW", "s", "u")

    assert (merged.is_merged, merged.is_abandoned, merged.is_open) == (True, False, False)
    assert (abandoned.is_merged, abandoned.is_abandoned, abandoned.is_open) == (False, True, False)
    assert (open_change.is_merged, open_change.is_abandoned, open_change.is_open) == (False, False, True)


def test_unexpected_shape_is_rejected():
    client = GerritClient("https://gerrit.example.org/r", session=FakeSession(FakeResponse(text=body({}))))
    with pytest.raises(GerritError, match="list of changes"):
        client.changes_by_change_id(["I" + "5" * 40])
