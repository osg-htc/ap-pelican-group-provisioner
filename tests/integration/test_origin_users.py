"""Users job against a real Origin: personal collections as people join and leave."""

from __future__ import annotations

import pytest

from provisioner.report import EXIT_OK, HardError

from origin_harness import admin, admin_token, make_scenario, pelican_settings, users_scenario  # noqa: F401  (fixtures)

pytestmark = pytest.mark.origin

RUN = {"max_delete_fraction": 1.0}


def test_each_member_gets_a_collection_only_they_can_write(users_scenario):
    s = users_scenario
    s.member("alice")
    s.member("bob")

    report = s.sync()

    assert report.exit_code == EXIT_OK, report.errors
    assert s.access() == {"alice": {"alice"}, "bob": {"bob"}}
    for name in ("alice", "bob"):
        assert s.acl_count(name) == 1
    assert s.groups() == {}   # the users job never makes groups
    s.sync()
    assert s.last_writes == []


def test_join_leave_and_return(users_scenario):
    s = users_scenario
    s.member("alice")
    s.member("bob")
    s.sync()
    bob_collection, bob_id = s.collections()["bob"].id, s.user_ids()["bob"]

    s.member("carol")
    s.member("bob", active=False)
    s.sync(**RUN)
    assert s.access() == {"alice": {"alice"}, "carol": {"carol"}}

    s.member("bob")
    s.sync()
    assert s.access() == {"alice": {"alice"}, "bob": {"bob"}, "carol": {"carol"}}
    # Same user, new collection: the old one and anything granted on it by hand are gone.
    assert s.user_ids()["bob"] == bob_id
    assert s.collections()["bob"].id != bob_collection


def test_username_change_moves_the_collection_to_the_new_name(users_scenario):
    s = users_scenario
    s.member("alice")
    s.member("bob")
    s.sync()
    alice_id = s.user_ids()["alice"]

    s.member("alice", username=f"alicia-{s.rid}")
    s.sync(**RUN)

    collections = s.collections()
    assert set(collections) == {"alicia", "bob"}
    # The collection follows the LDAP value, the grant follows the identity.
    assert {(a.subject_type, a.subject_id) for a in s.admin.list_acls(collections["alicia"].id)} == {("user", alice_id)}


def test_mass_departure_refused(users_scenario):
    s = users_scenario
    for name in ("alice", "bob", "carol"):
        s.member(name)
    s.sync()

    s.leave("bob")
    s.leave("carol")
    with pytest.raises(HardError, match="refusing to delete 2 of 3"):
        s.sync()
    assert s.last_writes == []
    assert set(s.collections()) == {"alice", "bob", "carol"}


def test_users_and_projects_jobs_share_an_origin(make_scenario):
    users, projects = make_scenario("users"), make_scenario("projects")
    users.member("alice")
    projects.projects("ACE")
    projects.member("bob", "ACE")

    users.sync()
    projects.sync()
    users.sync()

    assert users.access() == {"alice": {"alice"}}
    assert projects.access() == {"ACE": {"bob"}}
    assert users.last_writes == []
