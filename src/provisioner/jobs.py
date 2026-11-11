"""The two job kinds and the command-line entry point.

Both kinds share the same phases, in an order chosen so that a refused run has changed
nothing: read LDAP and the Origin, resolve usernames, plan the collections and check the
deletion ceiling, and only then create users, groups, and collections.
"""

from __future__ import annotations

import logging
import sys

from provisioner.collections import CollectionReconciler, Grant
from provisioner.config import ConfigError, Job, Settings, load_settings
from provisioner.groups import GroupProvisioner
from provisioner.identifiers import path_segment_problem
from provisioner.ldap_client import LdapClient, LdapError, LdapPerson
from provisioner.logging_setup import setup_logging
from provisioner.pelican_client import PelicanClient, PelicanError, PelicanUser
from provisioner.projects import fetch_project_names
from provisioner.report import EXIT_HARD, HardError, SyncReport
from provisioner.users import UserProvisioner

log = logging.getLogger(__name__)


class JobRunner:
    def __init__(self, ldap, pelican, settings: Settings, job: Job) -> None:
        self._ldap = ldap
        self._pelican = pelican
        self._settings = settings
        self._job = job
        self.report = SyncReport()
        dry_run = settings.run.dry_run
        self._users = UserProvisioner(pelican, settings.ldap, settings.pelican, self.report, dry_run)
        self._groups = GroupProvisioner(pelican, settings.pelican, self.report, dry_run)
        self._collections = CollectionReconciler(pelican, settings, job, self.report)

    def run(self) -> SyncReport:
        log.info("Job (%s): LDAP group %s -> %s/<%s>", self._job.kind, self._job.group,
                 self._job.base_path.rstrip("/"), "user" if self._job.kind == "users" else "project")
        if self._job.kind == "users":
            self._run_users()
        else:
            self._run_projects()
        return self.report

    # -- shared ----------------------------------------------------------------------

    def _members(self) -> list[LdapPerson]:
        try:
            return self._ldap.group_members(self._job.group)
        except LdapError as exc:
            raise HardError(str(exc)) from exc

    def _existing_users(self) -> list[PelicanUser]:
        try:
            return self._pelican.list_users()
        except PelicanError as exc:
            raise HardError(f"Could not list Pelican users: {exc}") from exc

    def _refuse_if_empty(self, what: str, count: int) -> None:
        if count == 0 and not self._settings.ldap.allow_empty_group:
            raise HardError(
                f"LDAP group {self._job.group!r} resolved to zero {what}. Refusing to continue because that "
                "would delete every managed collection. Set LDAP_ALLOW_EMPTY_GROUP=true if this is intended."
            )

    # -- kind: users -----------------------------------------------------------------

    def _run_users(self) -> None:
        """One personal collection per member, granted to the member's own user."""
        existing_users = self._existing_users()
        desired = self._users.desired_users(self._members(), existing_users)
        # The raw LDAP value becomes a path under the base path, so it must be one segment.
        for username, u in list(desired.items()):
            if problem := path_segment_problem(u.ldap_name):
                attr = self._settings.ldap.username_attribute
                self.report.error(f"{attr}={u.ldap_name!r} {problem}; cannot name a collection, skipped")
                del desired[username]
        self._refuse_if_empty("usable members", len(desired))
        log.info("Desired users: %d", len(desired))

        plan = self._collections.plan({u.ldap_name for u in desired.values()})
        user_ids = self._users.ensure_users(desired, existing_users)

        grants = {u.ldap_name: Grant("user", user_ids[u.username]) for u in desired.values() if u.username in user_ids}
        metadata = {u.ldap_name: {"provisioner.ldap_name": u.ldap_name, "provisioner.username": u.username}
                    for u in desired.values()}
        self._collections.apply(plan, grants, metadata)

    # -- kind: projects --------------------------------------------------------------

    def _run_projects(self) -> None:
        """One collection per project with members, granted to a group of those members."""
        assert self._job.projects_url  # enforced by Job
        project_names = fetch_project_names(self._job.projects_url, self._settings.pelican.timeout_seconds)
        existing_users = self._existing_users()
        members = self._members()
        self._refuse_if_empty("members", len(members))

        # Only members of at least one known project need a Pelican user.
        in_projects = [p for p in members if p.groups & project_names.keys()]
        desired = self._users.desired_users(in_projects, existing_users)
        wanted: dict[str, set[str]] = {}          # project -> usernames
        for u in desired.values():
            for project in u.groups & project_names.keys():
                wanted.setdefault(project, set()).add(u.username)
        for project in sorted(wanted):
            if problem := path_segment_problem(project):
                self.report.error(f"project {project!r} {problem}; cannot name a collection, skipped")
                del wanted[project]
        self._refuse_if_empty("project members", len(wanted))
        log.info("Projects with members: %d; members in projects: %d of %d", len(wanted), len(desired), len(members))

        # Every check that can refuse the run happens before the first write.
        plan = self._collections.plan(set(wanted))
        existing_groups = self._groups.list_groups()
        me = self._groups.my_user_id()
        self._groups.plan_replacements(set(wanted), existing_groups, me, self._settings.run)

        user_ids = self._users.ensure_users(desired, existing_users)
        member_ids = {project: {user_ids[n] for n in names if n in user_ids} for project, names in wanted.items()}
        group_ids = self._groups.ensure_groups(member_ids, project_names, existing_groups)

        grants = {project: Grant("group", gid) for project, gid in group_ids.items()}
        metadata = {project: {"provisioner.project": project, "provisioner.pelican_group": project} for project in wanted}
        self._collections.apply(plan, grants, metadata)
        self._groups.delete_orphaned_groups(set(wanted), existing_groups)


def main() -> None:
    try:
        settings = load_settings()
    except ConfigError as exc:
        setup_logging()
        log.error("%s", exc)
        sys.exit(EXIT_HARD)

    setup_logging(settings.run.log_level, settings.run.log_format)
    if settings.run.dry_run:
        log.info("DRY RUN: no changes will be made")

    ldap = LdapClient(settings.ldap)
    pelican = PelicanClient(settings.pelican)
    total = SyncReport()
    try:
        for job in settings.jobs:
            report = JobRunner(ldap, pelican, settings, job).run()
            log.info("Job %s complete: %s", job.group, report.summary())
            total.merge(report)
    except HardError as exc:
        log.error("Hard error, run aborted: %s", exc)
        sys.exit(EXIT_HARD)
    except Exception:
        # Without this an unexpected exception exits 1, the code for "some users failed".
        log.exception("Unexpected error, run aborted")
        sys.exit(EXIT_HARD)
    finally:
        ldap.close()
        pelican.close()

    log.info("Run complete (%d jobs): %s", len(settings.jobs), total.summary())
    sys.exit(total.exit_code)


if __name__ == "__main__":
    main()
