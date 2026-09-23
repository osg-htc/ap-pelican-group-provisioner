"""The set of known projects, from Topology's miscproject JSON."""

from __future__ import annotations

import logging

import httpx

from provisioner.report import HardError

log = logging.getLogger(__name__)


def fetch_project_names(url: str, timeout_seconds: float = 30) -> dict[str, str]:
    """Return project name -> display name from the JSON object at `url`.

    The document is an object keyed by project name; each value carries the project's
    Topology record (Name, PIName, Organization, ...). A fetch failure or an unexpected
    shape is a hard error: with no project list the job cannot tell a departed project
    from a broken feed, and must not start deleting.
    """
    try:
        response = httpx.get(url, timeout=timeout_seconds, follow_redirects=True)
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HardError(f"Could not fetch projects from {url}: {exc}") from exc
    if not isinstance(data, dict) or not data:
        raise HardError(f"Projects document at {url} is not a non-empty object")
    names = {}
    for key, record in data.items():
        display = record.get("Name") if isinstance(record, dict) else None
        names[str(key)] = str(display or key)
    log.info("Projects known to %s: %d", url, len(names))
    return names
