"""Synchronize an LDAP group into Pelican users and one collection per user.

See design/user-collections.md. The reconciliation logic here takes client objects as
arguments so it can be unit tested with in-memory fakes.
"""

from __future__ import annotations

import logging
import math
import re
import string
import sys
from dataclasses import dataclass, field
from typing import Protocol

from provisioner.config import ConfigError, Job, Settings, load_settings
from provisioner.ldap_client import LdapClient, LdapError, LdapPerson
from provisioner.logging_setup import setup_logging
from provisioner.pelican_client import (
    CollectionAcl,
    PelicanClient,
    PelicanCollection,
    PelicanError,
    PelicanUser,
)

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_HARD = 2

# Mirrors Pelican's database.ValidateIdentifier: A-Za-z0-9._@-, length 2-64, must start
# with a letter or digit, and must not contain ".." (path-traversal marker).
IDENTIFIER_MAX_LEN = 64
_IDENTIFIER_CHARS = frozenset(string.ascii_letters + string.digits + "._@-")
IDENTIFIER_RE = re.compile(rf"^[A-Za-z0-9][A-Za-z0-9._@-]{{1,{IDENTIFIER_MAX_LEN - 1}}}$")


def is_valid_identifier(value: str) -> bool:
    return bool(IDENTIFIER_RE.match(value)) and ".." not in value


def sanitize_identifier(value: str) -> str:
    """Mirror Pelican's database.SanitizeIdentifier, which it applies to OIDC claims when
    auto-enrolling a user. Using the same rule means the username this script picks is the
    one Pelican would pick if the person logged in first, so the two never collide.

    Disallowed bytes become '_' (non-ASCII included, byte-wise like the Go code), leading
    non-alphanumerics are stripped, '..' collapses to '.', the result is cut to 64 chars.
    Returns '' when nothing valid remains.
    """
    if not value:
        return ""
    out = "".join(chr(b) if chr(b) in _IDENTIFIER_CHARS else "_" for b in value.encode("utf-8"))
    while out and not out[0].isalnum():
        out = out[1:]
    while ".." in out:
        out = out.replace("..", ".")
    out = out[:IDENTIFIER_MAX_LEN]
    return out if is_valid_identifier(out) else ""


def with_suffix(base: str, n: int) -> str:
    """base + n, trimmed so the result still fits Pelican's 64-character limit."""
    suffix = str(n)
    return base[: IDENTIFIER_MAX_LEN - len(suffix)] + suffix


class HardError(Exception):
    """Stops the run before completion. Maps to exit code 2."""


class UserSource(Protocol):
    def group_members(self, group: str) -> list[LdapPerson]: ...


class OriginApi(Protocol):
    def list_users(self) -> list[PelicanUser]: ...
    def create_user(self, username: str, display_name: str | None, sub: str, issuer: str) -> PelicanUser: ...
    def list_collections(self) -> list[PelicanCollection]: ...
    def create_collection(
        self, name: str, namespace: str, description: str, visibility: str, metadata: dict[str, str] | None = None
    ) -> PelicanCollection: ...
    def delete_collection(self, collection_id: str) -> None: ...
    def get_collection_metadata(self, collection_id: str) -> dict[str, str]: ...
    def list_acls(self, collection_id: str) -> list[CollectionAcl]: ...
    def grant_user_acl(self, collection_id: str, user_id: str, role: str) -> None: ...


@dataclass(frozen=True)
class DesiredUser:
    username: str      # Pelican username: sanitized LDAP value, possibly numbered, or adopted from an existing user
    ldap_name: str     # raw LDAP value; names the collection and its namespace path
    display_name: str | None
    sub: str           # CILogon identifier; the issuer is the run-wide pelican.issuer setting


@dataclass
class SyncReport:
    users_created: int = 0
    users_existing: int = 0
    collections_created: int = 0
    collections_deleted: int = 0
    acls_repaired: int = 0
    errors: list[str] = field(default_factory=list)

    def error(self, message: str) -> None:
        log.error(message)
        self.errors.append(message)

    @property
    def exit_code(self) -> int:
        return EXIT_PARTIAL if self.errors else EXIT_OK

    def merge(self, other: SyncReport) -> None:
        self.users_created += other.users_created
        self.users_existing += other.users_existing
        self.collections_created += other.collections_created
        self.collections_deleted += other.collections_deleted
        self.acls_repaired += other.acls_repaired
        self.errors.extend(other.errors)

    def summary(self) -> str:
        return (
            f"users_created={self.users_created} users_existing={self.users_existing} "
            f"collections_created={self.collections_created} collections_deleted={self.collections_deleted} "
            f"acls_repaired={self.acls_repaired} errors={len(self.errors)}"
        )


