from __future__ import annotations

import pytest

from provisioner.identifiers import sanitize_identifier, with_suffix
from provisioner.jobs import JobRunner
from provisioner.ldap_client import LdapPerson
from provisioner.report import EXIT_OK, EXIT_PARTIAL, HardError

from conftest import BASE_PATH, DESCRIPTION, GROUP, FakeLdap, acl, make_settings, person


def run(pelican, people, **settings_overrides):
    settings = make_settings(**settings_overrides)
    return JobRunner(FakeLdap(people), pelican, settings, settings.jobs[0]).run()


def collection_names(pelican):
    return sorted(c.name for c in pelican.collections.values())


def test_creates_missing_users_and_collections(pelican):
    report = run(pelican, [person("alice"), person("bob")])

    assert report.users_created == 2
    assert report.collections_created == 2
    assert report.exit_code == EXIT_OK
    assert sorted(u.username for u in pelican.users.values()) == ["alice", "bob"]
    assert collection_names(pelican) == ["alice", "bob"]

    create_calls = [c for c in pelican.calls if c[0] == "create_user"]
    assert ("create_user", "alice", "Alice", "http://cilogon.org/serverA/users/alice", "https://cilogon.org") in create_calls

    for c in pelican.collections.values():
        assert c.namespace == f"{BASE_PATH}/{c.name}"
        assert c.description == DESCRIPTION
        acls = pelican.acls[c.id]
        assert len(acls) == 1
        assert acls[0].role == "write"
        assert acls[0].subject_type == "user"
        owner = next(u for u in pelican.users.values() if u.username == c.name)
        assert acls[0].subject_id == owner.id


def test_collection_metadata_records_provenance(pelican):
    run(pelican, [person("alice")])
    call = next(c for c in pelican.calls if c[0] == "create_collection")
    metadata = call[5]
    assert metadata["provisioner.source"] == "ldap"
    assert metadata["provisioner.group"] == GROUP
    assert metadata["provisioner.username"] == "alice"


def test_existing_users_are_skipped_not_modified(pelican):
    """Same identity, different display name in LDAP: the Pelican record is left alone."""
    existing = pelican.add_user("alice")
    report = run(pelican, [person("alice", cn="Alice Renamed")])

    assert report.users_created == 0
    assert report.users_existing == 1
    assert not any(c[0] == "create_user" for c in pelican.calls)
    assert pelican.users[existing.id] == existing


def test_same_username_different_identity_is_a_different_person(pelican):
    """An existing 'alice' with another sub is not this LDAP member; they get 'alice2'."""
    pelican.add_user("alice", sub="old-sub")
    report = run(pelican, [person("alice", sub="new-sub")])

    assert report.users_created == 1
    assert {u.sub: u.username for u in pelican.users.values()} == {"old-sub": "alice", "new-sub": "alice2"}


def test_removes_collection_for_user_no_longer_in_group(pelican):
    alice = pelican.add_user("alice")
    bob = pelican.add_user("bob")
    ca = pelican.add_collection("alice")
    cb = pelican.add_collection("bob")
    pelican.acls[ca.id].append(acl(alice.id))
    pelican.acls[cb.id].append(acl(bob.id))

    report = run(pelican, [person("alice")])

    assert report.collections_deleted == 1
    assert report.collections_created == 0
    assert collection_names(pelican) == ["alice"]
    assert ("delete_collection", cb.id) in pelican.calls
    # bob the user is untouched
    assert bob.id in pelican.users


def test_base_path_is_managed_exclusively(pelican):
    """Anything under the base path that is not a member's correct collection is removed."""
    pelican.add_user("alice")
    hand_made = pelican.add_collection("carol", description="Carol's stuff", group=None)      # unmarked, under base
    copied_marker = pelican.add_collection("frank", group=None)                                # marker, no metadata
    other_job_here = pelican.add_collection("erin", group="ap41-login")                        # marker, other job
    at_base_itself = pelican.add_collection("base", namespace=BASE_PATH, description="x", group=None)

    report = run(pelican, [person("alice")], allow_mass_delete=True)

    assert report.collections_deleted == 4
    for c in (hand_made, copied_marker, other_job_here, at_base_itself):
        assert c.id not in pelican.collections
    assert collection_names(pelican) == ["alice"]


