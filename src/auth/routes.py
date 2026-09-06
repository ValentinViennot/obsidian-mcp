"""Auth routes — login, logout, bootstrap admin registration.

The router is mounted at the FastAPI app level in `src/main.py` ONLY when
`settings.multi_user_mode` is true. In single-user mode the router is not
mounted at all, so these paths 404.

`AUTH_MODE` decides which of two mutually exclusive login mechanisms this
router exposes, and it is exclusive on purpose:

* `local` (the default, and unchanged) — the username/password form, the
  bootstrap registration page, and the four handlers below that serve them.
* `pocketid` — `GET /admin/auth/login` redirects to an OIDC provider,
  `GET /admin/auth/oidc/callback` completes the flow, and **the password form
  and self-registration 404**. Not hidden: 404. A login page that still
  accepted a password would be a way around the identity provider, and the
  provider is the whole point of choosing that mode.

**What does not change in either mode**: the server's own OAuth 2.0
authorization server in `src/oauth/routes.py`. MCP clients still register
dynamically, still do PKCE, still see the consent screen, still refresh and
revoke — because PocketID publishes no `registration_endpoint` and those
clients require one. `GET /authorize`'s redirect to
`/admin/auth/login?next=<the whole /authorize URL>` is the one seam between the
two, and its contract is untouched: this router still answers that path, still
honours that parameter, and still lands the person back on the consent screen.

`/admin/auth/*` and `/admin/register` live under the `/admin` prefix so that
Traefik's `chain-oauth@file` middleware (which gates `/admin/*` on the
production deploy) still fronts them. That gating is what makes the bootstrap
race-free in practice — only an already-SSO'd admin can reach
`/admin/register`. The application also enforces a strict empty-users-table
guard with a PostgreSQL transaction-scoped advisory lock for defense in
depth.
"""
import logging
import os
import re
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth import oidc
from src.auth.passwords import (
    MIN_PASSWORD_LENGTH,
    hash_password,
    unusable_password_hash,
    validate_new_password,
    verify_password,
)
from src.auth.session import (
    SESSION_ID_KEY,
    get_active_session_user,
    hash_session_id,
    revoke_session,
    start_session,
)
from src.config import settings
from src.csrf import generate_csrf_token, verify_csrf
from src.database import get_session
from src.limiter import limiter
from src.models.db import APIKey, NoteMetadata, OAuthClient, OAuthCode, OAuthToken, UsageLog, User
from src.oauth.grants import USER_BOOTSTRAP_LOCK_KEY
from src.services import security_events
from src.services.vault import validate_vault_root_path, warm_user_vault_cache

router = APIRouter(tags=["auth"], dependencies=[Depends(verify_csrf)])

# Templates resolved from the panel directory so all auth templates can
# extend `auth_base.html` co-located with the existing panel templates.
templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(__file__), "..", "control_panel", "templates")
)

# Advisory-lock key for the bootstrap-registration critical section. Any
# distinct 32-bit int works; this is just a constant the lock function
# expects. Two concurrent /admin/register POSTs will serialize on this key.
# Shared with the OAuth token-minting handlers, which take the same key so a
# mint cannot insert a new ownerless token in the window between this
# transaction's `WHERE user_id IS NULL` claim and its COMMIT. The value is
# unchanged; only its definition moved, so a rolling deploy still serializes.
_BOOTSTRAP_LOCK_KEY = USER_BOOTSTRAP_LOCK_KEY


# --- Helpers --------------------------------------------------------------


_USERNAME_RE = re.compile(r"^[a-z0-9_]{1,64}$")


def _safe_next(next_url: str | None) -> str:
    """Return `next_url` if it's a safe in-app redirect, else `/admin/`.

    Prevents an open-redirect via `?next=https://evil.example/...`. Only
    same-origin paths are allowed: one leading `/`, no scheme, no authority.

    Two refusals beyond "starts with a single slash", both about what a
    *browser* does with the value rather than what a parser says about it:

    * a leading `/` followed by a backslash, where the second slash of `//`
      would be. Browsers normalise that to `//`, so it navigates off-site
      exactly as `//evil.example` would.
    * control characters, including the tab, CR and LF that some clients strip
      *before* resolving the URL, which lets a value reassemble into an
      authority after this check has looked at it.

    The producing side (`require_user_panel` in the panel router) now
    percent-encodes what it puts in `?next=`, so a legitimate path with its own
    query string arrives here intact instead of half of it landing in the login
    URL's own query.
    """
    if not next_url:
        return "/admin/"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in next_url):
        return "/admin/"
    if not next_url.startswith("/"):
        return "/admin/"
    if next_url[1:2] in ("/", "\\"):
        return "/admin/"
    return next_url


