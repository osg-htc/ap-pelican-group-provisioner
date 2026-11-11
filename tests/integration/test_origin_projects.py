"""Projects job against a real Origin: people moving in and out of projects.

Every assertion on access goes through Scenario.access(), which follows the collection's
ACLs on the Origin to the group and from the group to its users, so it checks what Pelican
would actually authorize rather than what the provisioner meant to do.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from provisioner.report import EXIT_OK, EXIT_PARTIAL, HardError

from origin_harness import SimulatedCrash, admin, admin_token, make_scenario, pelican_settings, scenario  # noqa: F401  (fixtures)

pytestmark = pytest.mark.origin

NO_DELETE_CEILING = {"max_delete_fraction": 1.0}


def assert_converged(scenario) -> None:
    """A run on unchanged inputs must make no writes at all."""
    report = scenario.sync(**NO_DELETE_CEILING)
    assert report.exit_code == EXIT_OK, report.errors
    assert scenario.last_writes == [], scenario.last_writes


# -- first run -----------------------------------------------------------------------


def test_first_run_grants_each_project_to_exactly_its_members(scenario):
    scenario.projects("ACE", "Evol", "Idle")
    scenario.member("alice", "ACE")
    scenario.member("bob", "ACE", "Evol")
    scenario.member("carol", "NotInTopology")   # active, but in no known project

    report = scenario.sync()

    assert report.exit_code == EXIT_OK, report.errors
    assert scenario.access() == {"ACE": {"alice", "bob"}, "Evol": {"bob"}}
    # One group per collection, carrying the marker; no group for the memberless project.
    assert set(scenario.groups()) == {"ACE", "Evol"}
    assert all(g.description == "AUTOMATED: Do not manually edit this group." for g in scenario.groups().values())
    # Only people in a known project become Pelican users.
    assert set(scenario.user_ids()) == {"alice", "bob"}
    for name in ("ACE", "Evol"):
        assert scenario.acl_count(name) == 1
    assert_converged(scenario)


def test_collection_metadata_on_the_origin(scenario, admin):
    scenario.projects("ACE")
    scenario.member("alice", "ACE")
    scenario.sync()

    c = scenario.collections()["ACE"]
    assert admin.get_collection_metadata(c.id) == {
        "provisioner.source": "ldap",
        "provisioner.kind": "projects",
        "provisioner.group": scenario.ldap_group,
        "provisioner.project": scenario.project("ACE"),
        "provisioner.pelican_group": scenario.project("ACE"),
    }


# -- people coming in ----------------------------------------------------------------


def test_new_member_joins_existing_project_without_recreating_anything(scenario):
    scenario.projects("ACE")
    scenario.member("alice", "ACE")
    scenario.sync()
    collection_id, group_id = scenario.collections()["ACE"].id, scenario.groups()["ACE"].id

    scenario.member("bob", "ACE")
    report = scenario.sync()

    assert report.members_added == 1 and report.users_created == 1
    assert scenario.access() == {"ACE": {"alice", "bob"}}
    assert scenario.collections()["ACE"].id == collection_id
    assert scenario.groups()["ACE"].id == group_id
    assert_converged(scenario)


def test_existing_member_joins_a_second_project(scenario):
    scenario.projects("ACE", "Evol")
    scenario.member("alice", "ACE")
    scenario.member("bob", "Evol")
    scenario.sync()
    alice_id = scenario.user_ids()["alice"]

    scenario.member("alice", "ACE", "Evol")
    report = scenario.sync()

    assert report.users_created == 0
    assert scenario.user_ids()["alice"] == alice_id
    assert scenario.access() == {"ACE": {"alice"}, "Evol": {"alice", "bob"}}


def test_first_member_of_a_project_creates_collection_and_group(scenario):
    scenario.projects("ACE", "Evol")
    scenario.member("alice", "ACE")
    scenario.sync()

    scenario.member("bob", "Evol")
    scenario.sync()

    assert scenario.access() == {"ACE": {"alice"}, "Evol": {"bob"}}
    assert set(scenario.groups()) == {"ACE", "Evol"}


def test_project_added_to_topology_after_members_already_have_it(scenario):
    """LDAP lists the project before Topology does; nothing is provisioned until both agree."""
    scenario.projects("ACE")
    scenario.member("alice", "ACE", "Late")
    scenario.sync()
    assert scenario.access() == {"ACE": {"alice"}}

    scenario.projects("ACE", "Late")
    scenario.sync()
    assert scenario.access() == {"ACE": {"alice"}, "Late": {"alice"}}


def test_project_names_match_exactly_not_by_case(scenario):
    scenario.projects("ACE")
    scenario.member("alice", "ACE")
    scenario.member("mallory")
    # Same name as the project but different case: must not be granted.
    mallory = scenario.people["mallory"]
    scenario.people["mallory"] = replace(mallory, groups=mallory.groups | {scenario.project("ACE").lower()})
    scenario.sync()

    assert scenario.access() == {"ACE": {"alice"}}


# -- people going out ----------------------------------------------------------------


def test_member_leaves_one_of_several_projects(scenario):
    scenario.projects("ACE", "Evol")
    scenario.member("alice", "ACE")
    scenario.member("bob", "ACE", "Evol")
    scenario.sync()

    scenario.member("bob", "Evol")
    report = scenario.sync()

    assert report.members_removed == 1
    assert scenario.access() == {"ACE": {"alice"}, "Evol": {"bob"}}
    assert_converged(scenario)


def test_member_leaves_last_project_but_stays_in_ldap_group(scenario):
    scenario.projects("ACE")
    scenario.member("alice", "ACE")
    scenario.member("bob", "ACE")
    scenario.sync()
    bob_id = scenario.user_ids()["bob"]

    scenario.member("bob")   # still active, no projects
    scenario.sync()

    assert scenario.access() == {"ACE": {"alice"}}
    # Users are never deleted.
    assert scenario.user_ids()["bob"] == bob_id


def test_member_leaves_ldap_group_loses_every_project(scenario):
    """Dropping out of the job's group removes all access even though the person's entry
    still lists the projects."""
    scenario.projects("ACE", "Evol")
    scenario.member("alice", "ACE", "Evol")
    scenario.member("bob", "ACE", "Evol")
    scenario.sync()

    scenario.member("bob", "ACE", "Evol", active=False)
    scenario.sync()

    assert scenario.access() == {"ACE": {"alice"}, "Evol": {"alice"}}


def test_member_disappears_from_directory(scenario):
    scenario.projects("ACE")
    scenario.member("alice", "ACE")
    scenario.member("bob", "ACE")
    scenario.sync()

    scenario.leave("bob")
    scenario.sync()

    assert scenario.access() == {"ACE": {"alice"}}


def test_last_member_leaves_deletes_collection_and_group(scenario):
    scenario.projects("ACE", "Evol")
    scenario.member("alice", "ACE")
    scenario.member("bob", "Evol")
    scenario.sync()

    scenario.member("bob")
    report = scenario.sync(**NO_DELETE_CEILING)

    assert report.collections_deleted == 1 and report.groups_deleted == 1
    assert scenario.access() == {"ACE": {"alice"}}
    assert set(scenario.groups()) == {"ACE"}
    assert_converged(scenario)


def test_project_removed_from_topology_revokes_access_although_ldap_still_lists_it(scenario):
    scenario.projects("ACE", "Evol")
    scenario.member("alice", "ACE", "Evol")
    scenario.sync()

    scenario.projects("ACE")
    scenario.sync(**NO_DELETE_CEILING)

    assert scenario.access() == {"ACE": {"alice"}}
    assert set(scenario.groups()) == {"ACE"}


# -- moves and returns ---------------------------------------------------------------


def test_member_moves_between_projects_in_one_run(scenario):
    scenario.projects("ACE", "Evol")
    scenario.member("alice", "ACE")
    scenario.member("bob", "ACE")
    scenario.member("carol", "Evol")
    scenario.sync()

    scenario.member("bob", "Evol")
    report = scenario.sync()

    assert report.members_added == 1 and report.members_removed == 1
    assert scenario.access() == {"ACE": {"alice"}, "Evol": {"bob", "carol"}}


def test_two_members_swap_projects(scenario):
    scenario.projects("ACE", "Evol")
    scenario.member("alice", "ACE")
    scenario.member("bob", "Evol")
    scenario.sync()

    scenario.member("alice", "Evol")
    scenario.member("bob", "ACE")
    scenario.sync()

    assert scenario.access() == {"ACE": {"bob"}, "Evol": {"alice"}}


def test_entire_membership_replaced_keeps_collection(scenario):
    scenario.projects("ACE")
    scenario.member("alice", "ACE")
    scenario.member("bob", "ACE")
    scenario.sync()
    collection_id = scenario.collections()["ACE"].id

    scenario.member("alice")
    scenario.member("bob")
    scenario.member("carol", "ACE")
    scenario.member("dave", "ACE")
    scenario.sync()

    assert scenario.access() == {"ACE": {"carol", "dave"}}
    assert scenario.collections()["ACE"].id == collection_id


def test_member_returns_after_leaving_reuses_their_user(scenario):
    scenario.projects("ACE")
    scenario.member("alice", "ACE")
    scenario.member("bob", "ACE")
    scenario.sync()
    bob_id = scenario.user_ids()["bob"]

    scenario.leave("bob")
    scenario.sync()
    scenario.member("bob", "ACE")
    report = scenario.sync()

    assert report.users_created == 0
    assert scenario.user_ids()["bob"] == bob_id
    assert scenario.access() == {"ACE": {"alice", "bob"}}


def test_project_that_emptied_and_refilled_gets_a_fresh_collection_and_group(scenario):
    scenario.projects("ACE", "Evol")
    scenario.member("alice", "ACE")
    scenario.member("bob", "Evol")
    scenario.sync()
    old_collection, old_group = scenario.collections()["Evol"].id, scenario.groups()["Evol"].id

    scenario.member("bob")
    scenario.sync(**NO_DELETE_CEILING)
    assert "Evol" not in scenario.collections()

    scenario.member("carol", "Evol")
    scenario.sync()

    assert scenario.access() == {"ACE": {"alice"}, "Evol": {"carol"}}   # bob does not come back
    assert scenario.collections()["Evol"].id != old_collection
    assert scenario.groups()["Evol"].id != old_group


# -- identity edge cases -------------------------------------------------------------


def test_ldap_username_change_keeps_the_same_pelican_user(scenario, admin):
    """employeeNumber changes but the CILogon sub does not: same person, same user, same access."""
    scenario.projects("ACE")
    scenario.member("alice", "ACE")
    scenario.sync()
    alice_id = scenario.user_ids()["alice"]

    scenario.member("alice", "ACE", username=f"alice2-{scenario.rid}")
    report = scenario.sync()

    assert report.users_created == 0 and report.members_added == 0 and report.members_removed == 0
    assert {m.user_id for m in scenario.groups()["ACE"].members} == {alice_id}


def test_username_taken_by_a_different_identity_is_never_granted(scenario, admin):
    """A Pelican user already holds the member's username with another sub (for example
    someone who logged in first). The member gets a numbered username; the squatter gets
    nothing."""
    squatter = admin.create_user(scenario.username("alice"), "Squatter", "http://other.example/squatter",
                                 "https://cilogon.org")
    scenario.projects("ACE")
    scenario.member("alice", "ACE")
    report = scenario.sync()

    assert report.exit_code == EXIT_OK, report.errors
    members = {m.user_id for m in scenario.groups()["ACE"].members}
    assert squatter.id not in members
    usernames = {u.id: u.username for u in admin.list_users()}
    assert [usernames[i] for i in members] == [f"alice-{scenario.rid}2"]


def test_member_missing_sub_is_skipped_and_loses_access(scenario):
    """An entry that lost its uid cannot be tied to an identity. The run reports it and
    reconciles without them, which removes their memberships."""
    scenario.projects("ACE")
    scenario.member("alice", "ACE")
    scenario.member("bob", "ACE")
    scenario.sync()

    scenario.member("bob", "ACE", sub="")
    report = scenario.sync()

    assert report.exit_code == EXIT_PARTIAL
    assert scenario.access() == {"ACE": {"alice"}}


def test_two_entries_with_the_same_sub_grant_only_the_first(scenario):
    scenario.projects("ACE", "Evol")
    scenario.member("alice", "ACE")
    scenario.member("alias", "Evol", sub=scenario.sub("alice"))
    report = scenario.sync()

    assert report.exit_code == EXIT_PARTIAL
    # Sorted by LDAP value, "alias" is processed first and keeps the identity.
    assert scenario.access() == {"Evol": {"alias"}}


# -- safeguards ----------------------------------------------------------------------


def test_mass_departure_is_refused_before_any_group_changes(scenario):
    """A partial LDAP result empties several projects at once. The run must stop before it
    touches memberships anywhere, including projects that would have survived."""
    scenario.projects("ACE", "Evol", "Gen")
    scenario.member("alice", "ACE")
    scenario.member("bob", "ACE", "Evol")
    scenario.member("carol", "Gen")
    scenario.sync()
    before = scenario.access()

    scenario.leave("bob")      # ACE loses a member, Evol empties
    scenario.leave("carol")    # Gen empties: 2 of 3 collections would go
    with pytest.raises(HardError, match="refusing to delete 2 of 3"):
        scenario.sync()

    assert scenario.last_writes == []
    assert scenario.access() == before

    report = scenario.sync(allow_mass_delete=True)
    assert report.exit_code == EXIT_OK
    assert scenario.access() == {"ACE": {"alice"}}


def test_empty_ldap_group_is_refused(scenario):
    scenario.projects("ACE")
    scenario.member("alice", "ACE")
    scenario.sync()
    before = scenario.access()

    scenario.leave("alice")
    with pytest.raises(HardError, match="zero members"):
        scenario.sync(**NO_DELETE_CEILING)
    assert scenario.access() == before


def test_no_member_in_any_known_project_is_refused(scenario):
    scenario.projects("ACE")
    scenario.member("alice", "ACE")
    scenario.sync()

    scenario.member("alice", "SomethingElse")
    with pytest.raises(HardError, match="zero project members"):
        scenario.sync(**NO_DELETE_CEILING)
    assert scenario.access() == {"ACE": {"alice"}}


def test_dry_run_changes_nothing(scenario):
    scenario.projects("ACE")
    scenario.member("alice", "ACE")
    scenario.sync()

    scenario.member("alice")
    scenario.member("bob", "ACE")
    report = scenario.sync(dry_run=True)

    assert report.members_added == 1 and report.members_removed == 1
    assert scenario.last_writes == []
    assert scenario.access() == {"ACE": {"alice"}}


# -- out-of-band changes on the Origin -----------------------------------------------


def test_stranger_added_to_managed_group_is_removed(scenario, admin):
    scenario.projects("ACE")
    scenario.member("alice", "ACE")
    scenario.sync()
    stranger = admin.create_user(f"stranger-{scenario.rid}", None, f"http://other.example/{scenario.rid}",
                                 "https://cilogon.org")
    admin.add_group_member(scenario.groups()["ACE"].id, stranger.id)
    assert scenario.access() == {"ACE": {"alice", "stranger"}}

    report = scenario.sync()

    assert report.members_removed == 1
    assert scenario.access() == {"ACE": {"alice"}}


def test_managed_group_deleted_out_of_band_is_recreated_and_regranted(scenario, admin):
    scenario.projects("ACE")
    scenario.member("alice", "ACE")
    scenario.sync()
    admin.delete_group(scenario.groups()["ACE"].id)
    assert scenario.access() == {"ACE": set()}

    report = scenario.sync()

    assert report.groups_created == 1 and report.acls_repaired == 1
    assert scenario.access() == {"ACE": {"alice"}}
    # The grant to the deleted group must not linger next to the new one.
    assert scenario.acl_count("ACE") == 1


def test_preexisting_group_with_project_name_is_adopted_but_never_deleted(scenario, admin):
    """A group named after the project already exists (Pelican makes these from CILogon
    claims; an admin may have made one by hand). It is reused, its membership is brought in
    line with LDAP, and because it lacks the marker it survives the project going away."""
    scenario.projects("ACE", "Evol")
    group = admin.create_group(scenario.project("ACE"), "Hand made", "made by a person")
    stranger = admin.create_user(f"stranger-{scenario.rid}", None, f"http://other.example/{scenario.rid}",
                                 "https://cilogon.org")
    admin.add_group_member(group.id, stranger.id)
    scenario.member("alice", "ACE", "Evol")
    scenario.sync()

    assert scenario.groups()["ACE"].id == group.id
    assert scenario.access() == {"ACE": {"alice"}, "Evol": {"alice"}}

    scenario.member("alice", "Evol")
    scenario.sync(**NO_DELETE_CEILING)

    assert scenario.access() == {"Evol": {"alice"}}
    assert scenario.groups()["ACE"].id == group.id


def test_collection_made_by_hand_under_the_base_path_is_removed(scenario, admin):
    scenario.projects("ACE")
    scenario.member("alice", "ACE")
    scenario.sync()
    admin.create_collection(name=f"Hand_{scenario.rid}", namespace=f"{scenario.base_path}/Hand_{scenario.rid}",
                            description="mine", visibility="private")

    scenario.sync()

    assert set(scenario.collections()) == {"ACE"}


# -- groups someone else controls ----------------------------------------------------------


def hand_over(admin, group_id: str, **ownership: str) -> None:
    """Give a group's ownership or group-admin role to someone else, as a system admin can."""
    admin._request("PUT", f"/groups/{group_id}/ownership", json=ownership)