def test_outside_base_path_is_never_touched(pelican):
    pelican.add_user("alice")
    unmarked_outside = pelican.add_collection("carol", namespace="/other/carol", description="Carol's", group=None)
    other_job_outside = pelican.add_collection("dave", namespace="/other/dave", group="ap41-login")
    marker_no_meta_outside = pelican.add_collection("gina", namespace="/elsewhere/gina", group=None)

    report = run(pelican, [person("alice")])

    assert report.collections_deleted == 0
    for c in (unmarked_outside, other_job_outside, marker_no_meta_outside):
        assert c.id in pelican.collections


def test_duplicate_collections_reduced_to_one(pelican):
    alice = pelican.add_user("alice")
    first = pelican.add_collection("alice")
    second = pelican.add_collection("alice")
    pelican.acls[first.id].append(acl(alice.id))

    report = run(pelican, [person("alice")])

    assert report.collections_deleted == 1
    assert second.id not in pelican.collections and first.id in pelican.collections
    assert report.collections_created == 0


def test_repairs_missing_acl_on_existing_collection(pelican):
    alice = pelican.add_user("alice")
    pelican.add_collection("alice")  # no ACL rows

    report = run(pelican, [person("alice")])

    assert report.acls_repaired == 1
    assert report.collections_created == 0
    acls = pelican.acls[next(iter(pelican.collections))]
    assert acls == [acl(alice.id)]


def test_does_not_touch_correct_acl(pelican):
    alice = pelican.add_user("alice")
    c = pelican.add_collection("alice")
    pelican.acls[c.id].append(acl(alice.id))

    report = run(pelican, [person("alice")])

    assert report.acls_repaired == 0
    assert not any(call[0] == "grant_acl" for call in pelican.calls)


def test_failed_grant_cleans_up_new_collection(pelican):
    pelican.fail_grant_for.add("alice")

    report = run(pelican, [person("alice")])

    assert report.exit_code == EXIT_PARTIAL
    assert report.collections_created == 0
    assert collection_names(pelican) == []
    assert any(c[0] == "delete_collection" for c in pelican.calls)
    assert any("grant write on new collection alice" in e for e in report.errors)


def test_failed_user_create_is_reported_and_others_continue(pelican):
    pelican.fail_create_user.add("alice")

    report = run(pelican, [person("alice"), person("bob")])

    assert report.exit_code == EXIT_PARTIAL
    assert report.users_created == 1
    assert collection_names(pelican) == ["bob"]
    assert any(e.startswith("create user alice") for e in report.errors)


def test_skips_entries_missing_attributes_or_unsanitizable_usernames(pelican):
    people = [
        LdapPerson(dn="uid=1,ou=people", username=None, display_name="No Name", sub="s1"),
        LdapPerson(dn="uid=2,ou=people", username="nosub", display_name="No Sub", sub=None),
        LdapPerson(dn="uid=4,ou=people", username="x", display_name="Short", sub="s4"),
        LdapPerson(dn="uid=7,ou=people", username="../..", display_name="Punct", sub="s7"),
        person("good"),
    ]
    report = run(pelican, people)

    assert report.users_created == 1
    assert collection_names(pelican) == ["good"]
    assert len(report.errors) == 4


