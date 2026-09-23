from __future__ import annotations

import pytest

from provisioner.config import Job, LdapSettings, PelicanSettings, RunSettings, Settings
from provisioner.ldap_client import LdapPerson
from provisioner.pelican_client import (
    CollectionAcl,
    GroupMember,
    PelicanCollection,
    PelicanError,
    PelicanGroup,
    PelicanUser,
)

BASE_PATH = "/ospool/ap40/data"   # the users job's base_path
GROUP = "ap40-login"
PROJECTS_BASE = "/ospool/projects"
PROJECTS_GROUP = "CO:members:active"
PROJECTS_URL = "https://topology.example.org/miscproject/json"
DESCRIPTION = "AUTOMATED: Do not manually edit this group."


def make_settings(**overrides) -> Settings:
    ldap = LdapSettings(
        server="ldap.example.org",
        bind_dn="cn=readonly,dc=example",
        bind_password="secret",
        user_search_base="ou=people,dc=example",
        allow_empty_group=overrides.pop("allow_empty_group", False),
    )
    pelican = PelicanSettings(
        origin_url="https://origin.example.org",
        admin_username="admin",
        admin_password="secret",
        collection_role=overrides.pop("collection_role", "write"),
    )
    jobs = overrides.pop("jobs", None) or [Job(group=GROUP, base_path=overrides.pop("base_path", BASE_PATH))]
    run = RunSettings(
        dry_run=overrides.pop("dry_run", False),
        max_delete_fraction=overrides.pop("max_delete_fraction", 0.05),
        allow_mass_delete=overrides.pop("allow_mass_delete", False),
        jobs=jobs,
    )
    assert not overrides, f"unknown overrides: {overrides}"
    return Settings(ldap=ldap, pelican=pelican, run=run)


class FakeLdap:
    """Members per group name; a list is shorthand for the default GROUP."""

    def __init__(self, people: list[LdapPerson] | dict[str, list[LdapPerson]] | None = None) -> None:
        if isinstance(people, dict):
            self.groups = people
        else:
            self.groups = {GROUP: list(people or [])}
        self.queried: list[str] = []

    def group_members(self, group: str) -> list[LdapPerson]:
        self.queried.append(group)
        return list(self.groups.get(group, []))


