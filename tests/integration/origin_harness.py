"""Fixtures and helpers for tests that run the provisioner against a real Pelican Origin.

LDAP and the Topology project feed are faked; everything else (users, groups, collections,
ACLs) is real API traffic to the Origin started by scripts/test-origin.sh. Every test is
skipped when no Origin answers, so a plain `uv run pytest` still works without Docker.

This is not a conftest.py: tests/conftest.py is imported as the module `conftest` by the
unit tests, and a second conftest.py would shadow it. Test modules import the fixtures
they use from here.

Each test gets its own Scenario with a random id woven into every name it creates (base
path, LDAP group, project names, usernames). Tests therefore do not see each other's
objects, and the Origin does not need to be reset between them.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field

import httpx
import pytest

from provisioner import jobs as jobs_module
from provisioner.config import Job, LdapSettings, PelicanSettings, RunSettings, Settings
from provisioner.jobs import JobRunner
from provisioner.ldap_client import LdapPerson
from provisioner.pelican_client import PelicanClient, PelicanError
from provisioner.report import SyncReport

ORIGIN_URL = os.environ.get("PELICAN_TEST_ORIGIN_URL", "https://localhost:18444").rstrip("/")
CONTAINER = os.environ.get("PELICAN_TEST_CONTAINER", "provisioner-test-origin")
NAMESPACE = "/test"
DESCRIPTION = "AUTOMATED: Do not manually edit this group."


def _origin_up() -> bool:
    try:
        httpx.get(f"{ORIGIN_URL}/.well-known/openid-configuration", verify=False, timeout=3).raise_for_status()
    except httpx.HTTPError:
        return False
    return True


def _mint_admin_token() -> str:
    """An admin JWT signed by the Origin's own issuer key, made inside the container.

    Set PELICAN_TEST_TOKEN to use a token minted some other way (e.g. a non-Docker Origin)."""
    if token := os.environ.get("PELICAN_TEST_TOKEN", "").strip():
        return token
    result = subprocess.run(
        ["docker", "exec", CONTAINER, "pelican-server", "token", "create",
         "--issuer", ORIGIN_URL, "--subject", "admin",
         "--raw-scope", "server.admin web_ui.access", "--lifetime", "86400",
         f"pelican://{ORIGIN_URL.removeprefix('https://')}{NAMESPACE}"],
        capture_output=True, text=True, check=False,
    )
    token = result.stdout.strip()
    if result.returncode != 0 or not token:
        pytest.fail(f"could not mint an admin token in container {CONTAINER}: {result.stderr.strip()[-500:]}")
    return token


@pytest.fixture(scope="session")
def admin_token() -> str:
    if not _origin_up():
        message = f"no Pelican Origin at {ORIGIN_URL}; start one with scripts/test-origin.sh"
        if os.environ.get("PELICAN_TEST_REQUIRE_ORIGIN"):
            pytest.fail(message)   # CI: a missing Origin must not pass as a run of skips
        pytest.skip(message)
    return _mint_admin_token()


@pytest.fixture(scope="session")
def pelican_settings(admin_token: str) -> PelicanSettings:
    return PelicanSettings(origin_url=ORIGIN_URL, api_token=admin_token, verify_tls=False,
                           collection_description=DESCRIPTION)


@pytest.fixture(scope="session")
def admin(pelican_settings: PelicanSettings) -> Iterator[PelicanClient]:
    """A client for arranging and inspecting Origin state, separate from the one under test."""
    client = PelicanClient(pelican_settings)
    yield client
    client.close()


class SimulatedCrash(BaseException):
    """The process dying mid-run. BaseException so no `except PelicanError` swallows it."""


class CrashingClient:
    """Wraps a PelicanClient and dies just before its Nth write (0-based)."""

    WRITES = {"create_user", "create_collection", "delete_collection", "grant_acl",
              "create_group", "delete_group", "add_group_member", "remove_group_member"}

    def __init__(self, inner: PelicanClient, crash_before: int | None) -> None:
        self._inner = inner
        self._crash_before = crash_before
        self.writes: list[tuple] = []

    def __getattr__(self, name: str):
        attr = getattr(self._inner, name)
        if name not in self.WRITES:
            return attr

        def write(*args, **kwargs):
            if self._crash_before is not None and len(self.writes) == self._crash_before:
                raise SimulatedCrash(f"crash before write {len(self.writes)}: {name}{args}")
            self.writes.append((name, *args))
            return attr(*args, **kwargs)
        return write


class FakeLdap:
    def __init__(self) -> None:
        self.groups: dict[str, list[LdapPerson]] = {}

    def group_members(self, group: str) -> list[LdapPerson]:
        return list(self.groups.get(group, []))


@dataclass
class Scenario:
    """One test's world: an LDAP directory, a project feed, and a job, all uniquely named.

    Names used in tests are short ("alice", "ACE"); the scenario maps them to the unique
    names actually sent to the Origin and back again when reading state.
    """

    admin: PelicanClient
    pelican_settings: PelicanSettings
    kind: str = "projects"
    rid: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    feed: dict[str, str] = field(default_factory=dict)            # project -> display name
    people: dict[str, LdapPerson] = field(default_factory=dict)    # short name -> entry
    last_writes: list[tuple] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.ldap_group = f"it-{self.rid}-active"
        self.base_path = f"{NAMESPACE}/it-{self.rid}/{self.kind}"
        self.projects_url = f"https://topology.example.org/{self.rid}/miscproject/json"

    # -- naming --------------------------------------------------------------------

    def project(self, short: str) -> str:
        return f"{short}_{self.rid}"

    def username(self, short: str) -> str:
        return f"{short}-{self.rid}"

    def sub(self, short: str) -> str:
        return f"http://cilogon.org/serverT/users/{self.rid}/{short}"

    def short_user(self, username: str) -> str:
        return username.removesuffix(f"-{self.rid}")

    def short_project(self, name: str) -> str:
        return name.removesuffix(f"_{self.rid}")

    # -- arranging LDAP and the feed -----------------------------------------------

    def projects(self, *shorts: str) -> None:
        """Set the Topology feed to exactly these projects."""
        self.feed = {self.project(s): f"Project {s}" for s in shorts}

    def member(self, short: str, *projects: str, active: bool = True, username: str | None = None,
               sub: str | None = None) -> None:
        """Put a person in LDAP with exactly these project memberships. active=False drops
        them from the job's LDAP group, so the search no longer returns them."""
        groups = {self.project(p) for p in projects}
        if active:
            groups.add(self.ldap_group)
        self.people[short] = LdapPerson(
            dn=f"uid={short},ou=people,dc=it-{self.rid}",
            username=username if username is not None else self.username(short),
            display_name=f"{short.title()} Example",
            sub=self.sub(short) if sub is None else sub,
            groups=frozenset(groups),
        )

    def leave(self, short: str) -> None:
        """Remove the person from the directory entirely."""
        del self.people[short]

    # -- running -------------------------------------------------------------------

    def job(self) -> Job:
        if self.kind == "projects":
            return Job(kind="projects", group=self.ldap_group, base_path=self.base_path, projects_url=self.projects_url)
        return Job(kind="users", group=self.ldap_group, base_path=self.base_path)

    def settings(self, jobs: list[Job] | None = None, **run: object) -> Settings:
        ldap = LdapSettings(server="ldap.invalid", bind_dn="cn=x", bind_password="x", user_search_base="ou=people",
                            allow_empty_group=bool(run.pop("allow_empty_group", False)))
        return Settings(ldap=ldap, pelican=self.pelican_settings, run=RunSettings(jobs=jobs or [self.job()], **run))

    def directory(self) -> FakeLdap:
        ldap = FakeLdap()
        ldap.groups[self.ldap_group] = [p for p in self.people.values() if self.ldap_group in p.groups]
        return ldap

    def sync(self, crash_before: int | None = None, **run: object) -> SyncReport:
        """One provisioner run with a fresh client, as a CronJob would do it."""
        settings = self.settings(**run)
        client = PelicanClient(self.pelican_settings)
        wrapped = CrashingClient(client, crash_before)
        try:
            return JobRunner(self.directory(), wrapped, settings, settings.jobs[0]).run()
        finally:
            self.last_writes = wrapped.writes
            client.close()

    # -- reading the Origin back ----------------------------------------------------

    def collections(self) -> dict[str, object]:
        """Collections under this scenario's base path, by short name."""
        out = {}
        for c in self.admin.list_collections():
            if c.namespace.rstrip("/").startswith(self.base_path + "/"):
                short = self.short_project(c.name) if self.kind == "projects" else self.short_user(c.name)
                assert short not in out, f"two collections named {c.name}"
                out[short] = c
        return out

    def groups(self) -> dict[str, object]:
        """Groups whose name carries this scenario's id, by short project name."""
        return {self.short_project(g.name): g for g in self.admin.list_groups() if g.name.endswith(f"_{self.rid}")}

    def user_ids(self) -> dict[str, str]:
        """Short name -> Pelican user id, for this scenario's users."""
        return {self.short_user(u.username): u.id for u in self.admin.list_users() if u.username.endswith(f"-{self.rid}")
                or (u.sub or "").startswith(self.sub(""))}

    def access(self) -> dict[str, set[str]]:
        """What the Origin actually grants: collection -> short names of users who can write.

        Follows every ACL on every collection under the base path to the users behind it,
        through group membership for group grants. A collection with no usable ACL maps to
        an empty set. Also checks the invariants every collection must satisfy.
        """
        users = {u.id: u for u in self.admin.list_users()}
        groups = {g.id: g for g in self.admin.list_groups()}
        role = self.pelican_settings.collection_role
        result: dict[str, set[str]] = {}
        for short, c in self.collections().items():
            assert c.description == DESCRIPTION, c
            assert c.namespace == f"{self.base_path}/{c.name}", c
            who: set[str] = set()
            for a in self.admin.list_acls(c.id):
                assert a.role == role, (c.name, a)
                if a.subject_type == "group":
                    g = groups.get(a.subject_id)
                    member_ids = [m.user_id for m in g.members] if g else []
                else:
                    member_ids = [a.subject_id]
                who |= {self.short_user(users[i].username) for i in member_ids if i in users}
            result[short] = who
        return result

    def acl_count(self, short: str) -> int:
        return len(self.admin.list_acls(self.collections()[short].id))

    # -- cleanup -------------------------------------------------------------------

    def cleanup(self) -> None:
        for c in self.admin.list_collections():
            if c.namespace.startswith(f"{NAMESPACE}/it-{self.rid}/"):
                _quietly(self.admin.delete_collection, c.id)
        for g in self.admin.list_groups():
            if self.rid in g.name:
                _quietly(self.admin.delete_group, g.id)
        for u in self.admin.list_users():
            if self.rid in u.username or self.rid in (u.sub or ""):
                _quietly(self.admin._request, "DELETE", f"/users/{u.id}")


def _quietly(fn, *args) -> None:
    try:
        fn(*args)
    except PelicanError:
        pass


@pytest.fixture
def make_scenario(admin, pelican_settings, monkeypatch):
    """Factory for scenarios; every one made is cleaned up after the test."""
    made: list[Scenario] = []
    feeds: dict[str, Scenario] = {}

    def fake_fetch(url: str, timeout_seconds: float = 30) -> dict[str, str]:
        return dict(feeds[url].feed)

    monkeypatch.setattr(jobs_module, "fetch_project_names", fake_fetch)

    def make(kind: str = "projects") -> Scenario:
        s = Scenario(admin=admin, pelican_settings=pelican_settings, kind=kind)
        feeds[s.projects_url] = s
        made.append(s)
        return s

    yield make
    for s in made:
        s.cleanup()


@pytest.fixture
def scenario(make_scenario) -> Scenario:
    return make_scenario("projects")


@pytest.fixture
def users_scenario(make_scenario) -> Scenario:
    return make_scenario("users")