@pytest.mark.parametrize("raw, expected", [
    ("aaronp..wemhoff", "aaronp.wemhoff"),
    ("bockelman/admin", "bockelman_admin"),
    (".leading", "leading"),
    ("__x.y", "x.y"),
    ("a...b", "a.b"),
    ("Zoë", "Zo__"),            # byte-wise like the Go code: two UTF-8 bytes -> two underscores
    ("x", ""),                  # too short after sanitizing
    ("../..", ""),              # nothing alphanumeric left
    ("", ""),
    ("a" * 70, "a" * 64),
    ("plain.name-1@x", "plain.name-1@x"),
])
def test_sanitize_identifier_matches_pelican(raw, expected):
    assert sanitize_identifier(raw) == expected


def test_sanitized_username_keeps_raw_name_for_collection(pelican):
    report = run(pelican, [person("aaronp..wemhoff")])

    assert report.exit_code == EXIT_OK
    user = next(iter(pelican.users.values()))
    assert user.username == "aaronp.wemhoff"
    col = next(iter(pelican.collections.values()))
    assert col.name == "aaronp..wemhoff"
    assert col.namespace == f"{BASE_PATH}/aaronp..wemhoff"
    assert pelican.acls[col.id] == [acl(user.id)]
    call = next(c for c in pelican.calls if c[0] == "create_collection")
    assert call[5]["provisioner.username"] == "aaronp.wemhoff"
    assert call[5]["provisioner.ldap_name"] == "aaronp..wemhoff"

    # Second run is a no-op and still links the collection to the sanitized user.
    pelican.calls.clear()
    report2 = run(pelican, [person("aaronp..wemhoff")])
    assert report2.users_existing == 1
    assert report2.collections_created == 0
    assert report2.collections_deleted == 0
    assert not any(c[0] in {"create_user", "create_collection", "delete_collection", "grant_acl"} for c in pelican.calls)


def test_sanitized_user_collection_removed_when_they_leave(pelican):
    run(pelican, [person("aaronp..wemhoff"), person("alice")])
    report = run(pelican, [person("alice")])
    assert report.collections_deleted == 1
    assert collection_names(pelican) == ["alice"]


def test_same_ldap_value_two_identities_second_is_skipped(pelican):
    """Usernames can be numbered, but one collection path can only belong to one person."""
    people = [person("alice", sub="second"), person("alice", sub="first")]
    report = run(pelican, people)

    assert report.users_created == 1
    # Winner chosen by sub, not by the order LDAP returned the entries.
    assert {u.sub: u.username for u in pelican.users.values()} == {"first": "alice"}
    assert collection_names(pelican) == ["alice"]
    assert report.exit_code == EXIT_PARTIAL
    assert any("cannot share one collection path" in e for e in report.errors)


def test_empty_group_is_hard_error_by_default(pelican):
    pelican.add_collection("alice")
    with pytest.raises(HardError):
        run(pelican, [])
    assert collection_names(pelican) == ["alice"]


def test_empty_group_allowed_drains_namespace(pelican):
    pelican.add_collection("alice")
    report = run(pelican, [], allow_empty_group=True)
    assert report.collections_deleted == 1
    assert collection_names(pelican) == []


def test_dry_run_makes_no_writes(pelican):
    pelican.add_user("bob")
    pelican.add_collection("bob")  # ACL missing, would be repaired
    pelican.add_collection("gone")  # would be deleted

    report = run(pelican, [person("alice"), person("bob")], dry_run=True)

    writes = {"create_user", "create_collection", "delete_collection", "grant_acl"}
    assert not any(c[0] in writes for c in pelican.calls)
    assert report.users_created == 1
    assert report.collections_created == 1
    assert report.collections_deleted == 1
    assert report.acls_repaired == 1
    assert report.exit_code == EXIT_OK


def test_role_is_configurable(pelican):
    run(pelican, [person("alice")], collection_role="read")
    acl = next(a for acls in pelican.acls.values() for a in acls)
    assert acl.role == "read"