def stranger(scenario, admin):
    return admin.create_user(f"stranger-{scenario.rid}", None, f"http://other.example/{scenario.rid}",
                             "https://cilogon.org")


@pytest.mark.parametrize("role", ["owner", "user-admin", "group-admin"])
def test_group_someone_else_controls_is_replaced(scenario, admin, role):
    """Whoever owns or administers a project group could add people to it between runs and
    so to the project's collection. Such a group is deleted and recreated under the
    provisioner's account, and the collection is granted to the new group only."""
    scenario.projects("ACE")
    scenario.member("alice", "ACE")
    scenario.sync()
    old = scenario.groups()["ACE"]
    other = stranger(scenario, admin)
    if role == "owner":
        hand_over(admin, old.id, ownerId=other.id)
    elif role == "user-admin":
        hand_over(admin, old.id, adminId=other.id, adminType="user")
    else:
        friends = admin.create_group(f"friends_{scenario.rid}", "Friends", "theirs")
        hand_over(admin, old.id, adminId=friends.id, adminType="group")
    admin.add_group_member(old.id, other.id)        # what the new controller would do

    report = scenario.sync()

    assert report.exit_code == EXIT_OK, report.errors
    assert report.groups_replaced == 1
    new = scenario.groups()["ACE"]
    me = admin.get_me().id
    assert new.id != old.id and new.owner_id == me and new.admin_id == ""
    assert new.description == scenario.pelican_settings.collection_description
    assert scenario.access() == {"ACE": {"alice"}}
    assert scenario.acl_count("ACE") == 1
    assert_converged(scenario)


