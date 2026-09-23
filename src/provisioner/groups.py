"""Keep one Pelican group per project equal to the project's members."""

from __future__ import annotations

import logging

from provisioner.config import PelicanSettings
from provisioner.pelican_client import PelicanError, PelicanGroup
from provisioner.report import HardError, SyncReport

log = logging.getLogger(__name__)


class GroupProvisioner:
    """Groups are matched by name. A group that already exists, including one Pelican
    created from an OIDC claim, is reused as-is; only its membership is reconciled. Members
    Pelican asserted from a login claim (source "oidc") are never removed, since Pelican
    re-asserts them on the next login and both sides read the same directory anyway."""

    def __init__(self, pelican, settings: PelicanSettings, report: SyncReport, dry_run: bool) -> None:
        self._pelican = pelican
        self._marker = settings.collection_description
        self.report = report
        self._dry_run = dry_run

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

    def ensure_groups(self, wanted: dict[str, set[str]], display_names: dict[str, str],
                      existing: dict[str, PelicanGroup]) -> dict[str, str]:
        """Create missing groups and reconcile membership. Returns name -> group id."""
        group_ids: dict[str, str] = {}
        for name in sorted(wanted):
            group = existing.get(name)
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
            if current[user_id] == "oidc":
                continue
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