@dataclass
class CollectionPlan:
    """What reconciliation decided, before anything is changed."""

    keep: dict[str, PelicanCollection]                  # ldap_name -> the one correct collection
    deletions: list[tuple[PelicanCollection, str]]      # collection, reason


class UserCollectionSync:
    """Synchronize one job: one LDAP group into one base path on the Origin.

    The job's base path is owned exclusively by this script: anything under it that is not
    the single correct collection for a current group member is deleted. Collections
    outside the base path are never touched, except this job's own collections left behind
    by a base path change, which are recognized by their metadata and removed.
    """

    def __init__(self, ldap: UserSource, pelican: OriginApi, settings: Settings, job: Job) -> None:
        self._ldap = ldap
        self._pelican = pelican
        self._settings = settings
        self._job = job
        self._dry_run = settings.run.dry_run
        self.report = SyncReport()

    # -- entry -----------------------------------------------------------------------

    def run(self) -> SyncReport:
        log.info("Job: LDAP group %s -> %s/<user>", self._job.group, self.base_path)
        existing_users = self.list_existing_users()
        desired = self.desired_users(existing_users)
        # Plan the collection changes and check the deletion ceiling BEFORE creating any
        # user, so a refused run has changed nothing at all.
        plan = self.plan_collections(desired)
        user_ids = self.ensure_users(desired, existing_users)
        self.apply_collections(plan, desired, user_ids)
        return self.report

    # -- phase 1: users wanted -------------------------------------------------------

    def list_existing_users(self) -> list[PelicanUser]:
        try:
            return self._pelican.list_users()
        except PelicanError as exc:
            raise HardError(f"Could not list Pelican users: {exc}") from exc

    def desired_users(self, existing_users: list[PelicanUser]) -> dict[str, DesiredUser]:
        """Map each usable group member to the Pelican username they should have.

        Username rules, in order:
          1. A Pelican user already linked to the member's (sub, issuer) keeps their username,
             whatever it is. This covers people who logged in before the script created them
             and keeps numbered names stable when the member they collided with leaves.
          2. Otherwise the sanitized LDAP value.
          3. If that is taken by another Pelican user or another member, append 2, 3, ...
        """
        s = self._settings.ldap
        issuer = self._settings.pelican.issuer
        try:
            people = self._ldap.group_members(self._job.group)
        except LdapError as exc:
            raise HardError(str(exc)) from exc

        by_identity = {(u.sub, u.issuer): u for u in existing_users if u.sub}
        taken = {u.username for u in existing_users}
        desired: dict[str, DesiredUser] = {}
        seen_subs: dict[str, str] = {}
        seen_ldap_names: dict[str, str] = {}

        def order(p: LdapPerson) -> tuple[int, int, str, str]:
            # Members with an existing Pelican identity first (their names are fixed), then
            # members whose LDAP value is already a valid identifier, then everyone else.
            # Within a tier, sort by LDAP value, then sub, so collisions resolve the same way
            # every run regardless of the order the directory returns entries.
            has_identity = 0 if (p.sub, issuer) in by_identity else 1
            unchanged = 0 if p.username and sanitize_identifier(p.username) == p.username else 1
            return (has_identity, unchanged, p.username or "", p.sub or "")

        for person in sorted(people, key=order):
            if not person.username:
                self.report.error(f"{person.dn}: missing {s.username_attribute}, skipped")
                continue
            if not person.sub:
                self.report.error(f"{person.dn}: missing {s.sub_attribute}, skipped")
                continue
            if person.sub in seen_subs:
                self.report.error(f"{person.dn}: same {s.sub_attribute} as {seen_subs[person.sub]}, skipped")
                continue
            if person.username in seen_ldap_names:
                # Two identities cannot share one collection path. Usernames can be
                # numbered, but the directory <base_path>/<value> belongs to one person.
                self.report.error(
                    f"{person.dn}: same {s.username_attribute}={person.username!r} as {seen_ldap_names[person.username]}, "
                    "skipped; two members cannot share one collection path"
                )
                continue

            linked = by_identity.get((person.sub, issuer))
            if linked is not None:
                username = linked.username
                if username != person.username:
                    log.info("%s: keeping existing Pelican username %r for %s=%r",
                             person.dn, username, s.username_attribute, person.username)
            else:
                base = sanitize_identifier(person.username)
                if not base:
                    self.report.error(
                        f"{person.dn}: {s.username_attribute}={person.username!r} cannot be made a valid Pelican identifier, skipped"
                    )
                    continue
                username, n = base, 2
                while username in taken:
                    username, n = with_suffix(base, n), n + 1
                if username != person.username:
                    log.warning("%s: %s=%r becomes username %r%s", person.dn, s.username_attribute, person.username,
                                username, " (collision)" if username != base else "")

            desired[username] = DesiredUser(
                username=username, ldap_name=person.username, display_name=person.display_name, sub=person.sub,
            )
            taken.add(username)
            seen_subs[person.sub] = person.dn
            seen_ldap_names[person.username] = person.dn

        if not desired and not s.allow_empty_group:
            raise HardError(
                f"LDAP group {self._job.group!r} resolved to zero usable members. "
                "Refusing to continue because that would delete every managed collection. "
                "Set LDAP_ALLOW_EMPTY_GROUP=true if this is intended."
            )
        log.info("Desired users: %d", len(desired))
        return desired

    # -- phase 2: users exist --------------------------------------------------------

    def ensure_users(self, desired: dict[str, DesiredUser], existing_users: list[PelicanUser]) -> dict[str, str]:
        """Return a map of username -> Pelican user id for every desired user that exists."""
        issuer = self._settings.pelican.issuer
        existing = {u.username: u for u in existing_users}
        user_ids: dict[str, str] = {}
        for username, want in desired.items():
            have = existing.get(username)
            if have is not None:
                if have.sub and have.sub != want.sub:
                    # desired_users avoids this by numbering around taken names; if it happens
                    # anyway, never grant a collection to a user with a different identity.
                    self.report.error(f"user {username} exists with a different identity (sub={have.sub}), skipped")
                    continue
                user_ids[username] = have.id
                self.report.users_existing += 1
                continue

            if self._dry_run:
                log.info("DRY RUN: would create user %s (sub=%s issuer=%s)", username, want.sub, issuer)
                user_ids[username] = f"dry-run:{username}"
                self.report.users_created += 1
                continue

            try:
                created = self._pelican.create_user(username, want.display_name, want.sub, issuer)
            except PelicanError as exc:
                self.report.error(f"create user {username}: {exc}")
                continue
            log.info("Created user %s (id=%s)", username, created.id)
            user_ids[username] = created.id
            self.report.users_created += 1
        return user_ids

    # -- phase 3: collections --------------------------------------------------------

    @property
    def base_path(self) -> str:
        return self._job.base_path.rstrip("/")

    def collection_namespace(self, ldap_name: str) -> str:
        """The prefix a user's collection covers. Storage scopes are minted for exactly
        this path, so it must be the user's own subtree, never the shared base. It uses
        the raw LDAP name because that is the directory the user's data lives under."""
        return f"{self.base_path}/{ldap_name}"

    def is_marked(self, collection: PelicanCollection) -> bool:
        return collection.description == self._settings.pelican.collection_description

    def is_under_base(self, collection: PelicanCollection) -> bool:
        ns = collection.namespace.rstrip("/")
        return ns == self.base_path or ns.startswith(self.base_path + "/")

    def is_correct(self, collection: PelicanCollection) -> bool:
        return collection.namespace.rstrip("/") == self.collection_namespace(collection.name)

    def belongs_to_job(self, collection: PelicanCollection) -> bool:
        """Confirm via metadata that this job created the collection. Used only for marked
        collections found outside the base path, i.e. after a base path change."""
        try:
            metadata = self._pelican.get_collection_metadata(collection.id)
        except PelicanError as exc:
            self.report.error(f"read metadata of collection {collection.name} ({collection.id}): {exc}")
            return False
        return metadata.get("provisioner.group") == self._job.group

    def plan_collections(self, desired: dict[str, DesiredUser]) -> CollectionPlan:
        """Classify every collection on the Origin and check the deletion ceiling. Read-only."""
        try:
            all_collections = self._pelican.list_collections()
        except PelicanError as exc:
            raise HardError(f"Could not list Pelican collections: {exc}") from exc

        wanted_names = {u.ldap_name for u in desired.values()}
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
                    deletions.append((c, "user no longer in group"))
            elif self.is_marked(c) and self.belongs_to_job(c):
                in_scope += 1
                deletions.append((c, f"left at old base path {c.namespace}"))

        log.info("Collections in scope of %s: %d, of which %d correct and %d to delete; %d of %d total belong elsewhere",
                 self.base_path, in_scope, len(keep), len(deletions), len(all_collections) - in_scope, len(all_collections))
        self._check_delete_ceiling(len(deletions), in_scope)
        return CollectionPlan(keep=keep, deletions=deletions)

    def apply_collections(self, plan: CollectionPlan, desired: dict[str, DesiredUser], user_ids: dict[str, str]) -> None:
        role = self._settings.pelican.collection_role

        for collection, reason in plan.deletions:
            self._delete_collection(collection, reason)

        # Every desired user with a Pelican id either keeps their correct collection (and
        # gets its grant checked) or receives a new one. A user whose creation failed this
        # run has no id and is picked up next run.
        for username, user_id in sorted(user_ids.items()):
            want = desired[username]
            kept = plan.keep.get(want.ldap_name)
            if kept is None:
                self._create_collection_for(want, user_id)
            else:
                self._ensure_acl(kept, want.ldap_name, user_id, role)

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

    def _delete_collection(self, collection: PelicanCollection, reason: str) -> None:
        if self._dry_run:
            log.info("DRY RUN: would delete collection %s (id=%s, ns=%s): %s",
                     collection.name, collection.id, collection.namespace, reason)
            self.report.collections_deleted += 1
            return
        try:
            self._pelican.delete_collection(collection.id)
        except PelicanError as exc:
            self.report.error(f"delete collection {collection.name} ({collection.id}): {exc}")
            return
        log.info("Deleted collection %s (id=%s, ns=%s): %s", collection.name, collection.id, collection.namespace, reason)
        self.report.collections_deleted += 1

    def _create_collection_for(self, want: DesiredUser, user_id: str) -> None:
        p = self._settings.pelican
        name = want.ldap_name  # collection name; the Pelican username may differ
        namespace = self.collection_namespace(want.ldap_name)
        metadata = {
            "provisioner.source": "ldap",
            "provisioner.group": self._job.group,
            "provisioner.ldap_name": want.ldap_name,
            "provisioner.username": want.username,
        }
        if self._dry_run:
            log.info("DRY RUN: would create collection %s at %s and grant %s to user %s",
                     name, namespace, p.collection_role, user_id)
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
            self._pelican.grant_user_acl(created.id, user_id, p.collection_role)
        except PelicanError as exc:
            # Do not leave a collection nobody can reach; the next run recreates it.
            self.report.error(f"grant {p.collection_role} on new collection {name} ({created.id}): {exc}")
            try:
                self._pelican.delete_collection(created.id)
            except PelicanError as cleanup:
                self.report.error(f"cleanup of collection {name} ({created.id}) failed: {cleanup}")
            return

        log.info("Created collection %s (id=%s) with %s for user %s (id=%s)", name, created.id, p.collection_role,
                 want.username, user_id)
        self.report.collections_created += 1

    def _ensure_acl(self, collection: PelicanCollection, name: str, user_id: str, role: str) -> None:
        try:
            acls = self._pelican.list_acls(collection.id)
        except PelicanError as exc:
            self.report.error(f"list ACLs for {name} ({collection.id}): {exc}")
            return
        for acl in acls:
            if acl.subject_type == "user" and acl.subject_id == user_id and acl.role == role:
                return
        if self._dry_run:
            log.info("DRY RUN: would grant %s on collection %s to user %s", role, name, user_id)
            self.report.acls_repaired += 1
            return
        try:
            self._pelican.grant_user_acl(collection.id, user_id, role)
        except PelicanError as exc:
            self.report.error(f"repair {role} on collection {name} ({collection.id}): {exc}")
            return
        log.info("Repaired %s grant on collection %s (id=%s) for user id %s", role, name, collection.id, user_id)
        self.report.acls_repaired += 1


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
            report = UserCollectionSync(ldap, pelican, settings, job).run()
            log.info("Job %s complete: %s", job.group, report.summary())
            total.merge(report)
    except HardError as exc:
        log.error("Hard error, run aborted: %s", exc)
        sys.exit(EXIT_HARD)
    finally:
        ldap.close()
        pelican.close()

    log.info("Run complete (%d jobs): %s", len(settings.jobs), total.summary())
    sys.exit(total.exit_code)


if __name__ == "__main__":
    main()
