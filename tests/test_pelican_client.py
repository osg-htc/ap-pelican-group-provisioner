"""PelicanClient against a scripted transport: authentication, retries, response parsing."""

from __future__ import annotations

import json

import httpx
import pytest

from provisioner import pelican_client
from provisioner.config import PelicanSettings
from provisioner.pelican_client import GroupMember, PelicanClient, PelicanError

ORIGIN = "https://origin.example.org"


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(pelican_client, "RETRY_BACKOFF_SECONDS", 0)


class Script:
    """Answers requests in order from a list of responses or exceptions to raise."""

    def __init__(self, *steps) -> None:
        self.steps = list(steps)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        step = self.steps.pop(0)
        if isinstance(step, type) and issubclass(step, Exception):
            raise step("scripted", request=request)
        if isinstance(step, int):
            return httpx.Response(step, json={})
        return step

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests]


def client(script: Script, **settings) -> PelicanClient:
    settings.setdefault("api_token", "jwt-123")
    return PelicanClient(PelicanSettings(origin_url=ORIGIN, **settings), transport=httpx.MockTransport(script))


# -- authentication ------------------------------------------------------------------

def test_api_token_is_sent_as_bearer_and_cookie():
    script = Script(httpx.Response(200, json=[]))
    client(script).list_users()

    (request,) = script.requests
    assert request.headers["Authorization"] == "Bearer jwt-123"
    assert "login=jwt-123" in request.headers["Cookie"]
    assert request.url == f"{ORIGIN}/api/v1.0/users"


def test_login_once_and_reuse_session_jwt():
    login = httpx.Response(200, json={}, headers={"Set-Cookie": "login=session-jwt; Path=/"})
    script = Script(login, httpx.Response(200, json=[]), httpx.Response(200, json=[]))
    c = client(script, api_token=None, admin_username="admin", admin_password="pw")

    c.list_users()
    c.list_groups()

    assert script.paths() == ["POST /api/v1.0/auth/login", "GET /api/v1.0/users", "GET /api/v1.0/groups"]
    assert json.loads(script.requests[0].content) == {"user": "admin", "password": "pw"}
    assert script.requests[1].headers["Authorization"] == "Bearer session-jwt"


def test_login_without_cookie_is_an_error():
    script = Script(httpx.Response(200, json={}))
    c = client(script, api_token=None, admin_username="admin", admin_password="pw")
    with pytest.raises(PelicanError, match="no login cookie"):
        c.list_users()


def test_login_refused_is_an_error_with_status():
    script = Script(httpx.Response(401, text="bad password"))
    c = client(script, api_token=None, admin_username="admin", admin_password="pw")
    with pytest.raises(PelicanError) as info:
        c.list_users()
    assert info.value.status == 401


# -- retries ---------------------------------------------------------------------------

def test_get_is_retried_after_5xx_and_timeout():
    script = Script(503, httpx.ReadTimeout, httpx.Response(200, json=[]))
    assert client(script).list_users() == []
    assert len(script.requests) == 3


def test_get_gives_up_after_three_attempts():
    script = Script(500, 502, 503)
    with pytest.raises(PelicanError) as info:
        client(script).list_users()
    assert info.value.status == 503
    assert len(script.requests) == 3


def test_4xx_is_never_retried():
    script = Script(403)
    with pytest.raises(PelicanError) as info:
        client(script).list_users()
    assert info.value.status == 403
    assert len(script.requests) == 1


@pytest.mark.parametrize("failure", [500, httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError])
def test_create_is_not_retried_when_the_server_may_have_acted(failure):
    """A retry after the Origin may already have created the object would create it twice."""
    script = Script(failure, httpx.Response(200, json={"id": "c1", "name": "x", "namespace": "/x"}))
    with pytest.raises(PelicanError):
        client(script).create_collection("x", "/x", "d", "private")
    assert len(script.requests) == 1


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout])
def test_create_is_retried_when_the_request_never_left(failure):
    script = Script(failure, httpx.Response(200, json={"id": "u1", "username": "alice"}))
    user = client(script).create_user("alice", None, "sub", "iss")
    assert user.id == "u1"
    assert len(script.requests) == 2


def test_delete_that_404s_on_retry_counts_as_done():
    script = Script(httpx.ReadTimeout, 404)
    client(script).delete_collection("c1")
    assert script.paths() == ["DELETE /api/v1.0/origin_ui/collections/c1"] * 2


def test_delete_that_404s_first_time_is_an_error():
    script = Script(404)
    with pytest.raises(PelicanError) as info:
        client(script).delete_group("g1")
    assert info.value.status == 404


# -- requests and parsing ----------------------------------------------------------------

def test_create_user_body_omits_empty_display_name():
    script = Script(httpx.Response(200, json={"id": 7, "username": "alice", "sub": "s", "issuer": "i"}))
    user = client(script).create_user("alice", None, "s", "i")

    assert json.loads(script.requests[0].content) == {"username": "alice", "sub": "s", "issuer": "i"}
    assert user.id == "7"          # numeric ids become strings


def test_groups_parse_members_and_skip_rows_without_user_id():
    body = [{"id": "g1", "name": "P", "description": None,
             "members": [{"userId": "u1", "source": "oidc"}, {"userId": "u2"}, {"source": "api"}]},
            {"id": "g2", "name": "Q", "members": None}]
    groups = client(Script(httpx.Response(200, json=body))).list_groups()

    assert groups[0].members == (GroupMember("u1", "oidc"), GroupMember("u2", ""))
    assert groups[0].description == ""
    assert groups[1].members == ()


def test_null_list_body_is_empty():
    assert client(Script(httpx.Response(200, content=b"null"))).list_collections() == []


def test_collection_metadata_values_are_strings():
    script = Script(httpx.Response(200, json={"id": "c1", "metadata": {"n": 3, "k": "v"}}))
    assert client(script).get_collection_metadata("c1") == {"n": "3", "k": "v"}


def test_grant_and_membership_requests():
    script = Script(200, 200, 200)
    c = client(script)
    c.grant_acl("c1", "group", "g1", "write")
    c.add_group_member("g1", "u1")
    c.remove_group_member("g1", "u1")

    assert script.paths() == ["POST /api/v1.0/origin_ui/collections/c1/acl",
                              "POST /api/v1.0/groups/g1/members",
                              "DELETE /api/v1.0/groups/g1/members/u1"]
    assert json.loads(script.requests[0].content) == {"subjectType": "group", "subjectId": "g1", "role": "write"}
