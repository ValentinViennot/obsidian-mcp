"""The panel's "Confirm vault is empty" action (#309, design D5).

An index pass refuses to prune a scope whose vault root yielded no markdown
file while its index still holds rows (`indexer.IndexIndeterminate`). This
module is what an administrator uses to say "the vault really was emptied":
it issues and redeems the confirmation token, decides whether a target is
eligible, and — on a redemption that passes every check — grants the scope
the single-use empty-prune permission (`services.empty_prune`) and starts an
index pass for that scope alone (`indexer.index_scope_now`).

**Why the re-embed confirmation was not reused.** `reembed-confirm` signs a
random string and checks its age, nothing more: the same token can be posted
any number of times within its minute, by any administrator, and it names no
target. That is tolerable for an action whose worst case is a re-embed; it is
not for one that authorises deleting a tenant's whole index. So this token has
its own salt and binds everything the decision depends on:

- the action name (a token signed for anything else is refused),
- the target scope (`None` for single-user, else the user id),
- the issuing administrator's id (the sentinel's `None` in single-user mode),
- the scope's canonical vault assignment at issue time,
- a random **process epoch** generated at import — a restart empties the
  consumed-nonce set, so a token from before the restart must not verify,
- a random nonce, consumed atomically on redemption.

It expires after `TOKEN_MAX_AGE_SECONDS` (10 minutes), independently of the
permission's own 15-minute lifetime.

`--workers 1` is part of the deployment contract (rate-limits.md), so the
in-process consumed-nonce set is the server's. Every check below is
synchronous: there is no `await` between "not yet consumed" and "consumed",
so two concurrent redemptions of one token cannot both pass on the single
event loop.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass

from itsdangerous import (
    BadSignature,
    SignatureExpired,
    TimestampSigner,
    URLSafeTimedSerializer,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.models.db import NoteMetadata, User
from src.services import empty_prune, vault_overlap
from src.services.transfer import canonical_vault_root

logger = logging.getLogger(__name__)

#: The action name bound into every token, and the serializer salt. The salt is
#: distinct from every other in the codebase (`csrf-token`, `reembed-confirm`,
#: the session cookie's), so no other signed value verifies here.
ACTION = "confirm-empty-vault"
SALT = "confirm-empty-vault-v1"

#: Token lifetime (design D5): ten minutes, separate from the permission TTL.
TOKEN_MAX_AGE_SECONDS = 600

#: Generated once per process. A token carries the epoch it was issued under
#: and is refused under any other, so a restart invalidates every outstanding
#: token — the consumed-nonce set below does not survive one.
PROCESS_EPOCH = secrets.token_hex(16)

#: Wall clock for issuance and expiry, a module attribute so tests can move it.
_clock = time.time

#: nonce -> wall-clock instant after which its token cannot verify anyway.
_consumed: dict[str, float] = {}


class _Signer(TimestampSigner):
    """`TimestampSigner` on this module's clock, so a test can expire a token."""

    def get_timestamp(self) -> int:
        return int(_clock())


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt=SALT, signer=_Signer)


class TokenRefused(Exception):
    """The token does not authorise this redemption. Nothing was granted."""


def issue_token(scope: int | None, admin_id: int | None, assignment: str) -> str:
    """A signed, single-use token for confirming `scope` is empty."""
    return _serializer().dumps(
        {
            "a": ACTION,
            "s": scope,
            "i": admin_id,
            "v": assignment,
            "e": PROCESS_EPOCH,
            "n": secrets.token_urlsafe(16),
        }
    )


def _purge(now: float) -> None:
    for nonce in [n for n, until in _consumed.items() if until <= now]:
        del _consumed[nonce]


