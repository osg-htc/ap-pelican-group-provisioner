"""Thin typed wrapper over the Pelican Origin web API (base path /api/v1.0)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from provisioner.config import PelicanSettings

log = logging.getLogger(__name__)

API_PREFIX = "/api/v1.0"
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 0.5
IDEMPOTENT_METHODS = frozenset({"GET", "DELETE"})
# Transport errors raised before any byte of the request reached the server.
NOT_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


class PelicanError(Exception):
    """A non-retryable or exhausted-retry failure talking to the Origin."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class PelicanUser:
    id: str
    username: str
    sub: str | None = None
    issuer: str | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> PelicanUser:
        return cls(id=str(data["id"]), username=data["username"], sub=data.get("sub"), issuer=data.get("issuer"))


@dataclass(frozen=True)
class PelicanCollection:
    id: str
    name: str
    namespace: str
    description: str = ""

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> PelicanCollection:
        return cls(
            id=str(data["id"]),
            name=data.get("name", ""),
            namespace=data.get("namespace", ""),
            description=data.get("description") or "",
        )


@dataclass(frozen=True)
class GroupMember:
    user_id: str
    source: str   # "oidc" when Pelican asserted it from a login claim, otherwise added via the API


@dataclass(frozen=True)
class PelicanGroup:
    id: str
    name: str
    description: str = ""
    members: tuple[GroupMember, ...] = ()
    # Who besides system admins can change the membership: the owner, and the group admin
    # (a user, or every member of another group, by admin_type).
    owner_id: str = ""
    admin_id: str = ""
    admin_type: str = ""
    source: str = ""          # "pelican" when made through the API, else the provider that asserts it

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> PelicanGroup:
        members = tuple(
            GroupMember(user_id=str(m["userId"]), source=m.get("source") or "")
            for m in data.get("members") or [] if m.get("userId")
        )
        return cls(id=str(data["id"]), name=data.get("name", ""), description=data.get("description") or "",
                   members=members, owner_id=str(data.get("ownerId") or ""), admin_id=str(data.get("adminId") or ""),
                   admin_type=data.get("adminType") or "", source=data.get("source") or "")


@dataclass(frozen=True)
class CollectionAcl:
    subject_type: str
    subject_id: str
    role: str

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> CollectionAcl:
        return cls(
            subject_type=data.get("subjectType", ""),
            subject_id=str(data.get("subjectId", "")),
            role=data.get("role", ""),
        )


