"""Read group membership from LDAP via the isMemberOf attribute on person entries."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import ldap3
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars

from provisioner.config import LdapSettings

log = logging.getLogger(__name__)


class LdapError(Exception):
    """Raised when the directory cannot be reached or searched."""


@dataclass(frozen=True)
class LdapPerson:
    dn: str
    username: str | None
    display_name: str | None
    sub: str | None


def _first(value: object) -> str | None:
    """ldap3 returns multi-valued attributes as lists; take the first string."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        if not value:
            return None
        value = value[0]
    text = str(value).strip()
    return text or None


class LdapClient:
    def __init__(self, settings: LdapSettings) -> None:
        self._settings = settings
        self._conn: ldap3.Connection | None = None

    def connect(self) -> None:
        s = self._settings
        server = ldap3.Server(
            s.server,
            port=s.port,
            use_ssl=s.use_ssl,
            connect_timeout=s.timeout_seconds,
            get_info=ldap3.NONE,
        )
        try:
            self._conn = ldap3.Connection(
                server,
                user=s.bind_dn,
                password=s.bind_password.get_secret_value(),
                auto_bind=True,
                receive_timeout=s.timeout_seconds,
                raise_exceptions=True,
            )
        except LDAPException as exc:
            raise LdapError(f"LDAP bind to {s.server}:{s.port} failed: {exc}") from exc
        log.info("Bound to LDAP %s:%s as %s", s.server, s.port, s.bind_dn)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.unbind()
            self._conn = None

    def group_members(self, group: str) -> list[LdapPerson]:
        """Return every person entry whose isMemberOf attribute contains `group`."""
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        s = self._settings

        search_filter = f"({s.member_of_attribute}={escape_filter_chars(group)})"
        attributes = [s.username_attribute, s.display_name_attribute, s.sub_attribute]
        log.debug("LDAP search base=%s filter=%s", s.user_search_base, search_filter)

        people: list[LdapPerson] = []
        try:
            entries = self._conn.extend.standard.paged_search(
                search_base=s.user_search_base,
                search_filter=search_filter,
                search_scope=ldap3.SUBTREE,
                attributes=attributes,
                paged_size=s.page_size,
                generator=True,
            )
            for entry in entries:
                if entry.get("type") != "searchResEntry":
                    continue
                attrs = entry.get("attributes", {})
                people.append(
                    LdapPerson(
                        dn=entry["dn"],
                        username=_first(attrs.get(s.username_attribute)),
                        display_name=_first(attrs.get(s.display_name_attribute)),
                        sub=_first(attrs.get(s.sub_attribute)),
                    )
                )
        except LDAPException as exc:
            raise LdapError(f"LDAP search failed: {exc}") from exc

        log.info("LDAP group %s has %d member entries", group, len(people))
        return people
