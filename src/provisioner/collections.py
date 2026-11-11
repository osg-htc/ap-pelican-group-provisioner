"""Reconcile the collections under one job's base path.

The base path is owned exclusively by the job: the only collections allowed under it are
one correct collection per desired name, where correct means the marker description and a
namespace of exactly <base_path>/<name>. Everything else under the base path is deleted.
Outside the base path only this job's own leftovers (marker plus matching metadata) are
touched, so a base path change migrates cleanly.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from provisioner.config import Job, Settings
from provisioner.pelican_client import PelicanCollection, PelicanError
from provisioner.report import HardError, SyncReport

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Grant:
    """Who gets the job's role on a collection."""

    subject_type: str   # "user" | "group"
    subject_id: str


@dataclass
class CollectionPlan:
    """What reconciliation decided, before anything is changed."""

    keep: dict[str, PelicanCollection]                  # name -> the one correct collection
    deletions: list[tuple[PelicanCollection, str]]      # collection, reason


class CollectionReconciler:
    def __init__(self, pelican, settings: Settings, job: Job, report: SyncReport) -> None:
        self._pelican = pelican
        self._settings = settings
        self._job = job
        self.report = report
        self._dry_run = settings.run.dry_run

    @property
    def base_path(self) -> str:
        return self._job.base_path.rstrip("/")

    def namespace_for(self, name: str) -> str:
        """The prefix a collection covers. Storage scopes are minted for exactly this path,
        so it is always the name's own subtree, never the shared base."""
        return f"{self.base_path}/{name}"

    def is_marked(self, c: PelicanCollection) -> bool:
        return c.description == self._settings.pelican.collection_description

    def is_under_base(self, c: PelicanCollection) -> bool:
        ns = c.namespace.rstrip("/")
        return ns == self.base_path or ns.startswith(self.base_path + "/")

    def is_correct(self, c: PelicanCollection) -> bool:
        return c.namespace.rstrip("/") == self.namespace_for(c.name)

    def under_another_job(self, c: PelicanCollection) -> bool:
        """Inside another configured job's base path, which that job owns. Checked before the
        metadata lookup so each run does not fetch the metadata of every collection another
        job made (one request per personal collection otherwise)."""
        ns = c.namespace.rstrip("/")
        for job in self._settings.jobs:
            base = job.base_path.rstrip("/")
            if base == self.base_path:   # this job (base paths are disjoint across jobs)
                continue
            if ns == base or ns.startswith(base + "/"):
                return True
        return False

    def belongs_to_job(self, c: PelicanCollection) -> bool:
        """Confirm via metadata that this job created the collection. Used only for marked
        collections found outside the base path, i.e. after a base path change."""
        try:
            metadata = self._pelican.get_collection_metadata(c.id)
        except PelicanError as exc:
            self.report.error(f"read metadata of collection {c.name} ({c.id}): {exc}")
            return False
        return metadata.get("provisioner.group") == self._job.group and metadata.get("provisioner.kind", "users") == self._job.kind

    # -- plan ------------------------------------------------------------------------

    def plan(self, wanted_names: set[str]) -> CollectionPlan:
        """Classify every collection on the Origin and check the deletion ceiling. Read-only."""
        try:
            all_collections = self._pelican.list_collections()
        except PelicanError as exc:
            raise HardError(f"Could not list Pelican collections: {exc}") from exc

        keep: dict[str, PelicanCollection] = {}
        deletions: list[tuple[PelicanCollection, str]] = []
        in_scope = 0
        for c in sorted(all_collections, key=lambda c: c.id):
            if self.is_under_base(c):
                in_scope += 1
                if self.is_marked(c) and self.is_correct(c) and c.name in wanted_names:
                    if c.name in keep:
                        deletions.append((c, f"duplicate of {keep[c.name].id}"))
                    else:
                        keep[c.name] = c
                elif not self.is_marked(c):
                    deletions.append((c, "not created by the provisioner; the base path is managed exclusively"))
                elif not self.is_correct(c):
                    deletions.append((c, f"wrong namespace {c.namespace}"))
                else:
                    deletions.append((c, "no longer wanted"))
            elif self.is_marked(c) and not self.under_another_job(c) and self.belongs_to_job(c):
                in_scope += 1
                deletions.append((c, f"left at old base path {c.namespace}"))

        log.info("Collections in scope of %s: %d, of which %d correct and %d to delete; %d of %d total belong elsewhere",
                 self.base_path, in_scope, len(keep), len(deletions), len(all_collections) - in_scope, len(all_collections))
        self._check_delete_ceiling(len(deletions), in_scope)
        return CollectionPlan(keep=keep, deletions=deletions)

    def _check_delete_ceiling(self, planned: int, in_scope: int) -> None:
        """Refuse to delete more than a fraction of this job's collections in one run.

        A partial LDAP result (size limit, replica mid-resync) looks exactly like a mass
        departure. Deleting and recreating would change collection IDs and drop any grants
        users added by hand, so a run past the ceiling stops before touching anything. A
        single deletion is always allowed so small deployments are not frozen.
        """
        if planned == 0:
            return
        r = self._settings.run
        allowed = max(1, math.floor(r.max_delete_fraction * in_scope))
        if planned <= allowed:
            return
        message = (f"job {self._job.group}: refusing to delete {planned} of {in_scope} collections under {self.base_path} "
                   f"(ceiling {allowed}, {r.max_delete_fraction:.0%})")
        if r.allow_mass_delete:
            log.warning("%s; proceeding because ALLOW_MASS_DELETE is set", message)
            return
        raise HardError(message + ". Set ALLOW_MASS_DELETE=true for one run if this is intended.")

    # -- apply -----------------------------------------------------------------------

    def apply(self, plan: CollectionPlan, grants: dict[str, Grant], metadata_for: dict[str, dict[str, str]]) -> None:
        """Delete what the plan says, then make sure every name in `grants` has a correct
        collection carrying its grant. Names without a resolved grant (their user or group
        could not be created this run) are left for the next run."""
        role = self._settings.pelican.collection_role
        for collection, reason in plan.deletions:
            self._delete(collection, reason)
        for name, grant in sorted(grants.items()):
            kept = plan.keep.get(name)
            if kept is None:
                self._create(name, grant, metadata_for.get(name, {}))
            else:
                self._ensure_acl(kept, grant, role)

    def _delete(self, c: PelicanCollection, reason: str) -> None:
        if self._dry_run:
            log.info("DRY RUN: would delete collection %s (id=%s, ns=%s): %s", c.name, c.id, c.namespace, reason)
            self.report.collections_deleted += 1
            return
        try:
            self._pelican.delete_collection(c.id)
        except PelicanError as exc:
            self.report.error(f"delete collection {c.name} ({c.id}): {exc}")
            return
        log.info("Deleted collection %s (id=%s, ns=%s): %s", c.name, c.id, c.namespace, reason)
        self.report.collections_deleted += 1

    def _create(self, name: str, grant: Grant, extra_metadata: dict[str, str]) -> None:
        p = self._settings.pelican
        namespace = self.namespace_for(name)
        metadata = {
            "provisioner.source": "ldap",
            "provisioner.kind": self._job.kind,
            "provisioner.group": self._job.group,
            **extra_metadata,
        }
        if self._dry_run:
            log.info("DRY RUN: would create collection %s at %s and grant %s to %s %s",
                     name, namespace, p.collection_role, grant.subject_type, grant.subject_id)
            self.report.collections_created += 1
            return
        try:
            created = self._pelican.create_collection(
                name=name, namespace=namespace, description=p.collection_description,
                visibility=p.collection_visibility, metadata=metadata,
            )
        except PelicanError as exc:
            self.report.error(f"create collection {name}: {exc}")
            return
        try:
            self._pelican.grant_acl(created.id, grant.subject_type, grant.subject_id, p.collection_role)
        except PelicanError as exc:
            # Do not leave a collection nobody can reach; the next run recreates it.
            self.report.error(f"grant {p.collection_role} on new collection {name} ({created.id}): {exc}")
            try:
                self._pelican.delete_collection(created.id)
            except PelicanError as cleanup:
                self.report.error(f"cleanup of collection {name} ({created.id}) failed: {cleanup}")
            return
        log.info("Created collection %s (id=%s) with %s for %s %s",
                 name, created.id, p.collection_role, grant.subject_type, grant.subject_id)
        self.report.collections_created += 1

    def _ensure_acl(self, c: PelicanCollection, grant: Grant, role: str) -> None:
        try:
            acls = self._pelican.list_acls(c.id)
        except PelicanError as exc:
            self.report.error(f"list ACLs for {c.name} ({c.id}): {exc}")
            return
        if any(a.subject_type == grant.subject_type and a.subject_id == grant.subject_id and a.role == role for a in acls):
            return
        if self._dry_run:
            log.info("DRY RUN: would grant %s on collection %s to %s %s", role, c.name, grant.subject_type, grant.subject_id)
            self.report.acls_repaired += 1
            return
        try:
            self._pelican.grant_acl(c.id, grant.subject_type, grant.subject_id, role)
        except PelicanError as exc:
            self.report.error(f"repair {role} on collection {c.name} ({c.id}): {exc}")
            return
        log.info("Repaired %s grant on collection %s (id=%s) for %s %s", role, c.name, c.id, grant.subject_type, grant.subject_id)
        self.report.acls_repaired += 1