def test_replaces_collection_at_wrong_namespace(pelican):
    alice = pelican.add_user("alice")
    # Created by an earlier version at the shared base instead of /base/alice.
    wrong = pelican.add_collection("alice", namespace=BASE_PATH)
    pelican.acls[wrong.id].append(acl(alice.id))

    report = run(pelican, [person("alice")])

    assert report.collections_deleted == 1
    assert report.collections_created == 1
    assert wrong.id not in pelican.collections
    fixed = next(c for c in pelican.collections.values() if c.name == "alice")
    assert fixed.namespace == f"{BASE_PATH}/alice"
    assert pelican.acls[fixed.id] == [acl(alice.id)]


def test_stale_namespace_for_departed_user_is_just_deleted(pelican):
    pelican.add_user("alice")
    pelican.add_collection("bob", namespace=BASE_PATH)  # bob left, wrong ns anyway

    report = run(pelican, [person("alice")])

    assert report.collections_deleted == 1
    assert collection_names(pelican) == ["alice"]


def test_base_path_change_migrates_this_jobs_collections(pelican):
    """Collections created under an old base_path are moved, other jobs' are not, and the
    move is above the ceiling so it needs the explicit override."""
    alice = pelican.add_user("alice"); bob = pelican.add_user("bob")
    old_a = pelican.add_collection("alice", namespace="/ospool/ap40/alice")   # ours, old base
    old_b = pelican.add_collection("bob", namespace="/ospool/ap40/bob")
    foreign = pelican.add_collection("zed", namespace="/ospool/ap40/zed", group="ap41-login")
    pelican.acls[old_a.id].append(acl(alice.id)); pelican.acls[old_b.id].append(acl(bob.id))
    members = [person("alice"), person("bob")]

    with pytest.raises(HardError, match="refusing to delete 2 of 2"):
        run(pelican, members)
    assert old_a.id in pelican.collections and old_b.id in pelican.collections  # nothing happened

    report = run(pelican, members, allow_mass_delete=True)

    assert report.collections_deleted == 2
    assert report.collections_created == 2
    assert foreign.id in pelican.collections
    moved = {c.name: c for c in pelican.collections.values() if c.name in ("alice", "bob")}
    assert moved["alice"].namespace == f"{BASE_PATH}/alice"
    assert pelican.acls[moved["alice"].id] == [acl(alice.id)]


def test_job_group_is_what_gets_queried(pelican):
    settings = make_settings()
    ldap = FakeLdap({GROUP: [person("alice")], "other": [person("bob")]})
    JobRunner(ldap, pelican, settings, settings.jobs[0]).run()
    assert ldap.queried == [GROUP]
    assert collection_names(pelican) == ["alice"]


# ---------------------------------------------------------------- deletion ceiling

def _twenty_members(pelican):
    people = [person(f"user{i:02d}") for i in range(20)]
    for p in people:
        u = pelican.add_user(p.username, sub=p.sub)
        c = pelican.add_collection(p.username)
        pelican.acls[c.id].append(acl(u.id))
    return people


def test_ceiling_allows_a_single_departure(pelican):
    people = _twenty_members(pelican)
    report = run(pelican, people[1:])          # 1 of 20 = 5%, floor(1.0) = 1 allowed
    assert report.collections_deleted == 1


def test_ceiling_blocks_two_of_twenty(pelican):
    people = _twenty_members(pelican)
    with pytest.raises(HardError, match=r"refusing to delete 2 of 20 .*ceiling 1, 5%"):
        run(pelican, people[2:])
    assert len(pelican.collections) == 20
    assert not any(c[0] == "delete_collection" for c in pelican.calls)


def test_ceiling_checked_before_any_change(pelican):
    """A blocked run must not have created or granted anything either."""
    people = _twenty_members(pelican)
    newcomer = person("zed")
    with pytest.raises(HardError):
        run(pelican, people[2:] + [newcomer])
    assert not any(c[0] in {"create_user", "create_collection", "grant_acl", "delete_collection"} for c in pelican.calls)


