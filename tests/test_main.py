"""The sync-collections entry point: job order, combined report, exit codes."""

from __future__ import annotations

import pytest

from provisioner import jobs as jobs_module
from provisioner.config import ConfigError, Job
from provisioner.report import EXIT_HARD, EXIT_OK, EXIT_PARTIAL, HardError, SyncReport

from conftest import make_settings

JOBS = [Job(kind="users", group="first", base_path="/a"), Job(kind="users", group="second", base_path="/b")]


class Closable:
    def __init__(self, settings) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def harness(monkeypatch):
    """Replace settings, clients and JobRunner; `outcomes` maps job group -> report or exception."""
    state = {"outcomes": {}, "ran": [], "clients": []}

    def make_client(settings):
        c = Closable(settings)
        state["clients"].append(c)
        return c

    class FakeRunner:
        def __init__(self, ldap, pelican, settings, job) -> None:
            self.job = job

        def run(self):
            state["ran"].append(self.job.group)
            outcome = state["outcomes"].get(self.job.group, SyncReport())
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    monkeypatch.setattr(jobs_module, "load_settings", lambda: make_settings(jobs=list(JOBS)))
    monkeypatch.setattr(jobs_module, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(jobs_module, "LdapClient", make_client)
    monkeypatch.setattr(jobs_module, "PelicanClient", make_client)
    monkeypatch.setattr(jobs_module, "JobRunner", FakeRunner)
    return state


def exit_code() -> int:
    with pytest.raises(SystemExit) as info:
        jobs_module.main()
    return info.value.code


def test_all_jobs_clean_exits_0(harness):
    assert exit_code() == EXIT_OK
    assert harness["ran"] == ["first", "second"]
    assert all(c.closed for c in harness["clients"])


def test_errors_in_any_job_exit_1_after_running_every_job(harness):
    partial = SyncReport()
    partial.error("create user x: boom")
    harness["outcomes"]["first"] = partial

    assert exit_code() == EXIT_PARTIAL
    assert harness["ran"] == ["first", "second"]


def test_hard_error_exits_2_and_skips_later_jobs(harness):
    """Current behavior: a hard error in one job (e.g. Topology down) stops the jobs after it."""
    harness["outcomes"]["first"] = HardError("feed down")

    assert exit_code() == EXIT_HARD
    assert harness["ran"] == ["first"]
    assert all(c.closed for c in harness["clients"])


def test_unexpected_exception_exits_2_not_1(harness):
    """Python's default for an uncaught exception is 1, which means 'some users failed'."""
    harness["outcomes"]["second"] = KeyError("id")

    assert exit_code() == EXIT_HARD
    assert all(c.closed for c in harness["clients"])


def test_config_error_exits_2(harness, monkeypatch):
    def broken():
        raise ConfigError("Configuration errors: ...")

    monkeypatch.setattr(jobs_module, "load_settings", broken)
    assert exit_code() == EXIT_HARD
    assert harness["ran"] == []


def test_reports_are_combined(harness, caplog):
    a, b = SyncReport(collections_created=2), SyncReport(collections_created=3, collections_deleted=1)
    harness["outcomes"].update(first=a, second=b)

    with caplog.at_level("INFO"):
        assert exit_code() == EXIT_OK
    assert "Run complete (2 jobs): users_existing=0 collections_created=5 collections_deleted=1 errors=0" in caplog.text
