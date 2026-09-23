from __future__ import annotations

import pytest

from provisioner import jobs as jobs_module
from provisioner.config import Job
from provisioner.jobs import JobRunner
from provisioner.pelican_client import GroupMember, PelicanError, PelicanGroup
from provisioner.report import EXIT_OK, EXIT_PARTIAL, HardError

from conftest import (
    DESCRIPTION,
    PROJECTS_BASE,
    PROJECTS_GROUP,
    PROJECTS_URL,
    FakeLdap,
    acl,
    make_settings,
    person,
)

PROJECTS = {"ACE_NIAID": "ACE_NIAID", "EvolSims": "EvolSims", "Empty_Project": "Empty Project"}


@pytest.fixture(autouse=True)
def project_feed(monkeypatch):
    """Replace the HTTP fetch with the fixed project list above."""
    feed = {"names": dict(PROJECTS), "fail": False}

    def fake_fetch(url, timeout_seconds=30):
        assert url == PROJECTS_URL
        if feed["fail"]:
            raise HardError("feed down")
        return dict(feed["names"])

    monkeypatch.setattr(jobs_module, "fetch_project_names", fake_fetch)
    return feed


def projects_job(**overrides):
    return Job(kind="projects", group=PROJECTS_GROUP, base_path=PROJECTS_BASE, projects_url=PROJECTS_URL, **overrides)


def run(pelican, people, **settings_overrides):
    settings = make_settings(jobs=[projects_job()], **settings_overrides)
    return JobRunner(FakeLdap({PROJECTS_GROUP: people}), pelican, settings, settings.jobs[0]).run()


def names(pelican):
    return sorted(c.name for c in pelican.collections.values())


ALICE = person("alice", groups=["CO:members:active", "ACE_NIAID", "ospool-login"])
BOB = person("bob", groups=["CO:members:active", "ACE_NIAID", "EvolSims"])
CAROL = person("carol", groups=["CO:members:active", "Unknown_Project"])   # no known project


def test_creates_group_and_collection_per_project_with_members(pelican):
    report = run(pelican, [ALICE, BOB, CAROL])

    assert report.exit_code == EXIT_OK
    # Only members of known projects become Pelican users.
    assert sorted(u.username for u in pelican.users.values()) == ["alice", "bob"]
    # One collection per project that has members; Empty_Project gets nothing.
    assert names(pelican) == ["ACE_NIAID", "EvolSims"]
    for c in pelican.collections.values():
        assert c.namespace == f"{PROJECTS_BASE}/{c.name}"
        assert c.description == DESCRIPTION
        group = pelican.group_named(c.name)
        assert group is not None and group.description == DESCRIPTION
        assert pelican.acls[c.id] == [acl(group.id, subject_type="group")]
    ids = {u.username: u.id for u in pelican.users.values()}
    assert {m.user_id for m in pelican.group_named("ACE_NIAID").members} == {ids["alice"], ids["bob"]}
    assert {m.user_id for m in pelican.group_named("EvolSims").members} == {ids["bob"]}
    assert report.groups_created == 2 and report.members_added == 3 and report.collections_created == 2


def test_collection_metadata_records_kind_and_project(pelican):
    run(pelican, [ALICE])
    call = next(c for c in pelican.calls if c[0] == "create_collection")
    meta = call[5]
    assert meta["provisioner.kind"] == "projects"
    assert meta["provisioner.group"] == PROJECTS_GROUP
    assert meta["provisioner.project"] == "ACE_NIAID"


def test_second_run_is_a_no_op(pelican):
    run(pelican, [ALICE, BOB])
    pelican.calls.clear()
    report = run(pelican, [ALICE, BOB])
    assert pelican.writes() == []
    assert report.exit_code == EXIT_OK


def test_reuses_group_pelican_created_from_oidc_claims(pelican):
    alice = pelican.add_user("alice")
    existing = pelican.add_group("ACE_NIAID", members=[alice.id], source="oidc")   # no marker, oidc-sourced

    report = run(pelican, [ALICE, BOB])

    assert pelican.group_named("ACE_NIAID").id == existing.id                       # reused, not recreated
    assert not any(c[0] == "create_group" and c[1] == "ACE_NIAID" for c in pelican.calls)
    assert report.groups_created == 1                                               # only EvolSims, bob's other project
    bob = next(u for u in pelican.users.values() if u.username == "bob")
    assert {m.user_id for m in pelican.group_named("ACE_NIAID").members} == {alice.id, bob.id}
    assert ("add_group_member", existing.id, bob.id) in pelican.calls
    assert not any(c == ("add_group_member", existing.id, alice.id) for c in pelican.calls)   # already a member