def test_ceiling_override_and_fraction(pelican):
    people = _twenty_members(pelican)
    report = run(pelican, people[2:], allow_mass_delete=True)
    assert report.collections_deleted == 2

    pelican2 = type(pelican)()
    people = _twenty_members(pelican2)
    report = run(pelican2, people[3:], max_delete_fraction=0.15)   # floor(3.0) = 3
    assert report.collections_deleted == 3


def test_ceiling_always_allows_one_on_tiny_deployments(pelican):
    alice = pelican.add_user("alice"); pelican.add_user("bob")
    ca = pelican.add_collection("alice"); cb = pelican.add_collection("bob")
    pelican.acls[ca.id].append(acl(alice.id))
    report = run(pelican, [person("alice")])   # 1 of 2 = 50% but a single delete is fine
    assert report.collections_deleted == 1


def test_dry_run_still_reports_ceiling(pelican):
    people = _twenty_members(pelican)
    with pytest.raises(HardError):
        run(pelican, people[2:], dry_run=True)


# ---------------------------------------------------------------- username collisions

def test_collision_gets_numbered_suffix_deterministically(pelican):
    # 'a..b' and 'a.b' both sanitize to 'a.b'; the exact match keeps the plain name.
    exact = person("a.b", sub="sub-exact")
    dotted = person("a..b", sub="sub-dotted")

    report = run(pelican, [dotted, exact])     # order in LDAP results must not matter

    names = {u.sub: u.username for u in pelican.users.values()}
    assert names == {"sub-exact": "a.b", "sub-dotted": "a.b2"}
    assert report.exit_code == EXIT_OK
    cols = {c.name: c for c in pelican.collections.values()}
    assert set(cols) == {"a.b", "a..b"}
    assert cols["a..b"].namespace == f"{BASE_PATH}/a..b"
    assert pelican.acls[cols["a..b"].id][0].subject_id == next(u.id for u in pelican.users.values() if u.username == "a.b2")


def test_numbered_name_is_stable_when_collider_leaves(pelican):
    exact = person("a.b", sub="sub-exact")
    dotted = person("a..b", sub="sub-dotted")
    run(pelican, [exact, dotted])

    # exact leaves; dotted must keep 'a.b2' (matched by sub), not be renamed to 'a.b'.
    report = run(pelican, [dotted])

    assert {u.username for u in pelican.users.values()} == {"a.b", "a.b2"}   # users are never deleted
    assert report.users_created == 0
    assert report.users_existing == 1
    assert collection_names(pelican) == ["a..b"]


def test_collision_with_unrelated_existing_pelican_user(pelican):
    pelican.add_user("alice", sub="someone-else-entirely")
    report = run(pelican, [person("alice", sub="ldap-alice")])
    created = next(u for u in pelican.users.values() if u.sub == "ldap-alice")
    assert created.username == "alice2"
    assert report.exit_code == EXIT_OK


def test_adopts_username_of_user_already_linked_by_sub(pelican):
    """Someone who logged in via CILogon first was auto-enrolled under a different name."""
    pelican.add_user("alice_at_wisc", sub=person("alice").sub)
    report = run(pelican, [person("alice")])
    assert report.users_created == 0 and report.users_existing == 1
    col = next(iter(pelican.collections.values()))
    assert col.name == "alice"                                # collection keeps the LDAP name
    assert pelican.acls[col.id][0].subject_id == next(iter(pelican.users.values())).id


def test_same_sub_twice_in_group_is_an_error(pelican):
    report = run(pelican, [person("alice", sub="dup"), person("bob", sub="dup")])
    assert report.users_created == 1
    assert any("same uid" in e for e in report.errors)


def test_with_suffix_respects_length_limit():
    base = "a" * 64
    assert with_suffix(base, 7) == "a" * 63 + "7"
    assert with_suffix(base, 12) == "a" * 62 + "12"
    assert len(with_suffix(base, 123)) == 64