class FakePelican:
    """In-memory Origin. Records every write so tests can assert on them."""

    def __init__(self) -> None:
        self.users: dict[str, PelicanUser] = {}
        self.collections: dict[str, PelicanCollection] = {}
        self.acls: dict[str, list[CollectionAcl]] = {}
        self.metadata: dict[str, dict[str, str]] = {}
        self.groups: dict[str, PelicanGroup] = {}
        self.calls: list[tuple] = []
        self.fail_grant_for: set[str] = set()
        self.fail_create_user: set[str] = set()
        self._next_id = 1

    def _id(self, prefix: str) -> str:
        value = f"{prefix}{self._next_id:03d}"
        self._next_id += 1
        return value

    # helpers for arranging state
    def add_user(self, username: str, sub: str = "", issuer: str = "https://cilogon.org") -> PelicanUser:
        """Arrange an existing Pelican user. By default their sub is the one person() would
        give the same name, so they are the same identity as the LDAP member."""
        user = PelicanUser(id=self._id("u"), username=username,
                           sub=sub or f"http://cilogon.org/serverA/users/{username}", issuer=issuer)
        self.users[user.id] = user
        return user

    def add_collection(self, name: str, namespace: str | None = None, description: str = DESCRIPTION,
                       group: str | None = GROUP) -> PelicanCollection:
        """Arrange a collection. group=None means 'not created by the provisioner'."""
        if namespace is None:
            namespace = f"{BASE_PATH}/{name}"
        c = PelicanCollection(id=self._id("c"), name=name, namespace=namespace, description=description)
        self.collections[c.id] = c
        self.acls[c.id] = []
        self.metadata[c.id] = {"provisioner.source": "ldap", "provisioner.group": group} if group else {}
        return c

    def add_group(self, name: str, members: list[str] = (), description: str = "", source: str = "oidc") -> PelicanGroup:
        """Arrange a group. Default source 'oidc' mimics a group Pelican asserted from a login claim."""
        g = PelicanGroup(id=self._id("g"), name=name, description=description,
                         members=tuple(GroupMember(user_id=m, source=source) for m in members))
        self.groups[g.id] = g
        return g

    def group_named(self, name: str) -> PelicanGroup | None:
        return next((g for g in self.groups.values() if g.name == name), None)

    def writes(self) -> list[tuple]:
        reads = {"list_users", "list_collections", "list_acls", "get_collection_metadata", "list_groups"}
        return [c for c in self.calls if c[0] not in reads]

    # OriginApi
    def list_users(self) -> list[PelicanUser]:
        self.calls.append(("list_users",))
        return list(self.users.values())

    def create_user(self, username, display_name, sub, issuer) -> PelicanUser:
        self.calls.append(("create_user", username, display_name, sub, issuer))
        if username in self.fail_create_user:
            raise PelicanError("conflict", status=400)
        user = PelicanUser(id=self._id("u"), username=username, sub=sub, issuer=issuer)
        self.users[user.id] = user
        return user

    def list_collections(self) -> list[PelicanCollection]:
        self.calls.append(("list_collections",))
        return list(self.collections.values())

    def create_collection(self, name, namespace, description, visibility, metadata=None) -> PelicanCollection:
        self.calls.append(("create_collection", name, namespace, description, visibility, metadata))
        c = PelicanCollection(id=self._id("c"), name=name, namespace=namespace, description=description)
        self.collections[c.id] = c
        self.acls[c.id] = []
        self.metadata[c.id] = dict(metadata or {})
        return c

    def delete_collection(self, collection_id) -> None:
        self.calls.append(("delete_collection", collection_id))
        del self.collections[collection_id]
        self.acls.pop(collection_id, None)
        self.metadata.pop(collection_id, None)

    def get_collection_metadata(self, collection_id) -> dict[str, str]:
        self.calls.append(("get_collection_metadata", collection_id))
        return dict(self.metadata[collection_id])

    def list_acls(self, collection_id) -> list[CollectionAcl]:
        self.calls.append(("list_acls", collection_id))
        return list(self.acls[collection_id])

    def grant_acl(self, collection_id, subject_type, subject_id, role) -> None:
        self.calls.append(("grant_acl", collection_id, subject_type, subject_id, role))
        name = self.collections[collection_id].name
        if name in self.fail_grant_for:
            raise PelicanError("boom", status=400)
        self.acls[collection_id].append(CollectionAcl(subject_type=subject_type, subject_id=subject_id, role=role))

    def list_groups(self) -> list[PelicanGroup]:
        self.calls.append(("list_groups",))
        return list(self.groups.values())

    def create_group(self, name, display_name, description) -> PelicanGroup:
        self.calls.append(("create_group", name, display_name, description))
        if self.group_named(name) is not None:
            raise PelicanError("group exists", status=400)
        g = PelicanGroup(id=self._id("g"), name=name, description=description)
        self.groups[g.id] = g
        return g

    def delete_group(self, group_id) -> None:
        self.calls.append(("delete_group", group_id))
        del self.groups[group_id]

    def add_group_member(self, group_id, user_id) -> None:
        self.calls.append(("add_group_member", group_id, user_id))
        g = self.groups[group_id]
        if any(m.user_id == user_id for m in g.members):
            raise PelicanError("already a member", status=400)
        self.groups[group_id] = PelicanGroup(id=g.id, name=g.name, description=g.description,
                                             members=g.members + (GroupMember(user_id=user_id, source="api"),))

    def remove_group_member(self, group_id, user_id) -> None:
        self.calls.append(("remove_group_member", group_id, user_id))
        g = self.groups[group_id]
        self.groups[group_id] = PelicanGroup(id=g.id, name=g.name, description=g.description,
                                             members=tuple(m for m in g.members if m.user_id != user_id))


def acl(subject_id: str, role: str = "write", subject_type: str = "user") -> CollectionAcl:
    return CollectionAcl(subject_type=subject_type, subject_id=subject_id, role=role)


def person(username: str, sub: str | None = None, cn: str | None = None, groups: list[str] = ()) -> LdapPerson:
    return LdapPerson(
        dn=f"uid={sub or username},ou=people,dc=example",
        username=username,
        display_name=cn or username.title(),
        sub=sub or f"http://cilogon.org/serverA/users/{username}",
        groups=frozenset(groups),
    )


@pytest.fixture
def pelican() -> FakePelican:
    return FakePelican()