def test_membership_is_reconciled_but_oidc_rows_are_kept(pelican):
    alice = pelican.add_user("alice"); bob = pelican.add_user("bob"); zed = pelican.add_user("zed")
    # bob was added by an earlier run (api-sourced) but has left the project; zed's row was
    # asserted by Pelican from a login claim (oidc-sourced) and is not in the LDAP project.
    group = pelican.add_group("ACE_NIAID", members=[bob.id], description=DESCRIPTION, source="api")
    pelican.groups[group.id] = PelicanGroup(
        id=group.id, name=group.name, description=group.description,
        members=group.members + (GroupMember(user_id=zed.id, source="oidc"),),
    )

    report = run(pelican, [ALICE])

    members = {m.user_id: m.source for m in pelican.group_named("ACE_NIAID").members}
    assert alice.id in members                      # added
    assert bob.id not in members                    # removed: api-sourced and not in the project
    assert zed.id in members                        # kept: oidc-sourced
    assert report.members_added == 1 and report.members_removed == 1


def test_departed_project_collection_and_marker_group_removed(pelican):
    run(pelican, [ALICE, BOB])          # ACE_NIAID (alice, bob), EvolSims (bob)
    report = run(pelican, [ALICE])      # bob leaves: EvolSims has no members

    assert names(pelican) == ["ACE_NIAID"]
    assert pelican.group_named("EvolSims") is None
    assert pelican.group_named("ACE_NIAID") is not None
    assert report.collections_deleted == 1 and report.groups_deleted == 1
    # bob the user is never deleted
    assert any(u.username == "bob" for u in pelican.users.values())


def test_unmarked_group_is_never_deleted(pelican):
    pelican.add_group("EvolSims", members=[], source="oidc")   # Pelican's own, no marker
    report = run(pelican, [ALICE])                              # EvolSims has no members
    assert pelican.group_named("EvolSims") is not None
    assert report.groups_deleted == 0


def test_project_feed_failure_is_hard_error_before_any_change(pelican, project_feed):
    pelican.add_collection("ACE_NIAID", namespace=f"{PROJECTS_BASE}/ACE_NIAID", group=PROJECTS_GROUP)
    project_feed["fail"] = True
    with pytest.raises(HardError, match="feed down"):
        run(pelican, [ALICE])
    assert pelican.writes() == []


def test_no_members_in_any_project_is_hard_error(pelican):
    pelican.add_collection("ACE_NIAID", namespace=f"{PROJECTS_BASE}/ACE_NIAID", group=PROJECTS_GROUP)
    with pytest.raises(HardError, match="zero project members"):
        run(pelican, [CAROL])
    assert names(pelican) == ["ACE_NIAID"]


def test_dry_run_makes_no_writes(pelican):
    report = run(pelican, [ALICE, BOB], dry_run=True)
    assert pelican.writes() == []
    assert report.collections_created == 2 and report.groups_created == 2 and report.users_created == 2


def test_users_job_and_projects_job_do_not_touch_each_other(pelican):
    """Same Origin, different base paths: a users job's collections are outsiders to the
    projects job and vice versa, even with the same marker description."""
    users_settings = make_settings()
    JobRunner(FakeLdap([ALICE]), pelican, users_settings, users_settings.jobs[0]).run()
    before = names(pelican)

    report = run(pelican, [ALICE, BOB])

    assert names(pelican) == sorted(before + ["ACE_NIAID", "EvolSims"])
    assert report.collections_deleted == 0


def test_group_create_failure_leaves_project_for_next_run(pelican):
    original = pelican.create_group

    def failing(name, display_name, description):
        if name == "EvolSims":
            raise PelicanError("boom", status=500)
        return original(name, display_name, description)

    pelican.create_group = failing
    report = run(pelican, [ALICE, BOB])

    assert report.exit_code == EXIT_PARTIAL
    assert names(pelican) == ["ACE_NIAID"]                  # no collection without a grantable group
    assert any(e.startswith("create group EvolSims") for e in report.errors)


def test_projects_job_requires_url():
    with pytest.raises(ValueError, match="projects_url"):
        Job(kind="projects", group="g", base_path="/p")
    assert Job(group="g", base_path="/p").kind == "users"