def _bootstrap_refused(request: Request, reason: str) -> None:
    """One `panel_bootstrap_refused` record. The rendered form is unchanged.

    Bootstrap is reachable by anyone Traefik's OAuth chain lets through, and
    every branch below is a form field away, so the record is bounded on the
    client address like every other unauthenticated refusal.
    """
    security_events.emit(
        "panel_bootstrap_refused",
        subject=security_events.subject_for(request=request),
        reason=reason,
        client_ip=security_events.client_ip(request),
    )


def _bootstrap_password_reason(new: str, confirm: str) -> str:
    """The `panel_bootstrap_refused` reason behind a `validate_new_password` message.

    Called **only** once the validator has already refused, and it mirrors that
    function's order of checks — mismatch, then NUL, then length — so the
    record and the message rendered back into the form can never name
    different rules. The message is always the validator's own; this is only
    the log's word for it, which is why the two pre-existing reason strings
    are kept verbatim rather than renamed to match the panel's own catalogue.
    """
    if new != confirm:
        return "password_mismatch"
    if "\x00" in new:
        return "password_nul_byte"
    return "weak_password"


async def _users_table_empty(session: AsyncSession) -> bool:
    count = (await session.execute(select(func.count(User.id)))).scalar() or 0
    return count == 0


def _render_login(
    request: Request,
    *,
    error: str | None = None,
    next_url: str = "/admin/",
    username: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    # A raw API key left in the session by an unfollowed `/admin/keys` redirect
    # must not outlive the hop it was minted for. The panel's own dependency
    # (`_forget_new_key_flash`) covers every `/admin` and `/api` route; this
    # router is mounted at the app level and shares none of them, so the login
    # form — reachable while a stale session cookie is still being replayed —
    # clears it here. Logout and a successful login already do, through
    # `request.session.clear()`.
    try:
        request.session.pop("flash_new_key", None)
    except (AssertionError, AttributeError):
        pass
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": error, "next": next_url, "username": username, "csrf_token": generate_csrf_token(request)},
        status_code=status_code,
    )


