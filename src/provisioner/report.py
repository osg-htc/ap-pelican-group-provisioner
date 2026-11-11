"""Run outcome: counters, per-item errors, and the exit code they map to."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

EXIT_OK = 0        # everything reconciled
EXIT_PARTIAL = 1   # completed, but some items could not be processed
EXIT_HARD = 2      # stopped before completion; nothing after the failure was touched


class HardError(Exception):
    """Stops the run before completion. Maps to EXIT_HARD."""


@dataclass
class SyncReport:
    users_created: int = 0
    users_existing: int = 0
    groups_created: int = 0
    groups_deleted: int = 0
    groups_replaced: int = 0    # deleted because someone else controlled them, then recreated
    members_added: int = 0
    members_removed: int = 0
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
        for name in self.__dataclass_fields__:
            if name == "errors":
                self.errors.extend(other.errors)
            else:
                setattr(self, name, getattr(self, name) + getattr(other, name))

    def summary(self) -> str:
        counters = {name: getattr(self, name) for name in self.__dataclass_fields__ if name != "errors"}
        shown = " ".join(f"{k}={v}" for k, v in counters.items() if v or k in ("users_existing", "collections_created", "collections_deleted"))
        return f"{shown} errors={len(self.errors)}"
