"""fetch_project_names: the Topology feed's shape and failure handling."""

from __future__ import annotations

import httpx
import pytest

from provisioner import projects
from provisioner.projects import fetch_project_names
from provisioner.report import HardError

URL = "https://topology.example.org/miscproject/json"


@pytest.fixture
def serve(monkeypatch):
    """Serve one response for the feed URL; returns the list of URLs requested."""
    seen: list[str] = []

    def install(response: httpx.Response | Exception):
        def fake_get(url, timeout, follow_redirects):
            seen.append(url)
            if isinstance(response, Exception):
                raise response
            response.request = httpx.Request("GET", url)
            return response
        monkeypatch.setattr(projects.httpx, "get", fake_get)
        return seen
    return install


def test_names_and_display_names(serve):
    serve(httpx.Response(200, json={
        "ACE_NIAID": {"Name": "ACE_NIAID", "PIName": "x"},
        "Lab.v2": {"Name": "Lab v2"},
        "NoName": {"PIName": "y"},
        "Odd": "not a record",
    }))
    assert fetch_project_names(URL) == {"ACE_NIAID": "ACE_NIAID", "Lab.v2": "Lab v2", "NoName": "NoName", "Odd": "Odd"}


@pytest.mark.parametrize("body", [b"{}", b"[]", b'["ACE"]', b'"text"', b"null"])
def test_anything_but_a_non_empty_object_is_a_hard_error(serve, body):
    serve(httpx.Response(200, content=body))
    with pytest.raises(HardError, match="not a non-empty object"):
        fetch_project_names(URL)


def test_http_error_status_is_a_hard_error(serve):
    serve(httpx.Response(503))
    with pytest.raises(HardError, match="Could not fetch projects"):
        fetch_project_names(URL)


def test_invalid_json_is_a_hard_error(serve):
    serve(httpx.Response(200, content=b"<html>maintenance</html>"))
    with pytest.raises(HardError, match="Could not fetch projects"):
        fetch_project_names(URL)


def test_network_failure_is_a_hard_error(serve):
    serve(httpx.ConnectError("refused"))
    with pytest.raises(HardError, match="Could not fetch projects"):
        fetch_project_names(URL)
