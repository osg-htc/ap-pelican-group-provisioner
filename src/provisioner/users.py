"""Turn LDAP members into Pelican users: choose usernames, create what is missing."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from provisioner.config import LdapSettings, PelicanSettings
from provisioner.identifiers import sanitize_identifier, with_suffix
from provisioner.ldap_client import LdapPerson
from provisioner.pelican_client import PelicanError, PelicanUser
from provisioner.report import SyncReport

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DesiredUser:
    username: str      # Pelican username: sanitized LDAP value, possibly numbered, or adopted from an existing user
    ldap_name: str     # raw LDAP value; names the personal collection and its namespace path
    display_name: str | None
    sub: str           # CILogon identifier; the issuer is the run-wide pelican.issuer setting
    groups: frozenset[str] = frozenset()


class UserProvisioner:
    """Shared by every job kind: the same member always gets the same Pelican user."""

    def __init__(self, pelican, ldap_settings: LdapSettings, pelican_settings: PelicanSettings,
                 report: SyncReport, dry_run: bool) -> None:
        self._pelican = pelican
        self._ldap = ldap_settings
        self._issuer = pelican_settings.issuer
        self.report = report
        self._dry_run = dry_run

    def desired_users(self, people: list[LdapPerson], existing_users: list[PelicanUser]) -> dict[str, DesiredUser]:
        """Map each usable member to the Pelican username they should have.

        Username rules, in order:
          1. A Pelican user already linked to the member's (sub, issuer) keeps their username,
             whatever it is. This covers people who logged in before the script created them
             and keeps numbered names stable when the member they collided with leaves.
          2. Otherwise the sanitized LDAP value.
          3. If that is taken by another Pelican user or another member, append 2, 3, ...
        Two members with the same raw LDAP value cannot both have a personal collection
        path, so the second is reported and skipped.
        """
        s = self._ldap
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
            has_identity = 0 if (p.sub, self._issuer) in by_identity else 1
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
                self.report.error(
                    f"{person.dn}: same {s.username_attribute}={person.username!r} as {seen_ldap_names[person.username]}, "
                    "skipped; two members cannot share one collection path"
                )
                continue

            linked = by_identity.get((person.sub, self._issuer))
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
                username=username, ldap_name=person.username, display_name=person.display_name,
                sub=person.sub, groups=person.groups,
            )
            taken.add(username)
            seen_subs[person.sub] = person.dn
            seen_ldap_names[person.username] = person.dn
        return desired

    def ensure_users(self, desired: dict[str, DesiredUser], existing_users: list[PelicanUser]) -> dict[str, str]:
        """Return username -> Pelican user id for every desired user that exists afterwards."""
        existing = {u.username: u for u in existing_users}
        user_ids: dict[str, str] = {}
        for username, want in desired.items():
            have = existing.get(username)
            if have is not None:
                if have.sub and have.sub != want.sub:
                    # desired_users numbers around taken names, so this is defensive: never
                    # grant a collection to a user with a different identity.
                    self.report.error(f"user {username} exists with a different identity (sub={have.sub}), skipped")
                    continue
                user_ids[username] = have.id
                self.report.users_existing += 1
                continue

            if self._dry_run:
                log.info("DRY RUN: would create user %s (sub=%s issuer=%s)", username, want.sub, self._issuer)
                user_ids[username] = f"dry-run:{username}"
                self.report.users_created += 1
                continue

            try:
                created = self._pelican.create_user(username, want.display_name, want.sub, self._issuer)
            except PelicanError as exc:
                self.report.error(f"create user {username}: {exc}")
                continue
            log.info("Created user %s (id=%s)", username, created.id)
            user_ids[username] = created.id
            self.report.users_created += 1
        return user_ids
