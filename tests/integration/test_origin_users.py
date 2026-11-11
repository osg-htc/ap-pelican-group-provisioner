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


def test_ldap_value_with_slash_or_dot_segments_never_leaves_its_own_prefix(users_scenario):
    """Regression: Pelican path-cleans a collection namespace before storing it, so a raw
    LDAP value of "m/../victim" used to land on victim's prefix and "x/../../.." on the
    export root, each with a write grant to the member who had that value."""
    s = users_scenario
    victim = s.username("victim")
    s.member("victim")
    s.member("mallory", username=f"mallory-{s.rid}/../{victim}")
    s.member("nested", username=f"{victim}/sub")
    s.member("escape", username=f"x{s.rid}/../../..")
    s.member("dotted", username=f"first.last..{s.rid}")

    report = s.sync(**RUN)

    assert len(report.errors) == 3 and all("contains '/'" in e for e in report.errors), report.errors
    ours = [c for c in s.admin.list_collections() if s.rid in c.name or s.rid in c.namespace]
    assert sorted(c.namespace for c in ours) == sorted([f"{s.base_path}/{victim}", f"{s.base_path}/first.last..{s.rid}"])
    assert s.access()["victim"] == {"victim"}
    assert set(s.user_ids()) == {"victim", f"first.last.{s.rid}"}   # skipped members get no user; ".." sanitizes to "."


def test_grant_added_by_hand_is_kept(users_scenario):
    """By design the provisioner only adds its own grant; extra grants on a managed
    collection (an owner sharing it) survive every run. This pins that behavior down."""
    s = users_scenario
    s.member("alice")
    s.member("bob")
    s.sync()
    ids = s.user_ids()
    s.admin.grant_acl(s.collections()["alice"].id, "user", ids["bob"], s.pelican_settings.collection_role)

    s.sync()

    assert s.last_writes == []
    assert s.access() == {"alice": {"alice", "bob"}, "bob": {"bob"}}


# -- placeholder subs from the COManage bulk import ------------------------------------------
# People imported without a CILogon ID get uid "dummy-text-for-provisioning-<username>"
# (osg-comanage-scripts/mass_person_create_modify.py) until COManage records their real one.


def placeholder(s, short: str) -> str:
    return f"dummy-text-for-provisioning-{s.username(short)}"


def test_placeholder_sub_replaced_by_real_one_keeps_the_collection(users_scenario):
    """The real sub arrives: the same collection (same ID, same path) is granted to an
    account with the real identity, under a numbered name because the placeholder account
    keeps the plain one. Nothing is deleted and nobody else gains access."""
    s = users_scenario
    s.member("alice", sub=placeholder(s, "alice"))
    s.member("bob")
    s.sync()
    collection_id = s.collections()["alice"].id
    placeholder_user = s.user_ids()["alice"]

    s.member("alice")                                   # COManage now publishes the real sub
    report = s.sync()

    assert report.exit_code == EXIT_OK, report.errors
    assert report.users_created == 1 and report.collections_deleted == 0 and report.collections_created == 0
    assert s.collections()["alice"].id == collection_id
    real = next(u for u in s.admin.list_users() if u.sub == s.sub("alice"))
    assert real.username == f"{s.username('alice')}2" and real.id != placeholder_user
    # The placeholder account keeps its grant, but no CILogon login can ever reach it.
    assert s.access() == {"alice": {"alice", real.username}, "bob": {"bob"}}
    s.sync()
    assert s.last_writes == []


def test_placeholder_sub_person_who_logged_in_first_is_adopted(users_scenario):
    """They logged in through CILogon before the real sub reached LDAP, so Pelican already
    made an account with the real identity (under some other name). That account gets the
    collection; no third account is created."""
    s = users_scenario
    s.member("alice", sub=placeholder(s, "alice"))
    s.sync()
    collection_id = s.collections()["alice"].id
    logged_in = s.admin.create_user(f"{s.username('alice')}-login", "Alice", s.sub("alice"), "https://cilogon.org")

    s.member("alice")
    report = s.sync()

    assert report.exit_code == EXIT_OK, report.errors
    assert report.users_created == 0
    assert s.collections()["alice"].id == collection_id
    assert s.access() == {"alice": {"alice", logged_in.username}}
