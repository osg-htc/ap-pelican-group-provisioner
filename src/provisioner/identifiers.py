"""Pelican identifier rules, mirrored from database/identifiers.go so names are chosen the
way Pelican itself would choose them."""

from __future__ import annotations

import re
import string

# database.ValidateIdentifier: A-Za-z0-9._@-, length 2-64, must start with a letter or
# digit, and must not contain ".." (path-traversal marker).
IDENTIFIER_MAX_LEN = 64
_IDENTIFIER_CHARS = frozenset(string.ascii_letters + string.digits + "._@-")
IDENTIFIER_RE = re.compile(rf"^[A-Za-z0-9][A-Za-z0-9._@-]{{1,{IDENTIFIER_MAX_LEN - 1}}}$")


def is_valid_identifier(value: str) -> bool:
    return bool(IDENTIFIER_RE.match(value)) and ".." not in value


def sanitize_identifier(value: str) -> str:
    """Mirror database.SanitizeIdentifier, which Pelican applies to OIDC claims when
    auto-enrolling a user. Using the same rule means the name this script picks is the one
    Pelican would pick if the person logged in first, so the two never collide.

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


def path_segment_problem(value: str) -> str | None:
    """Why `value` cannot be the last component of a collection namespace, or None if it can.

    Collection names come from LDAP and the Topology feed and are appended to the job's base
    path. Pelican path-cleans the namespace before storing it and only checks that the result
    is inside an export, so "a/../b" or ".." would land on someone else's prefix or the export
    root. Dots inside a name ("first.last", "a..b") are fine: only whole "." and ".." segments
    move a path. Whitespace, control characters and ':' are rejected because Pelican refuses
    them in a namespace (database.CleanNamespacePath).
    """
    if not value:
        return "is empty"
    if "/" in value:
        return "contains '/'"
    if value in (".", ".."):
        return "is a relative path component"
    if any(ch.isspace() or not ch.isprintable() or ch == ":" for ch in value):
        return "contains whitespace, a control character or ':'"
    return None


def with_suffix(base: str, n: int) -> str:
    """base + n, trimmed so the result still fits Pelican's 64-character limit."""
    suffix = str(n)
    return base[: IDENTIFIER_MAX_LEN - len(suffix)] + suffix