def redeem_token(
    token: str, *, scope: int | None, admin_id: int | None, assignment: str
) -> None:
    """Verify `token` for this redemption and consume its nonce, or raise.

    Raises `TokenRefused` naming the first failed check. On success the nonce
    is consumed, so the same token is refused from then on. Synchronous end
    to end: the check and the consumption are one step on the event loop.
    """
    try:
        payload, issued_at = _serializer().loads(
            token, max_age=TOKEN_MAX_AGE_SECONDS, return_timestamp=True
        )
    except SignatureExpired:
        raise TokenRefused("expired") from None
    except BadSignature:
        raise TokenRefused("bad signature") from None
    if not isinstance(payload, dict) or payload.get("a") != ACTION:
        raise TokenRefused("not issued for this action")
    if payload.get("e") != PROCESS_EPOCH:
        raise TokenRefused("issued before the server restarted")
    if payload.get("i") != admin_id:
        raise TokenRefused("issued to a different administrator")
    if payload.get("s") != scope:
        raise TokenRefused("issued for a different vault")
    if payload.get("v") != assignment:
        raise TokenRefused("issued under a different vault assignment")
    nonce = payload.get("n")
    if not isinstance(nonce, str) or not nonce:
        raise TokenRefused("malformed")
    now = _clock()
    _purge(now)
    if nonce in _consumed:
        raise TokenRefused("already used")
    _consumed[nonce] = issued_at.timestamp() + TOKEN_MAX_AGE_SECONDS + 1


def reset() -> None:
    """Forget every consumed nonce. Tests only."""
    _consumed.clear()


# ── Target eligibility and the page's facts ─────────────────────────────────


@dataclass(frozen=True)
class Target:
    """A scope the action may be confirmed for, as it stands now."""

    scope: int | None
    label: str
    assignment: str


class Ineligible(Exception):
    """The target may not be confirmed. `str(e)` is operator-facing."""


async def resolve_target(session: AsyncSession, scope: int | None) -> Target:
    """The eligible target for `scope`, or raise `Ineligible`.

    Single-user mode has one scope (`None`) and it is always eligible. In
    multi-user mode the user must exist, be active, have a vault assigned and
    not be quarantined by the published vault-overlap snapshot — an
    unpublished snapshot refuses too, as `_vault_root` does. The assignment is
    `canonical_vault_root` of the configured or assigned path, exactly what
    the indexer re-checks the permission against.
    """
    if scope is None:
        if settings.multi_user_mode:
            raise Ineligible(
                "Multi-user mode has no single-user vault; confirm per user "
                "on the user's page."
            )
        return Target(
            scope=None,
            label="this vault",
            assignment=canonical_vault_root(settings.vault_path),
        )
    if not settings.multi_user_mode:
        raise Ineligible("Per-user confirmation exists only in multi-user mode.")
    user = (
        await session.execute(select(User).where(User.id == scope))
    ).scalar_one_or_none()
    if user is None:
        raise Ineligible("That user does not exist.")
    if not user.is_active:
        raise Ineligible(f"{user.username} is inactive; nothing was granted.")
    if not user.vault_path:
        raise Ineligible(f"{user.username} has no vault assigned; nothing was granted.")
    snapshot = vault_overlap.published_snapshot()
    if snapshot is None:
        raise Ineligible(
            "Vault roots have not been checked in this process yet; try again "
            "in a moment."
        )
    if snapshot.names(user.id):
        raise Ineligible(
            f"{user.username}'s vault root is quarantined by the overlap check; "
            "nothing was granted."
        )
    return Target(
        scope=user.id,
        label=f"{user.username}'s vault",
        assignment=canonical_vault_root(user.vault_path),
    )


async def indexed_note_count(session: AsyncSession, scope: int | None) -> int:
    """How many indexed notes the scope holds — what a confirmed pass deletes."""
    stmt = select(func.count(NoteMetadata.id))
    stmt = (
        stmt.where(NoteMetadata.user_id.is_(None))
        if scope is None
        else stmt.where(NoteMetadata.user_id == scope)
    )
    return int((await session.execute(stmt)).scalar() or 0)


def describe_scope(scope: int | None) -> str:
    return "single-user scope" if scope is None else f"user_id={scope}"


# ── Redemption ──────────────────────────────────────────────────────────────


async def _scope_pass(scope: int | None) -> None:
    """The background pass: `index_scope_now` for exactly `scope`.

    Looked up on the module at call time so a test's monkeypatch is honoured.
    `index_scope_now` records its own stages; this only stops an exception
    outside them (overlap detection, the cache warm) from vanishing into an
    unobserved task.
    """
    from src.services import indexer

    try:
        await indexer.index_scope_now(scope, trigger="manual")
    except Exception:
        logger.exception(
            "Confirm-vault-is-empty pass failed for %s", describe_scope(scope)
        )


