"""LdapClient with a fake ldap3 connection: entry parsing and error mapping."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from ldap3.core.exceptions import LDAPBindError, LDAPSocketReceiveError

from provisioner import ldap_client
from provisioner.ldap_client import LdapClient, LdapError, LdapPerson

from conftest import make_settings


class FakeConnection:
    def __init__(self, entries=(), fail: Exception | None = None) -> None:
        self.entries = list(entries)
        self.fail = fail
        self.searches: list[dict] = []
        self.unbound = False
        self.extend = SimpleNamespace(standard=SimpleNamespace(paged_search=self.paged_search))

    def paged_search(self, **kwargs):
        self.searches.append(kwargs)
        for entry in self.entries:
            yield entry
        if self.fail is not None:
            raise self.fail

    def unbind(self):
        self.unbound = True


def entry(dn, **attrs):
    return {"type": "searchResEntry", "dn": dn, "attributes": attrs}


def connected(conn: FakeConnection) -> LdapClient:
    client = LdapClient(make_settings().ldap)
    client._conn = conn
    return client


def test_parses_entries_and_skips_referrals():
    conn = FakeConnection([
        entry("uid=1,ou=people", employeeNumber="alice", cn=["Alice A", "Alice"], uid="sub-1",
              isMemberOf=["ap40-login", " ACE ", ""]),
        {"type": "searchResRef", "uri": ["ldap://elsewhere"]},
        entry("uid=2,ou=people", employeeNumber=[], cn=None, uid=["sub-2"], isMemberOf="ap40-login"),
    ])
    people = connected(conn).group_members("ap40-login")

    assert people == [
        LdapPerson(dn="uid=1,ou=people", username="alice", display_name="Alice A", sub="sub-1",
                   groups=frozenset({"ap40-login", "ACE"})),
        LdapPerson(dn="uid=2,ou=people", username=None, display_name=None, sub="sub-2",
                   groups=frozenset({"ap40-login"})),
    ]


def test_group_name_is_escaped_in_the_filter():
    conn = FakeConnection()
    connected(conn).group_members("CO:a*(b)")

    (search,) = conn.searches
    assert search["search_filter"] == r"(isMemberOf=CO:a\2a\28b\29)"
    assert search["search_base"] == "ou=people,dc=example"
    assert set(search["attributes"]) == {"employeeNumber", "cn", "uid", "isMemberOf"}


def test_search_failure_mid_stream_is_an_ldap_error():
    """A partial result must not be returned as if it were the whole group."""
    conn = FakeConnection([entry("uid=1", employeeNumber="a", uid="s")], fail=LDAPSocketReceiveError("reset"))
    with pytest.raises(LdapError, match="search failed"):
        connected(conn).group_members("g")


def test_bind_failure_is_an_ldap_error(monkeypatch):
    def refuse(*args, **kwargs):
        raise LDAPBindError("invalid credentials")

    monkeypatch.setattr(ldap_client.ldap3, "Connection", refuse)
    with pytest.raises(LdapError, match="bind to ldap.example.org:636 failed"):
        LdapClient(make_settings().ldap).group_members("g")


def test_close_unbinds_once():
    conn = FakeConnection()
    client = connected(conn)
    client.close()
    client.close()
    assert conn.unbound