def test_too_many_groups_to_replace_is_refused_before_any_change(scenario, admin):
    """Every group suddenly 'foreign' most likely means the provisioner is running as a
    different account, not a takeover. Nothing is touched until the operator says so."""
    scenario.projects("ACE", "Evol")
    scenario.member("alice", "ACE", "Evol")
    scenario.sync()
    other = stranger(scenario, admin)
    before = scenario.groups()
    for g in before.values():
        hand_over(admin, g.id, ownerId=other.id)

    with pytest.raises(HardError, match="refusing to replace 2 of 2 project groups"):
        scenario.sync()
    assert scenario.last_writes == []
    assert {k: g.id for k, g in scenario.groups().items()} == {k: g.id for k, g in before.items()}

    report = scenario.sync(allow_mass_delete=True)
    assert report.groups_replaced == 2
    assert scenario.access() == {"ACE": {"alice"}, "Evol": {"alice"}}


def test_crash_between_delete_and_recreate_is_safe(scenario, admin):
    """The run dies after deleting the foreign group: the project has no access at all until
    the next run, never the foreign controller's, and the next run recreates it."""
    scenario.projects("ACE")
    scenario.member("alice", "ACE")
    scenario.sync()
    other = stranger(scenario, admin)
    hand_over(admin, scenario.groups()["ACE"].id, ownerId=other.id)
    admin.add_group_member(scenario.groups()["ACE"].id, other.id)

    with pytest.raises(SimulatedCrash):
        scenario.sync(crash_before=1)
    assert [w[0] for w in scenario.last_writes] == ["delete_group"]
    assert scenario.access() == {"ACE": set()}

    report = scenario.sync()
    assert report.groups_created == 1 and report.groups_replaced == 0
    assert scenario.access() == {"ACE": {"alice"}}
    assert scenario.groups()["ACE"].owner_id == admin.get_me().id