def grant_and_start(target: Target, admin_username: str | None) -> None:
    """Grant the permission and start the targeted pass. Call after redemption.

    `empty_prune.grant` logs the grant at WARNING with the administrator and
    the scope, which is the audit line the spec requires. The task is kept by
    the panel's `_spawn`, as every other panel background pass is.
    """
    from src.control_panel import routes as panel_routes

    empty_prune.grant(target.scope, target.assignment, granted_by=admin_username)
    panel_routes._spawn(_scope_pass(target.scope))


def log_refusal(scope: int | None, admin_username: str | None, reason: str) -> None:
    logger.warning(
        "Confirm vault is empty refused for %s (admin %r): %s; nothing granted",
        describe_scope(scope),
        admin_username,
        reason,
    )


# ── The two routes' shared bodies ───────────────────────────────────────────
#
# Single-user: `/admin/settings/confirm-empty-vault` (routes.py). Multi-user:
# `/admin/users/{id}/confirm-empty-vault` (users.py). Both are admin-gated by
# their router/handler dependency and CSRF-checked by the router's
# `verify_csrf`, like every other panel POST.


def _admin_identity(user) -> tuple[int | None, str | None]:
    return getattr(user, "id", None), getattr(user, "username", None)


async def render_confirm_page(
    request,
    session: AsyncSession,
    user,
    scope: int | None,
    templates,
    *,
    post_action: str,
    cancel_url: str,
):
    """The confirmation page: the facts, and a token only when one is useful."""
    from src.control_panel.routes import _panel_context

    admin_id, _ = _admin_identity(user)
    refusal = None
    target = None
    count = 0
    try:
        target = await resolve_target(session, scope)
    except Ineligible as e:
        refusal = str(e)
    else:
        count = await indexed_note_count(session, scope)
    token = (
        issue_token(scope, admin_id, target.assignment)
        if target is not None and count > 0
        else None
    )
    return templates.TemplateResponse(
        request,
        "empty_vault_confirm.html",
        _panel_context(
            request,
            user,
            {
                "active": "settings" if scope is None else "users",
                "refusal": refusal,
                "target_label": target.label if target else None,
                "note_count": count,
                "token": token,
                "post_action": post_action,
                "cancel_url": cancel_url,
                "pending": target is not None and empty_prune.pending(scope),
                "token_minutes": TOKEN_MAX_AGE_SECONDS // 60,
                "permission_minutes": empty_prune.DEFAULT_TTL_SECONDS // 60,
            },
        ),
    )


async def handle_confirm_post(
    request,
    session: AsyncSession,
    user,
    scope: int | None,
    token: str,
    *,
    back_url: str,
    done_url: str,
):
    """Redeem the token for `scope`; on success grant and start the pass.

    Every refusal grants nothing and starts nothing, flashes the reason and
    returns to `back_url`. Eligibility is decided first, from the database as
    it stands now, and supplies the current assignment the token must match.
    """
    from fastapi.responses import RedirectResponse

    from src.control_panel.flash import ERR, flash

    admin_id, admin_username = _admin_identity(user)
    try:
        target = await resolve_target(session, scope)
    except Ineligible as e:
        log_refusal(scope, admin_username, f"ineligible: {e}")
        flash(request, str(e), ERR)
        return RedirectResponse(back_url, status_code=303)
    try:
        redeem_token(
            token, scope=scope, admin_id=admin_id, assignment=target.assignment
        )
    except TokenRefused as e:
        log_refusal(scope, admin_username, f"token {e}")
        flash(
            request,
            f"Confirmation refused ({e}); nothing was granted. Open the "
            "confirmation page again to get a fresh one.",
            ERR,
        )
        return RedirectResponse(back_url, status_code=303)
    grant_and_start(target, admin_username)
    flash(
        request,
        f"Confirmed: {target.label} may be pruned by the index pass that has "
        "just started. The permission is single-use and expires in "
        f"{empty_prune.DEFAULT_TTL_SECONDS // 60} minutes.",
    )
    return RedirectResponse(done_url, status_code=303)
