"""The empty-prune permission registry (#309, design D5).

An index pass refuses to prune a scope whose vault root yielded no markdown
file while its index still holds rows (`indexer.IndexIndeterminate`): an empty
root is far more often a mount that did not mount than a vault emptied on
purpose. The operator says which it was from the panel ("Confirm vault is
empty"), which calls `grant`; the next pass of that scope calls `take`.

**Single-use, short-lived, in-process.** One permission per scope, valid for
`DEFAULT_TTL_SECONDS` (15 minutes) and bound to the scope's vault assignment at
grant time. `take` removes it atomically (the event loop is single-threaded and
nothing here awaits), so exactly one pass ever holds it; that pass carries it
as an invocation-local `Authorisation` and never returns it, whatever happens
to the pass. Not persisted: a restart drops an untaken permission and the
operator confirms again (L2). `--workers 1` is part of the deployment contract
(see rate-limits.md), so "in-process" is "the server".

The pass, not this module, decides what the permission authorises: never a
prune beneath an unlisted directory, never an unlistable root, and only after
re-checking expiry and the assignment immediately before it deletes.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

Scope = int | None

#: The permission's lifetime (design D5).
DEFAULT_TTL_SECONDS = 900

#: Monotonic clock, a module attribute so tests can move time.
_clock = time.monotonic


@dataclass(frozen=True)
class Authorisation:
    """A taken (or held) permission: whose, until when, under which root.

    `expires_at` is on `_clock` (monotonic seconds), not wall time, so a clock
    step cannot extend it. `assignment` is the scope's canonical vault
    assignment (`transfer.canonical_vault_root`) when it was granted.
    """

    scope: Scope
    expires_at: float
    assignment: str

    def expired(self) -> bool:
        return _clock() >= self.expires_at

    def valid_for(self, assignment: str) -> bool:
        """Unexpired and granted for exactly this assignment."""
        return not self.expired() and self.assignment == assignment


_registry: dict[Scope, Authorisation] = {}


def _describe(scope: Scope) -> str:
    return "single-user scope" if scope is None else f"user_id={scope}"


def grant(
    scope: Scope,
    assignment: str,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    granted_by: str | None = None,
) -> None:
    """Grant `scope` one empty-prune permission, replacing any earlier one.

    `granted_by` is the administrator's username, for the audit line only.
    """
    _registry[scope] = Authorisation(
        scope=scope,
        expires_at=_clock() + ttl_seconds,
        assignment=assignment,
    )
    logger.warning(
        "Empty-prune permission granted for %s%s, valid %d s: the next index "
        "pass may delete the scope's index if its vault root is empty",
        _describe(scope),
        f" by {granted_by!r}" if granted_by else "",
        ttl_seconds,
    )


def take(scope: Scope) -> Authorisation | None:
    """Remove and return `scope`'s permission, or None if there is none.

    An expired permission is removed and returned all the same — the caller
    checks `valid_for`, so an expired one simply authorises nothing.
    """
    auth = _registry.pop(scope, None)
    if auth is not None:
        logger.warning(
            "Empty-prune permission taken by the index pass for %s%s",
            _describe(scope),
            " (already expired)" if auth.expired() else "",
        )
    return auth


def pending(scope: Scope) -> bool:
    """Whether `scope` holds an unexpired, untaken permission."""
    auth = _registry.get(scope)
    return auth is not None and not auth.expired()


def reset() -> None:
    """Forget every permission. Tests only."""
    _registry.clear()