class PelicanClient:
    """Client for the Origin web API.

    Authentication (see design/user-collections.md):

    * ``PELICAN_API_TOKEN`` set: used as ``Authorization: Bearer``. It must be a JWT
      minted by this Origin. The ``<id>.<secret>`` API keys from the Tokens page are
      not accepted by the users and collections routes.
    * otherwise ``PELICAN_ADMIN_USERNAME`` and ``PELICAN_ADMIN_PASSWORD``: the client
      calls ``POST /auth/login`` once and reuses the session JWT it receives, sending it
      as both the bearer token and the ``login`` cookie. The Origin accepts either on
      every route this script uses.
    """

    def __init__(self, settings: PelicanSettings, transport: httpx.BaseTransport | None = None) -> None:
        """`transport` replaces the network, for tests."""
        self._settings = settings
        if not settings.verify_tls:
            log.warning("PELICAN_VERIFY_TLS=false: not verifying the Origin's TLS certificate")
        self._client = httpx.Client(
            base_url=settings.origin_url + API_PREFIX,
            headers={"Accept": "application/json"},
            verify=False if not settings.verify_tls else (settings.ca_bundle or True),
            timeout=settings.timeout_seconds,
            transport=transport,
        )
        self._authenticated = False

    def close(self) -> None:
        self._client.close()

    # -- authentication ------------------------------------------------------------

    def authenticate(self) -> None:
        """Establish credentials. Runs once, before the first API request."""
        if self._authenticated:
            return
        s = self._settings
        if s.bearer_token is not None:
            jwt = s.bearer_token
            log.info("Authenticating to %s with PELICAN_API_TOKEN", s.origin_url)
        else:
            assert s.admin_username and s.admin_password is not None  # enforced by PelicanSettings
            jwt = self._login(s.admin_username, s.admin_password.get_secret_value())
            log.info("Logged in to %s as %s", s.origin_url, s.admin_username)
        self._client.headers["Authorization"] = f"Bearer {jwt}"
        self._client.cookies.set("login", jwt)  # some handlers read the session cookie instead
        self._authenticated = True

    def _login(self, username: str, password: str) -> str:
        try:
            response = self._client.post("/auth/login", json={"user": username, "password": password})
        except httpx.TransportError as exc:
            raise PelicanError(f"POST /auth/login failed: {exc}") from exc
        if response.status_code != 200:
            raise _http_error("POST", "/auth/login", response)
        jwt = response.cookies.get("login") or self._client.cookies.get("login")
        if not jwt:
            raise PelicanError("POST /auth/login succeeded but returned no login cookie")
        return jwt

    # -- transport -----------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Send one request, retrying with backoff where that cannot repeat a change.

        GET and DELETE are retried on transport errors and 5xx responses. A POST is retried
        only when the request never reached the server (connection failed); after a timeout
        or a 5xx the Origin may already have created the object, and a retry would create it
        twice. A DELETE that returns 404 on a retry counts as done, since the earlier attempt
        removed it. 4xx responses are never retried.
        """
        self.authenticate()
        idempotent = method in IDEMPOTENT_METHODS
        last_error: PelicanError | None = None
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                response = self._client.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                last_error = PelicanError(f"{method} {path} failed: {exc}")
                if not idempotent and not isinstance(exc, NOT_SENT_ERRORS):
                    raise last_error from exc
            else:
                if response.status_code < 400:
                    return response
                if method == "DELETE" and response.status_code == 404 and attempt > 1:
                    log.info("%s %s returned 404 on retry; the earlier attempt deleted it", method, path)
                    return response
                if response.status_code < 500 or not idempotent:
                    raise _http_error(method, path, response)
                last_error = _http_error(method, path, response)
            log.warning("%s (attempt %d of %d)", last_error, attempt, RETRY_ATTEMPTS)
            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SECONDS * 2 ** (attempt - 1))
        assert last_error is not None
        raise last_error

    # -- users ---------------------------------------------------------------------

    def get_me(self) -> PelicanUser:
        """The account this client is authenticated as."""
        return PelicanUser.from_api(self._request("GET", "/me").json())

    def list_users(self) -> list[PelicanUser]:
        response = self._request("GET", "/users")
        return [PelicanUser.from_api(item) for item in response.json() or []]

    def create_user(self, username: str, display_name: str | None, sub: str, issuer: str) -> PelicanUser:
        body: dict[str, Any] = {"username": username, "sub": sub, "issuer": issuer}
        if display_name:
            body["displayName"] = display_name
        response = self._request("POST", "/users", json=body)
        return PelicanUser.from_api(response.json())

    # -- collections ---------------------------------------------------------------

    def list_collections(self) -> list[PelicanCollection]:
        response = self._request("GET", "/origin_ui/collections")
        return [PelicanCollection.from_api(item) for item in response.json() or []]

    def create_collection(
        self,
        name: str,
        namespace: str,
        description: str,
        visibility: str,
        metadata: dict[str, str] | None = None,
    ) -> PelicanCollection:
        body: dict[str, Any] = {
            "name": name,
            "namespace": namespace,
            "description": description,
            "visibility": visibility,
        }
        if metadata:
            body["metadata"] = metadata
        response = self._request("POST", "/origin_ui/collections", json=body)
        return PelicanCollection.from_api(response.json())

    def delete_collection(self, collection_id: str) -> None:
        self._request("DELETE", f"/origin_ui/collections/{collection_id}")

    def get_collection_metadata(self, collection_id: str) -> dict[str, str]:
        """The key/value metadata of one collection. The list endpoint omits it."""
        response = self._request("GET", f"/origin_ui/collections/{collection_id}")
        metadata = response.json().get("metadata") or {}
        return {str(k): str(v) for k, v in metadata.items()}

    def list_acls(self, collection_id: str) -> list[CollectionAcl]:
        response = self._request("GET", f"/origin_ui/collections/{collection_id}/acl")
        return [CollectionAcl.from_api(item) for item in response.json() or []]

    def grant_acl(self, collection_id: str, subject_type: str, subject_id: str, role: str) -> None:
        """Grant role to a user or group, addressed by its immutable ID."""
        body = {"subjectType": subject_type, "subjectId": subject_id, "role": role}
        self._request("POST", f"/origin_ui/collections/{collection_id}/acl", json=body)

    # -- groups --------------------------------------------------------------------

    def list_groups(self) -> list[PelicanGroup]:
        """Every group with its members embedded, so membership needs no further calls."""
        response = self._request("GET", "/groups")
        return [PelicanGroup.from_api(item) for item in response.json() or []]

    def create_group(self, name: str, display_name: str, description: str) -> PelicanGroup:
        body = {"name": name, "displayName": display_name, "description": description}
        response = self._request("POST", "/groups", json=body)
        return PelicanGroup.from_api(response.json())

    def delete_group(self, group_id: str) -> None:
        self._request("DELETE", f"/groups/{group_id}")

    def add_group_member(self, group_id: str, user_id: str) -> None:
        self._request("POST", f"/groups/{group_id}/members", json={"userId": user_id})

    def remove_group_member(self, group_id: str, user_id: str) -> None:
        self._request("DELETE", f"/groups/{group_id}/members/{user_id}")


def _http_error(method: str, path: str, response: httpx.Response) -> PelicanError:
    return PelicanError(f"{method} {path} returned {response.status_code}: {response.text}", status=response.status_code)