def test_placeholder_sub_replaced_by_real_one_moves_project_access(scenario):
    """A bulk-imported member had uid "dummy-text-for-provisioning-<username>" until COManage
    recorded their real CILogon ID. When it arrives, the account with the real identity takes
    the placeholder account's place in every project group; collections and groups keep
    their IDs and nothing is deleted."""
    scenario.projects("ACE", "Evol")
    scenario.member("alice", "ACE", "Evol", sub=f"dummy-text-for-provisioning-{scenario.username('alice')}")
    scenario.member("bob", "ACE")
    scenario.sync()
    ids_before = ({k: c.id for k, c in scenario.collections().items()}, {k: g.id for k, g in scenario.groups().items()})

    scenario.member("alice", "ACE", "Evol")
    report = scenario.sync()

    assert report.exit_code == EXIT_OK, report.errors
    assert report.users_created == 1 and report.members_added == 2 and report.members_removed == 2
    assert report.collections_deleted == 0 and report.groups_replaced == 0
    assert ({k: c.id for k, c in scenario.collections().items()}, {k: g.id for k, g in scenario.groups().items()}) == ids_before
    alice = f"{scenario.username('alice')}2"
    assert scenario.access() == {"ACE": {alice, "bob"}, "Evol": {alice}}   # placeholder account is out
    assert_converged(scenario)