def _render_register(
    request: Request,
    *,
    error: str | None = None,
    username: str | None = None,
    vault_path: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    default_username = os.environ.get("BOOTSTRAP_ADMIN_USERNAME", "max")
    return templates.TemplateResponse(
        request,
        "register.html",
        {
            "error": error,
            "username": username if username is not None else default_username,
            "vault_path": vault_path if vault_path is not None else settings.vault_path,
            "csrf_token": generate_csrf_token(request),
            # `minlength` and the hint read the server's constant rather than
            # restating a number that has already drifted once.
            "min_password_length": MIN_PASSWORD_LENGTH,
        },
        status_code=status_code,
    )


# --- Federated login (AUTH_MODE=pocketid) ---------------------------------

#: Re-exported from `src/auth/oidc.py` so the two handlers that set and clear
#: it, and the tests that read it, name one constant.
LOGIN_COOKIE = oidc.LOGIN_COOKIE


def _federated() -> bool:
    return settings.auth_mode == "pocketid"


def _require_local_password_route() -> None:
    """404 the password form and self-registration under `AUTH_MODE=pocketid`.

    `_require_account_route`'s rule, one mode over (D23): a route that cannot
    do anything here should not advertise that it exists elsewhere, so 404 and
    not 403. The stronger reason is that these two routes are not merely
    useless in federated mode, they are a **bypass**: a `POST` to the login
    form that still verified `password_hash` would sign a person in without the
    identity provider ever being consulted, and `/admin/register` would mint a
    fresh administrator the provider has never heard of. Withdrawing them is
    what makes "the provider is the only way in" true of the code rather than
    of the template that stopped rendering a form.

    One rule for every method of both routes is also one thing to test.
    """
    if _federated():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def _require_federated_route() -> None:
    """The mirror: the OIDC callback does not exist under `AUTH_MODE=local`.

    Same 404, same reason. A callback that stayed reachable in local mode would
    be an unauthenticated endpoint performing outbound requests against
    whatever `OIDC_ISSUER` happened to be left in the environment.
    """
    if not _federated():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def _oidc_refused(request: Request, reason: str, *, user_id: int | None = None) -> None:
    """One `panel_oidc_login_refused`. The rendered page is the same for all.

    Subject is the client address, never a resolved row: like every other
    unauthenticated refusal in this server, keying on the account an attacker
    named would hand them one fresh allowance per identity they guess. `reason`
    is the closed vocabulary `src/auth/oidc.py` raises plus this module's own
    four, and it is the only place the cause exists — the browser sees one
    constant page for every branch, exactly as `login_submit`'s three password
    branches answer with one byte-identical 401.
    """
    security_events.emit(
        "panel_oidc_login_refused",
        subject=security_events.subject_for(request=request),
        reason=reason,
        user_id=user_id,
        client_ip=security_events.client_ip(request),
        route=request.url.path,
    )


def _render_oidc_error(request: Request, *, status_code: int) -> HTMLResponse:
    """The one page every federated refusal renders.

    It names no reason. A person who mistyped nothing and did everything right
    can reach this page (their provider is down, or an operator has not
    assigned them the group), and a person probing it can reach it too; telling
    the two apart is the log's job. The only affordance is a link back to
    `/admin/auth/login`, which starts a fresh authorization request — a retry
    is the correct response to most of the reasons behind it.
    """
    try:
        request.session.pop("flash_new_key", None)
    except (AssertionError, AttributeError):
        pass
    return templates.TemplateResponse(
        request, "oidc_error.html", {}, status_code=status_code
    )


def _clear_login_cookie(response: Response) -> None:
    """Delete `LOGIN_COOKIE` — on **every** callback path, including refusals.

    A `state`/`nonce`/verifier triple that outlives the callback it was minted
    for is a replayable one, and the refusal paths are exactly where a leftover
    would be most useful to somebody: a `state` mismatch that left the cookie in
    place would let an attacker keep firing codes at it until one matched.
    """
    response.delete_cookie(LOGIN_COOKIE, path="/")


def _set_login_cookie(response: Response, sealed: str) -> None:
    """`oauth_state`'s cookie attributes, verbatim, one flow over.

    `httponly` because no script has any business reading it; `secure` keyed on
    `BASE_URL`'s scheme, because a browser silently drops a `Secure` cookie on
    plain HTTP and loopback development would otherwise be unable to log in at
    all; `samesite="lax"`, which is the strongest setting that survives the
    provider's top-level `GET` redirect back — `strict` would have the browser
    withhold the cookie on exactly the request that needs it.
    """
    response.set_cookie(
        LOGIN_COOKIE,
        sealed,
        httponly=True,
        secure=settings.base_url.startswith("https://"),
        samesite="lax",
        max_age=oidc.LOGIN_COOKIE_MAX_AGE,
        path="/",
    )


async def _redirect_to_provider(request: Request, target: str) -> Response:
    """Mint a login attempt and send the browser to the provider.

    `target` arrives **already through `_safe_next`**, and it is sealed into
    the signed cookie rather than round-tripped through the provider's `state`.
    Two reasons: the value never leaves this server in a form anybody can edit,
    so the callback's redirect cannot be turned into an open redirect by
    tampering; and `state` stays what it is for — an opaque CSRF nonce with no
    payload to parse.
    """
    try:
        url, pending = await oidc.authorization_request(target)
    except oidc.OIDCError as exc:
        # The provider is unreachable, or its discovery document is unusable.
        # 503 rather than 500: nothing is wrong with the request, and a retry
        # in a minute is the right advice.
        _oidc_refused(request, exc.reason)
        return _render_oidc_error(
            request, status_code=status.HTTP_503_SERVICE_UNAVAILABLE
        )
    response = RedirectResponse(url, status_code=status.HTTP_302_FOUND)
    _set_login_cookie(response, oidc.seal_pending(pending))
    return response


def _local_username_for(identity: oidc.VerifiedIdentity) -> str | None:
    """The local `users.username` this provider identity corresponds to.

    Used for **two** things and they are not the same weight:

    * naming a **new** row, and
    * choosing which pre-existing row a *first* login may adopt.

    The email's local part first, then `preferred_username`, folded into the
    `_USERNAME_RE` alphabet the rest of this server already enforces. The order
    matters because adoption is specified in terms of the email: an operator
    who created `max` by hand expects `max@example.com` to land on it.

    **Adoption is guarded on the target row carrying no `oidc_subject` yet**
    (see `_resolve_federated_user`), which is what keeps this derivation from
    being an account-takeover primitive: two provider accounts can easily fold
    to one local name — `max@a.example` and `max@b.example` both give `max` —
    and without that guard the second would inherit the first's vault. With it,
    the second is refused and an administrator resolves it by renaming.

    Returns `None` when neither claim yields a usable name, which is a refusal
    rather than a generated fallback: a server-invented username is a name no
    operator can recognise in the panel.
    """
    for source in (identity.email, identity.preferred_username):
        if not source:
            continue
        candidate = re.sub(r"[^a-z0-9_]", "_", source.split("@", 1)[0].strip().lower())
        candidate = candidate.strip("_")[:64]
        if _USERNAME_RE.match(candidate):
            return candidate
    return None


async def _resolve_federated_user(
    session: AsyncSession, identity: oidc.VerifiedIdentity
) -> tuple[User | None, str]:
    """Find, adopt or create the local account for a verified identity.

    Returns `(user, outcome)` where `outcome` is one of `existing`, `linked`,
    `created` — or `(None, <refusal reason>)`, and the caller cannot tell the
    two apart except by the `None`, which is deliberate: every refusal renders
    the same page.

    **The whole check-then-act runs under the bootstrap advisory lock.** Two
    concurrent first logins by the same person — a browser and its own
    prefetch — would otherwise both see "no row for this subject", both derive
    the same username and both insert. The unique index on `users.username`
    turns the loser into an `IntegrityError`, i.e. a 500 on a login that should
    simply have joined the winner. The lock is the *same* key
    `register_submit` takes, which is correct rather than convenient: both are
    "decide whether this account exists and create it if not", and giving them
    separate keys would let a bootstrap and a first federated login race each
    other into two administrators' worth of confusion.

    The lock and `lock_account_guard` (which `start_session` takes) are
    therefore held **sequentially, never nested** — this function commits
    before the caller mints a session, exactly as `register_submit` does.

    **No account is ever auto-promoted.** A created row is `is_admin=False`
    with no `vault_path`, so a person the provider has authenticated arrives
    with a session and no vault, and every MCP tool refuses them until an
    administrator assigns one. That is the same fail-closed shape
    `_vault_root` already enforces for a local user with no assignment, and it
    is why the first administrator must be bootstrapped under `AUTH_MODE=local`
    (or by hand) rather than being whoever logs in first.
    """
    existing = (
        await session.execute(
            select(User).where(User.oidc_subject == identity.subject)
        )
    ).scalar_one_or_none()
    if existing is not None:
        if not existing.is_active:
            return None, "inactive_user"
        return existing, "existing"

    username = _local_username_for(identity)
    if username is None:
        return None, "no_username_claim"

    await session.execute(
        text("SELECT pg_advisory_xact_lock(:k)"), {"k": _BOOTSTRAP_LOCK_KEY}
    )

    # Re-read under the lock. A concurrent first login may have created or
    # adopted the row between the unlocked lookup above and this point, and the
    # correct answer then is that one — not a second row for the same person.
    existing = (
        await session.execute(
            select(User).where(User.oidc_subject == identity.subject)
        )
    ).scalar_one_or_none()
    if existing is not None:
        if not existing.is_active:
            return None, "inactive_user"
        return existing, "existing"

    local = (
        await session.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()

    if local is not None:
        if local.oidc_subject is not None:
            # Another provider identity already owns this name. Refused rather
            # than reassigned: the two are different people as far as the
            # provider is concerned, and rewriting the link would hand the
            # second one the first one's vault. An administrator renames one.
            return None, "username_claimed"
        if not local.is_active:
            return None, "inactive_user"
        local.oidc_subject = identity.subject
        return local, "linked"

    created = User(
        username=username,
        # No local password exists for this account and none is invented — see
        # `unusable_password_hash`.
        password_hash=unusable_password_hash(),
        # **Never auto-granted.** The provider says who somebody is; it does
        # not say what they may do here.
        is_admin=False,
        is_active=True,
        oidc_subject=identity.subject,
    )
    session.add(created)
    await session.flush()  # populate created.id
    return created, "created"


@router.get("/admin/auth/oidc/callback")
@limiter.limit("10/minute")
async def oidc_callback(
    request: Request,
    code: str = Query(""),
    state: str = Query(""),
    error: str = Query(""),
    session: AsyncSession = Depends(get_session),
):
    """Complete the authorization-code flow and start a panel session.

    The order below is the order the checks have to happen in, and each one
    stands between the next and something it would otherwise trust:

    1. the mode gate, so this route does not exist under `AUTH_MODE=local`;
    2. the signed cookie, which is the only thing that says a login is in
       flight at all;
    3. `state`, compared in constant time — the CSRF check, and it runs before
       any outbound request so a forged callback cannot make this server talk
       to the provider;
    4. the provider's own `error` parameter, honoured after `state` so a
       consent denial is attributed to a real login attempt;
    5. the code exchange, then full ID-token verification including `nonce`;
    6. the group requirement;
    7. the account resolution and the commit;
    8. `start_session`, which owns its own guarded transaction, and only then
       the redirect.

    Every refusal renders the same page and clears the cookie. Nothing partial
    survives: there is no path that commits a `users` row and then fails to
    sign the person in without saying so in the log.
    """
    _require_federated_route()

    def refuse(reason: str, *, status_code: int = status.HTTP_400_BAD_REQUEST,
               user_id: int | None = None) -> Response:
        _oidc_refused(request, reason, user_id=user_id)
        response = _render_oidc_error(request, status_code=status_code)
        _clear_login_cookie(response)
        return response

    pending = oidc.open_pending(request.cookies.get(LOGIN_COOKIE))
    if pending is None:
        # No cookie, a forged one, an expired one, or one carrying the wrong
        # shape. From here they are one event: there is no login in flight.
        return refuse("no_login_in_flight")

    if not state or not secrets.compare_digest(state, pending.state):
        return refuse("state_mismatch")

    if error:
        # The provider refused, most often because the person declined consent.
        # The provider's code is *not* logged: it is provider-authored text on
        # an unauthenticated path, and `reason` is a closed vocabulary.
        return refuse("provider_refused")

    if not code:
        return refuse("no_code")

    try:
        id_token = await oidc.exchange_code(code, pending.code_verifier)
        identity = await oidc.verify_id_token(id_token, nonce=pending.nonce)
    except oidc.OIDCError as exc:
        # `provider_unreachable` is the one branch that is not the caller's
        # fault, and it is still the same page: distinguishing them for the
        # browser would tell a prober which of their forged tokens got as far
        # as the network.
        status_code = (
            status.HTTP_503_SERVICE_UNAVAILABLE
            if exc.reason in ("provider_unreachable", "provider_error")
            else status.HTTP_400_BAD_REQUEST
        )
        return refuse(exc.reason, status_code=status_code)

    if not oidc.group_allows(identity):
        # Authenticated, not authorized. 403 rather than 400: the request was
        # perfectly well formed and the answer will not change on a retry until
        # an operator changes the provider-side group.
        return refuse("group_required", status_code=status.HTTP_403_FORBIDDEN)

    try:
        user, outcome = await _resolve_federated_user(session, identity)
        if user is None:
            await session.rollback()
            return refuse(outcome, status_code=status.HTTP_403_FORBIDDEN)
        user.last_login_at = datetime.now(timezone.utc)
        user_id = user.id
        username = user.username
        session_version = user.session_version
        await session.commit()
    except Exception:
        await session.rollback()
        raise

    # After the commit, never after the flush (D17): a commit that then raises
    # would otherwise leave a record asserting an account exists that does not.
    if outcome in ("created", "linked"):
        security_events.emit(
            f"panel_oidc_user_{outcome}",
            level=logging.INFO,
            subject=security_events.subject_for(user_id=user_id, request=request),
            user_id=user_id,
            username=username,
            client_ip=security_events.client_ip(request),
        )

    # Warm the per-user vault-path cache, exactly as `login_submit` does. A
    # freshly created federated user has no assignment and is filtered out.
    await warm_user_vault_cache(session, user_id)

    # The mint runs after the resolution has committed and takes its own
    # guard — the two advisory-lock keys are therefore sequential, never
    # nested. A refusal here means a deactivation or a password reset committed
    # in the window; nobody is signed in and no row comes back to life.
    if (
        await start_session(
            request, session, user_id, expected_session_version=session_version
        )
        is None
    ):
        request.session.clear()
        return refuse(
            "session_mint_refused",
            status_code=status.HTTP_403_FORBIDDEN,
            user_id=user_id,
        )

    # **After the mint**, for `login_submit`'s reason (D17, sharpened): until
    # `start_session` has committed a row there is no session to have
    # succeeded. The same event as a password login, deliberately — an operator
    # filtering for "who signed in" must find both.
    security_events.emit(
        "panel_login_succeeded",
        level=logging.INFO,
        subject=security_events.subject_for(user_id=user_id, request=request),
        user_id=user_id,
        username=username,
        client_ip=security_events.client_ip(request),
        route=request.url.path,
    )

    # `pending.next_url` went through `_safe_next` before it was **signed**, so
    # it cannot have been edited in the browser. It is re-validated anyway: the
    # cost is one function call, and the alternative is a redirect whose safety
    # rests on a signature check three hundred lines away.
    response = RedirectResponse(
        _safe_next(pending.next_url), status_code=status.HTTP_302_FOUND
    )
    _clear_login_cookie(response)
    return response


# --- Login / logout -------------------------------------------------------


@router.get("/admin/auth/login", response_class=HTMLResponse)
async def login_form(
    request: Request,
    next: str = "/admin/",
    session: AsyncSession = Depends(get_session),
):
    # The already-signed-in short-circuit resolves through the **same**
    # validation every other entry point uses. Reading `request.session["user_id"]`
    # raw here is what made a revoked cookie bounce forever between this page
    # (which saw a user id and redirected to the panel) and `require_user_panel`
    # (which refused the session and redirected back) — a login page nobody
    # holding a dead cookie could reach.
    if await get_active_session_user(request, session) is not None:
        return RedirectResponse(_safe_next(next), status_code=status.HTTP_302_FOUND)
    target = _safe_next(next)
    if _federated():
        # **The seam.** `GET /authorize` redirects an unauthenticated MCP
        # client's user here with the whole `/authorize` URL in `next`; this
        # branch changes only who answers the "who are you" question, and
        # `target` — already validated — is carried through the provider round
        # trip inside a signed cookie so the person lands back on the consent
        # screen they came from.
        return await _redirect_to_provider(request, target)
    return _render_login(request, next_url=target)


@router.post("/admin/auth/login")
@limiter.limit("5/minute")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/admin/"),
    session: AsyncSession = Depends(get_session),
):
    _require_local_password_route()
    target = _safe_next(next)
    normalized = (username or "").strip().lower()

    # Constant error message: don't leak whether the username exists.
    invalid_msg = "Invalid credentials"

    result = await session.execute(select(User).where(User.username == normalized))
    user = result.scalar_one_or_none()

    # The merged condition is split **for the reason code only**. All three
    # branches fall into one `_render_login(..., status_code=401)`, so the
    # response is byte-identical across them and the log is the only place the
    # cause exists (#191). The order also preserves the original
    # short-circuit: `verify_password` still runs only for an active row.
    reason: str | None = None
    if user is None:
        reason = "unknown_user"
    elif not user.is_active:
        reason = "inactive_user"
    elif not verify_password(password, user.password_hash, user_id=user.id):
        reason = "bad_password"

    if reason is not None:
        # The suppression subject is the client address, never the resolved
        # row: a failed login resolved no credential, and keying on the user
        # would hand an attacker one fresh allowance per valid username they
        # guess.
        security_events.emit(
            "panel_login_failed",
            subject=security_events.subject_for(request=request),
            reason=reason,
            username_submitted=normalized,
            # Present only where a row actually resolved; `unknown_user` has
            # none, and the unsuffixed name may hold nothing else (D15).
            user_id=None if user is None else user.id,
            client_ip=security_events.client_ip(request),
            route=request.url.path,
        )
        return _render_login(
            request,
            error=invalid_msg,
            next_url=target,
            username=normalized,
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    # Update last_login_at in the same session.
    await session.execute(
        update(User).where(User.id == user.id).values(last_login_at=datetime.now(timezone.utc))
    )
    await session.commit()

    # Warm the per-user vault-path cache so any subsequent panel route /
    # vault tool call in this process can resolve `_vault_root(user.id)`
    # without a sync DB miss. Skips users with no vault_path assigned
    # (warm_user_vault_cache filters them out).
    await warm_user_vault_cache(session, user.id)

    # The mint, **after** the `last_login_at` commit above: `start_session`
    # owns its own guarded transaction, and it commits the row before the
    # cookie carrying its identifier leaves. `expected_session_version` is the
    # generation `verify_password` just ran against, so a reset that commits in
    # the window between that check and the guard refuses this mint instead of
    # handing the superseded password a fresh session. A refusal here means
    # exactly one of those two races was lost — a deactivation or a reset — and
    # either way nobody is signed in and no row exists to come back to life.
    if (
        await start_session(
            request,
            session,
            user.id,
            expected_session_version=user.session_version,
        )
        is None
    ):
        request.session.clear()
        # The credential was correct and the sign-in still did not happen, so
        # the attempt cannot go unrecorded — and it is not a success. The
        # subject is the client address, like every other `panel_login_failed`.
        security_events.emit(
            "panel_login_failed",
            subject=security_events.subject_for(request=request),
            reason="session_mint_refused",
            username_submitted=normalized,
            user_id=user.id,
            client_ip=security_events.client_ip(request),
            route=request.url.path,
        )
        return _render_login(
            request,
            error=invalid_msg,
            next_url=target,
            username=normalized,
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    # **After the mint**, never before it (D17, sharpened): the record must
    # assert something durable, and until `start_session` has committed a row
    # and returned its identifier there is no session to have succeeded. The
    # `last_login_at` commit alone was never enough — a mint refused by the
    # reset race above would otherwise have left a `panel_login_succeeded`
    # behind it.
    security_events.emit(
        "panel_login_succeeded",
        level=logging.INFO,
        subject=security_events.subject_for(user_id=user.id, request=request),
        user_id=user.id,
        username=user.username,
        client_ip=security_events.client_ip(request),
        route=request.url.path,
    )

    return RedirectResponse(target, status_code=status.HTTP_302_FOUND)


@router.post("/admin/auth/logout")
async def logout(request: Request, session: AsyncSession = Depends(get_session)):
    """Revoke this session server-side, then clear the cookie. Always redirects.

    Clearing the cookie was the whole of logout before #198, and it logs
    nobody out: Starlette answers `request.session.clear()` with an expiring
    `Set-Cookie`, while the copy an attacker already holds stays correctly
    signed until its itsdangerous timestamp ages out. The row is what makes
    "signed out" true for every other holder of that cookie.

    Only the presenting session is revoked — a logout on one device is not an
    account event.

    **Under `AUTH_MODE=pocketid` the redirect target changes and nothing else
    does.** The local revocation above is the part that matters and runs
    identically; then, when the provider advertises an `end_session_endpoint`,
    the browser is sent there so the *provider's* session ends too — otherwise
    "sign out" leaves a session that signs the person straight back in on their
    next click, which is the surprise this branch exists to remove. It is
    strictly best-effort: `end_session_url` answers `None` for a provider that
    advertises no such endpoint or cannot be reached, and the redirect falls
    back to `/admin/auth/login` — which under this mode starts a fresh
    authorization request anyway. A logout that has already revoked the local
    session may not fail.
    """
    # Read the session **before** it is cleared, and read nothing else: the
    # `_session` suffix is the provenance (D15). Both values are copied from
    # the session cookie without a database lookup, so they may name an account
    # that has since been renamed or deleted — which is exactly what a logout
    # can honestly say, and a logout must not pay for a query to say it.
    try:
        user_id_session = request.session.get("user_id")
        username_session = request.session.get("username")
        sid = request.session.get(SESSION_ID_KEY)
    except (AssertionError, AttributeError):
        user_id_session = None
        username_session = None
        sid = None

    revoked = 0
    failure: str | None = None
    if sid:
        try:
            revoked = await revoke_session(session, hash_session_id(sid))
            await session.commit()
        except Exception as exc:  # noqa: BLE001 - a logout may not 500
            # Failing closed here would leave the user signed in *and* the
            # cookie alive, which is worse than the state we are leaving:
            # clearing it removes this browser's copy — the common case, a
            # person walking away from a shared machine. The replay window
            # survives only for a copy already taken, which is what this
            # record is for.
            failure = type(exc).__name__
            try:
                await session.rollback()
            except Exception as rollback_exc:  # noqa: BLE001 - nor may this
                # A failing rollback must not escape either, or the sign-out
                # becomes the 500 the branch above exists to avoid.
                failure = f"{failure}/{type(rollback_exc).__name__}"

    security_events.emit(
        "panel_logout",
        level=logging.INFO,
        subject=security_events.subject_for(user_id=user_id_session, request=request),
        user_id_session=user_id_session,
        username_session=username_session,
        client_ip=security_events.client_ip(request),
    )
    if failure is not None:
        # The exception's **class name only** — never `str(exc)`, never
        # `exc_info`. SQLAlchemy renders the failing statement *and its bound
        # parameters* into the message, and one of those parameters here is
        # the stored session hash, i.e. the name of a specific live session.
        security_events.emit(
            "panel_session_revocation_failed",
            level=logging.ERROR,
            subject=security_events.subject_for(
                user_id=user_id_session, request=request
            ),
            reason="logout",
            # `_session`, not the unsuffixed name: the id was copied from the
            # cookie and no row was read (the provenance rule).
            user_id_session=user_id_session,
            error_type=failure,
            route=request.url.path,
            client_ip=security_events.client_ip(request),
        )
    elif sid:
        security_events.emit(
            "panel_sessions_revoked",
            level=logging.INFO,
            subject=security_events.subject_for(
                user_id=user_id_session, request=request
            ),
            reason="logout",
            user_id_session=user_id_session,
            count=revoked,
        )

    request.session.clear()

    target = "/admin/auth/login"
    if _federated():
        # `end_session_url` never raises — see its docstring. The
        # `post_logout_redirect_uri` is built on `BASE_URL` rather than on
        # anything from the request, so a `Host` header cannot steer where the
        # provider sends the browser next.
        provider_logout = await oidc.end_session_url(
            f"{settings.base_url.rstrip('/')}/admin/auth/login"
        )
        if provider_logout is not None:
            target = provider_logout
    return RedirectResponse(target, status_code=status.HTTP_302_FOUND)


# --- Bootstrap registration ----------------------------------------------


@router.get("/admin/register", response_class=HTMLResponse)
async def register_form(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    _require_local_password_route()
    # Bootstrap is closed once any user exists.
    if not await _users_table_empty(session):
        return RedirectResponse("/admin/auth/login", status_code=status.HTTP_302_FOUND)
    return _render_register(request)


@router.post("/admin/register")
async def register_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    vault_path: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    _require_local_password_route()
    # Early UX-friendly validation (no DB roundtrip).
    normalized = (username or "").strip().lower()
    if not _USERNAME_RE.match(normalized):
        _bootstrap_refused(request, "invalid_username")
        return _render_register(
            request,
            error="Username must be 1–64 chars, lowercase letters / digits / underscores only.",
            username=normalized,
            vault_path=vault_path,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    # The shared password policy (#197, D10). Bootstrap is the **fourth**
    # setter, and it used to carry its own eight-character rule, its own
    # confirmation compare, and no NUL check at all — so the most privileged
    # account on the server sat under the weakest minimum, and a NUL byte in
    # the field went straight into `hash_password`, which raises `ValueError`,
    # and came back as a 500. One validator, one minimum, and it runs here,
    # before the advisory-lock section: a refusal must not have taken the
    # bootstrap lock.
    message = validate_new_password(password, password_confirm)
    if message is not None:
        _bootstrap_refused(request, _bootstrap_password_reason(password, password_confirm))
        return _render_register(
            request,
            error=message,
            username=normalized,
            vault_path=vault_path,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    vault_path = (vault_path or "").strip()
    if not vault_path:
        _bootstrap_refused(request, "vault_path_missing")
        return _render_register(
            request,
            error="Vault path is required.",
            username=normalized,
            vault_path=vault_path,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    # Awaited: the existence check is a syscall against a bind mount, and
    # `validate_vault_root_path` runs it off the loop under a deadline.
    normalized_vp, vp_err = await validate_vault_root_path(vault_path)
    if vp_err:
        _bootstrap_refused(request, "vault_path_invalid")
        return _render_register(
            request,
            error=vp_err,
            username=normalized,
            vault_path=vault_path,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    vault_path = normalized_vp or vault_path

    # **No vault-root overlap check here, deliberately (#199).** The panel's
    # user-edit handler refuses an assignment that is identical to, contains,
    # or is contained by another active user's root; this path does not, and a
    # future reader should not read that as an omission. Bootstrap runs only
    # while `_users_table_empty` holds — zero rows — and the check's peer set is
    # "every *other* active user holding an assignment", which is empty by that
    # same invariant. A check that can never fire would invite the belief that
    # this path is covered by code, when what covers it is the invariant.
    # Anything assigned here is checked at the next detection entry point
    # anyway, like any root that changes underneath an assignment.

    # Critical section: take a transaction-scoped advisory lock so two
    # concurrent first-visits serialize. Inside the lock we re-check that
    # `users` is empty before inserting. The lock auto-releases on commit
    # or rollback.
    try:
        await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _BOOTSTRAP_LOCK_KEY})

        if not await _users_table_empty(session):
            # Someone else won the race. Don't reveal that to the form
            # (just send them to login).
            await session.rollback()
            _bootstrap_refused(request, "already_bootstrapped")
            return RedirectResponse(
                "/admin/auth/login", status_code=status.HTTP_302_FOUND
            )

        new_user = User(
            username=normalized,
            password_hash=hash_password(password),
            is_admin=True,
            is_active=True,
            vault_path=vault_path,
        )
        session.add(new_user)
        await session.flush()  # populate new_user.id

        # Backfill — bind every pre-flag-flip orphaned row to the new admin.
        # All inside the same transaction so a failure rolls everything back.
        uid = new_user.id
        await session.execute(
            update(APIKey).where(APIKey.user_id.is_(None)).values(user_id=uid)
        )
        await session.execute(
            update(OAuthClient).where(OAuthClient.user_id.is_(None)).values(user_id=uid)
        )
        await session.execute(
            update(OAuthToken).where(OAuthToken.user_id.is_(None)).values(user_id=uid)
        )
        await session.execute(
            update(OAuthCode).where(OAuthCode.user_id.is_(None)).values(user_id=uid)
        )
        await session.execute(
            update(NoteMetadata).where(NoteMetadata.user_id.is_(None)).values(user_id=uid)
        )
        await session.execute(
            update(UsageLog).where(UsageLog.user_id.is_(None)).values(user_id=uid)
        )

        # Stamp last_login_at since we're logging the new admin in immediately.
        new_user.last_login_at = datetime.now(timezone.utc)

        await session.commit()
    except Exception:
        await session.rollback()
        raise

    # After the commit, never after the insert (D17): a commit that then raises
    # would otherwise leave a record asserting an administrator exists who does
    # not — which is the one claim an operator reading this line must be able
    # to trust.
    security_events.emit(
        "panel_bootstrap_admin_created",
        level=logging.INFO,
        subject=security_events.subject_for(user_id=uid, request=request),
        user_id=uid,
        username=normalized,
        client_ip=security_events.client_ip(request),
    )

    # Warm the freshly-created admin's vault-path cache before any vault
    # tool call. The bootstrap flow flips us straight into /admin/ which
    # in phase 4 will load the dashboard for `uid`.
    await warm_user_vault_cache(session, uid)

    # The mint runs **after** the bootstrap transaction has committed, never
    # inside it: that transaction holds `USER_BOOTSTRAP_LOCK_KEY` and must not
    # be lengthened, and `start_session` takes the account guard for its own
    # transaction. The two keys are therefore taken **sequentially, never
    # nested**, so no path holds one while asking for the other and no cycle is
    # introduced.
    if (
        await start_session(
            request,
            session,
            uid,
            expected_session_version=new_user.session_version,
        )
        is None
    ):
        # The account it just created is gone or disabled — only reachable if
        # another administrator acted in that window. Nobody is signed in.
        request.session.clear()
        return RedirectResponse(
            "/admin/auth/login", status_code=status.HTTP_302_FOUND
        )

    return RedirectResponse("/admin/", status_code=status.HTTP_302_FOUND)
