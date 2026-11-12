"""Read group membership from LDAP via the isMemberOf attribute on person entries."""

from __future__ import annotations

import logging
import os
import ssl
from dataclasses import dataclass

import certifi
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
    groups: frozenset[str] = frozenset()   # every value of the membership attribute


def _values(value: object) -> list[str]:
    """ldap3 returns a list for multi-valued attributes and a scalar otherwise."""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    return [text for text in (str(v).strip() for v in items) if text]


def _first(value: object) -> str | None:
    values = _values(value)
    return values[0] if values else None


def tls_config(s: LdapSettings) -> ldap3.Tls:
    """Require a certificate that chains to a trusted CA and names the server.

    ldap3 verifies nothing by default, which would let anyone on the path pose as the
    directory, collect the bind password and hand back invented memberships.
    """
    if not s.verify_tls:
        log.warning("LDAP TLS verification is disabled (LDAP_VERIFY_TLS=false)")
        return ldap3.Tls(validate=ssl.CERT_NONE)
    ca_file = s.ca_bundle or certifi.where()
    if not os.path.isfile(ca_file):
        # ldap3 silently ignores a missing file and would fall back to no CA at all.
        raise LdapError(f"LDAP CA bundle {ca_file} does not exist")
    return ldap3.Tls(
        validate=ssl.CERT_REQUIRED,
        ca_certs_file=ca_file,
        valid_names=[s.tls_server_name] if s.tls_server_name else None,
    )


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
            tls=tls_config(s),
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
        attributes = [s.username_attribute, s.display_name_attribute, s.sub_attribute, s.member_of_attribute]
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
                        groups=frozenset(_values(attrs.get(s.member_of_attribute))),
                    )
                )
        except LDAPException as exc:
            raise LdapError(f"LDAP search failed: {exc}") from exc

        log.info("LDAP group %s has %d member entries", group, len(people))
        return people
