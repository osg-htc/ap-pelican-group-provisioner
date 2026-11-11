"""A run is a sequence of independent API calls, not a transaction. These tests kill the
run just before each of its writes in turn and check two things:

* safety: the half-applied state never grants anyone access to a project they are in
  neither before nor after the change;
* recovery: the next complete run reaches exactly the intended state, and a run after that
  makes no writes.

Temporary loss of access for someone who should keep it is expected after a crash and is
not checked; the next run restores it.
"""

from __future__ import annotations

import pytest

from provisioner.report import EXIT_OK

from origin_harness import admin, admin_token, make_scenario, pelican_settings, scenario  # noqa: F401  (fixtures)
from origin_harness import SimulatedCrash


pytestmark = pytest.mark.origin

RUN = {"max_delete_fraction": 1.0}
MAX_WRITES = 16


def arrange_before(s) -> None:
    s.projects("ACE", "Evol", "Gen")
    s.member("alice", "ACE")
    s.member("bob", "ACE", "Evol")
    s.member("carol", "Gen")


def arrange_after(s) -> None:
    """Every kind of change at once: a new user joins, a user moves, a project empties,
    a new project appears, a user leaves entirely."""
    s.projects("ACE", "Evol", "Gen", "New")
    s.member("alice", "ACE", "New")      # gains New
    s.member("bob", "Gen")               # leaves ACE and Evol (Evol empties), joins Gen
    s.leave("carol")                     # gone
    s.member("dave", "ACE", "New")       # brand new user


EXPECTED = {"ACE": {"alice", "dave"}, "Gen": {"bob"}, "New": {"alice", "dave"}}


def assert_safe(before: dict[str, set[str]], after: dict[str, set[str]], seen: dict[str, set[str]]) -> None:
    for project, who in seen.items():
        allowed = before.get(project, set()) | after.get(project, set())
        assert who <= allowed, f"{project}: {sorted(who - allowed)} have access they never should"


def test_transition_without_crash(scenario):
    """Baseline, and a guard that MAX_WRITES covers every write of the transition."""
    arrange_before(scenario)
    scenario.sync(**RUN)
    arrange_after(scenario)

    report = scenario.sync(**RUN)

    assert report.exit_code == EXIT_OK, report.errors
    assert scenario.access() == EXPECTED
    assert 0 < len(scenario.last_writes) < MAX_WRITES, scenario.last_writes


@pytest.mark.parametrize("crash_before", range(MAX_WRITES))
def test_crash_mid_run_is_safe_and_recovers(scenario, crash_before):
    arrange_before(scenario)
    scenario.sync(**RUN)
    before = scenario.access()
    arrange_after(scenario)

    try:
        scenario.sync(crash_before=crash_before, **RUN)
    except SimulatedCrash:
        pass
    else:
        pytest.skip(f"the transition has fewer than {crash_before + 1} writes")
    crashed_at = scenario.last_writes

    assert_safe(before, EXPECTED, scenario.access())

    report = scenario.sync(**RUN)
    assert report.exit_code == EXIT_OK, (crashed_at, report.errors)
    assert scenario.access() == EXPECTED, crashed_at
    for name in EXPECTED:
        assert scenario.acl_count(name) == 1, (name, crashed_at)
    assert set(scenario.groups()) == set(EXPECTED), crashed_at

    again = scenario.sync(**RUN)
    assert scenario.last_writes == [], (crashed_at, again)


@pytest.mark.parametrize("crash_before", range(MAX_WRITES))
def test_crash_during_first_run_is_safe_and_recovers(scenario, crash_before):
    """Covers the window between creating a collection and granting it: the next run must
    grant the existing collection rather than leave it unusable or make a second one."""
    scenario.projects("ACE", "Evol")
    scenario.member("alice", "ACE")
    scenario.member("bob", "ACE", "Evol")
    expected = {"ACE": {"alice", "bob"}, "Evol": {"bob"}}

    try:
        scenario.sync(crash_before=crash_before)
    except SimulatedCrash:
        pass
    else:
        pytest.skip(f"a first run has fewer than {crash_before + 1} writes")
    crashed_at = scenario.last_writes
    assert_safe({}, expected, scenario.access())

    report = scenario.sync()
    assert report.exit_code == EXIT_OK, (crashed_at, report.errors)
    assert scenario.access() == expected, crashed_at
    for name in expected:
        assert scenario.acl_count(name) == 1, (name, crashed_at)
    scenario.sync()
    assert scenario.last_writes == [], crashed_at
