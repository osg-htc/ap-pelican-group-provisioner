"""Keep one Pelican group per project equal to the project's members."""

from __future__ import annotations

import logging
import math

from provisioner.config import PelicanSettings, RunSettings
from provisioner.pelican_client import PelicanError, PelicanGroup
from provisioner.report import HardError, SyncReport

log = logging.getLogger(__name__)


class GroupProvisioner:
    """Groups are matched by name. An existing project group is reused only if it is one
    the provisioner could have made itself: created through the groups API (source
    "pelican") and controlled by the provisioner's own account. Its membership is then made
    exactly the project's members, whatever each member's source.

    Any other project group is deleted and recreated: one Pelican made from a login claim
    or another provider (source "oidc", "file", "github", "unknown"), one owned by another
    account, or one with another user or group as its group admin. Its owner, admin or
    provider could otherwise decide who is in it, and so who gets the project's collection.
    Pelican never mirrors provider memberships into a "pelican" group, so after replacement
    every member is one this script added.
    """

    def __init__(self, pelican, settings: PelicanSettings, report: SyncReport, dry_run: bool) -> None:
        self._pelican = pelican
        self._marker = settings.collection_description
        self.report = report
        self._dry_run = dry_run
        self._replace: set[str] = set()

    def list_groups(self) -> dict[str, PelicanGroup]:
        try:
            groups = self._pelican.list_groups()
        except PelicanError as exc:
            raise HardError(f"Could not list Pelican groups: {exc}") from exc
        by_name: dict[str, PelicanGroup] = {}
        for g in groups:
            if g.name in by_name:
                log.warning("Two groups named %s (%s, %s); using the first", g.name, by_name[g.name].id, g.id)
                continue
            by_name[g.name] = g
        return by_name

    def my_user_id(self) -> str:
        try:
            me = self._pelican.get_me()
        except PelicanError as exc:
            raise HardError(f"Could not identify the provisioner's own Pelican account (GET /me): {exc}") from exc
        if not me.id:
            raise HardError("GET /me returned no user id")
        return me.id

    @staticmethod
    def who_else_controls(group: PelicanGroup, me: str) -> str | None:
        """Why someone other than `me` (or a system admin) decides this group's membership,
        or None when only `me` does."""
        # An empty source means a Pelican too old to report one; ownership still applies.
        if group.source and group.source != "pelican":
            return f"from the {group.source!r} group source, not created by the provisioner"
        if group.owner_id != me:
            return f"owned by {group.owner_id or 'nobody'}"
        if group.admin_id and not (group.admin_type == "user" and group.admin_id == me):
            return f"group admin is {group.admin_type or 'user'} {group.admin_id}"
        return None

    def plan_replacements(self, wanted: set[str], existing: dict[str, PelicanGroup], me: str,
                          run: RunSettings) -> None:
        """Decide which project groups must be replaced, and refuse too many at once. Read-only.

        Replacing changes the group's ID and briefly removes the project's grant, so it gets
        the same ceiling as collection deletions. The likely cause of a mass replacement is
        not a takeover but the provisioner running as a different account than before.
        """
        self._replace = set()
        in_use = sorted(name for name in wanted if name in existing)
        for name in in_use:
            why = self.who_else_controls(existing[name], me)
            if why is not None:
                log.warning("Group %s (id=%s) is %s; it will be deleted and recreated", name, existing[name].id, why)
                self._replace.add(name)
        if not self._replace:
            return
        allowed = max(1, math.floor(run.max_delete_fraction * len(in_use)))
        if len(self._replace) <= allowed:
            return
        message = (f"refusing to replace {len(self._replace)} of {len(in_use)} project groups not controlled by "
                   f"the provisioner's account {me} (ceiling {allowed}, {run.max_delete_fraction:.0%})")
        if run.allow_mass_delete:
            log.warning("%s; proceeding because ALLOW_MASS_DELETE is set", message)
            return
        raise HardError(message + ". Check the account the provisioner runs as; set ALLOW_MASS_DELETE=true "
                                  "for one run if this is intended.")

    def ensure_groups(self, wanted: dict[str, set[str]], display_names: dict[str, str],
                      existing: dict[str, PelicanGroup]) -> dict[str, str]:
        """Replace groups plan_replacements flagged, create missing groups and reconcile
        membership. Returns name -> group id."""
        group_ids: dict[str, str] = {}
        for name in sorted(wanted):
            group = existing.get(name)
            if group is not None and name in self._replace:
                if not self._delete_for_replacement(name, group):
                    continue          # never grant to a group someone else controls
                group = None
            if group is None:
                group_id = self._create_group(name, display_names.get(name, name))
                if group_id is None:
                    continue
                current: dict[str, str] = {}
            else:
                group_id = group.id
                current = {m.user_id: m.source for m in group.members}
            group_ids[name] = group_id
            self._sync_members(name, group_id, wanted[name], current)
        return group_ids

    def delete_orphaned_groups(self, wanted: set[str], existing: dict[str, PelicanGroup]) -> None:
        """Remove groups this script created (marker description) whose project is gone.
        Groups without the marker were made by someone else and are left alone."""
        for name, group in sorted(existing.items()):
            if name in wanted or group.description != self._marker:
                continue
            if self._dry_run:
                log.info("DRY RUN: would delete group %s (id=%s): project no longer present", name, group.id)
                self.report.groups_deleted += 1
                continue
            try:
                self._pelican.delete_group(group.id)
            except PelicanError as exc:
                self.report.error(f"delete group {name} ({group.id}): {exc}")
                continue
            log.info("Deleted group %s (id=%s): project no longer present", name, group.id)
            self.report.groups_deleted += 1

    def _delete_for_replacement(self, name: str, group: PelicanGroup) -> bool:
        if self._dry_run:
            log.info("DRY RUN: would delete group %s (id=%s) to recreate it", name, group.id)
            self.report.groups_replaced += 1
            return True
        try:
            self._pelican.delete_group(group.id)
        except PelicanError as exc:
            self.report.error(f"delete group {name} ({group.id}) for replacement: {exc}; project skipped")
            return False
        log.info("Deleted group %s (id=%s) to recreate it under the provisioner's account", name, group.id)
        self.report.groups_replaced += 1
        return True

    def _create_group(self, name: str, display_name: str) -> str | None:
        if self._dry_run:
            log.info("DRY RUN: would create group %s", name)
            self.report.groups_created += 1
            return f"dry-run:group:{name}"
        try:
            created = self._pelican.create_group(name, display_name, self._marker)
        except PelicanError as exc:
            self.report.error(f"create group {name}: {exc}")
            return None
        log.info("Created group %s (id=%s)", name, created.id)
        self.report.groups_created += 1
        return created.id

    def _sync_members(self, name: str, group_id: str, wanted_ids: set[str], current: dict[str, str]) -> None:
        for user_id in sorted(wanted_ids - set(current)):
            if self._dry_run:
                log.info("DRY RUN: would add user %s to group %s", user_id, name)
                self.report.members_added += 1
                continue
            try:
                self._pelican.add_group_member(group_id, user_id)
            except PelicanError as exc:
                self.report.error(f"add user {user_id} to group {name}: {exc}")
                continue
            self.report.members_added += 1
        for user_id in sorted(set(current) - wanted_ids):
            if self._dry_run:
                log.info("DRY RUN: would remove user %s from group %s", user_id, name)
                self.report.members_removed += 1
                continue
            try:
                self._pelican.remove_group_member(group_id, user_id)
            except PelicanError as exc:
                self.report.error(f"remove user {user_id} from group {name}: {exc}")
                continue
            self.report.members_removed += 1
