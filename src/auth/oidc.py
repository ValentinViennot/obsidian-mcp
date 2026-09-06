"""Federated panel login: discovery, PKCE, and ID-token verification.

This module is the whole of `AUTH_MODE=pocketid`'s cryptography and network
I/O. `src/auth/routes.py` owns the two routes and every database write; nothing
here touches a session, a cookie jar or a `users` row, so the verification can
be exercised without a request.

## What this is *not*

It is **not** a second authorization server, and it does not touch the one this
project already ships. `src/oauth/routes.py` keeps serving MCP clients with
dynamic client registration, PKCE, the consent screen, tokens, refresh and
revocation, unchanged, because PocketID's discovery document has no
`registration_endpoint` and the MCP clients require one. The only thing that
moves is *how the human behind the consent screen proves who they are*: the
`/admin/auth/login?next=…` redirect that `GET /authorize` already issues now
leads to somebody else's login page. That redirect's contract — path, query
parameter and meaning — is unchanged.

So this module is an OIDC **relying party**, and it is a confidential client.

## Four decisions worth not re-deriving

**PKCE, even though the client is confidential.** RFC 9700 §2.1.1 recommends it
for every authorization-code client, PocketID supports it, and it costs one
hash. The client secret protects the token *endpoint*; the verifier protects
the *code*, which travels through a browser redirect and a query string that
gets written to proxy logs. Neither substitutes for the other.

**`state`, `nonce`, `code_verifier` and `next` all live in one signed cookie.**
`src/oauth/routes.py` already binds its CSRF state to a signed
`oauth_state` cookie through `itsdangerous`; a second mechanism (a server-side
table, a second cookie per value) would be a second thing to expire, revoke and
reason about for no gain. So this reuses that shape with its own salt — a
`URLSafeTimedSerializer` keyed on `SECRET_KEY`, 10-minute `max_age`, four
values in one payload — and the cookie is deleted the moment the callback has
read it, on **every** path including the refusals.

The salt is what keeps the two apart: a value sealed as an OAuth state cannot
be presented as a login payload, and vice versa.

**The ID token is verified in full, and the `access_token` is never used.**
Signature (RS256, against a `kid` from the provider's JWKS), `iss`, `aud`,
`exp`, `iat`, and `nonce` — plus `azp` when the token is addressed to more than
one audience, which is the multi-audience clause of OIDC Core §3.1.3.7 and the
one that is usually skipped. Identity comes from the ID token's claims and from
nowhere else: no `userinfo` call, so there is no second document whose claims
could disagree with the signed one, and the access token is discarded
unread rather than stored.

**Nothing here fails open.** Every refusal is an `OIDCError` carrying a
`reason` from a closed vocabulary, and the caller renders one constant message
for all of them — the log is the only place the cause exists, exactly as
`login_submit`'s three password branches already work.

## The caches

Discovery and JWKS are cached with a TTL because they are fetched on the
authorization *request* path, which a person waits on. Both are refreshed on
expiry, and the JWKS additionally on an unknown `kid` — that is key rotation,
and a relying party that only refreshed on a timer would refuse every login for
up to an hour after the provider rolled its keys. The rotation refetch is
itself rate-limited (`_JWKS_REFRESH_MIN_INTERVAL_SECONDS`), because `kid` is
attacker-chosen: a forged header naming a random `kid` would otherwise be a
free outbound request to the provider on demand.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse

import httpx
import jwt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from jwt import PyJWKSet

from src.config import settings

#: The signed cookie carrying `state`, `nonce`, the PKCE verifier and the
#: validated `next` path between the redirect out and the callback back.
LOGIN_COOKIE = "oidc_login"

#: How long that cookie is honoured, in both directions: `max_age` on the
#: `Set-Cookie` and `max_age` on the signature check, so a stale one is refused
#: by the server even if a browser kept it. Ten minutes matches the
#: `oauth_state` cookie next door and is far longer than a login takes.
LOGIN_COOKIE_MAX_AGE = 600

#: Clock skew allowed on `exp` and `iat`. Small on purpose: the provider and
#: this server are both expected to be NTP-synchronised, and a generous leeway
#: is indistinguishable from a longer token lifetime.
CLOCK_SKEW_LEEWAY_SECONDS = 60

#: How long a fetched discovery document or JWKS is reused. An hour is the
#: usual provider guidance; the JWKS additionally refreshes on an unknown
#: `kid`, so a rotation is picked up long before this expires.
DISCOVERY_TTL_SECONDS = 3600
JWKS_TTL_SECONDS = 3600

#: The floor between two rotation-triggered JWKS refetches. `kid` comes out of
#: an unverified JWT header, so without this a caller could drive one outbound
#: request per forged token.
_JWKS_REFRESH_MIN_INTERVAL_SECONDS = 60

#: Both fetches. Short, because a person is waiting on the redirect.
_HTTP_TIMEOUT_SECONDS = 10.0

#: A provider that answers discovery with a megabyte is misconfigured or
#: hostile; either way the parse should not be what discovers it.
_MAX_DOCUMENT_BYTES = 512 * 1024

#: The only signature algorithm accepted. Asymmetric and explicitly listed, so
#: neither `none` nor an HMAC algorithm keyed on a public value can be
#: substituted by an attacker-supplied header.
_ALGORITHMS = ["RS256"]

#: Endpoints the discovery document must supply for the flow to run at all.
#: `end_session_endpoint` is deliberately absent — logout works without it.
_REQUIRED_ENDPOINTS = ("authorization_endpoint", "token_endpoint", "jwks_uri")


class OIDCError(Exception):
    """A refusal, carrying a `reason` from the module's closed vocabulary.

    Every caller renders one constant message for every reason, so this class
    is how the *log* distinguishes a forged signature from an expired token
    from a provider that is simply down. The message is for the operator
    reading a traceback in a test; it never reaches a browser.
    """

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


# ── The signed round-trip cookie ────────────────────────────────────────────


@dataclass(frozen=True)
class PendingLogin:
    """What the authorization request has to remember until the callback.

    Frozen because every field is an anti-replay or anti-forgery binding and
    nothing downstream has any business rewriting one.
    """

    state: str
    nonce: str
    code_verifier: str
    next_url: str


def _login_serializer() -> URLSafeTimedSerializer:
    """The `oauth_state` mechanism, under this module's own salt.

    Same primitive, same key, different salt — so a value sealed by the OAuth
    consent flow cannot be presented here as a login payload, and neither can
    a login payload be replayed as an OAuth CSRF state.
    """
    return URLSafeTimedSerializer(settings.secret_key, salt="oidc-login")


def seal_pending(pending: PendingLogin) -> str:
    return _login_serializer().dumps(
        {
            "state": pending.state,
            "nonce": pending.nonce,
            "verifier": pending.code_verifier,
            "next": pending.next_url,
        }
    )


def open_pending(sealed: str | None) -> PendingLogin | None:
    """The payload a `LOGIN_COOKIE` carries, or `None` for anything unusable.

    One return for a missing cookie, a forged one, an expired one and a
    well-signed one carrying the wrong shape: from the callback's point of view
    they are the same event — there is no login in flight — and a caller that
    had to distinguish them would grow four branches that all end in the same
    refusal.
    """
    if not sealed:
        return None
    try:
        payload = _login_serializer().loads(sealed, max_age=LOGIN_COOKIE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(payload, dict):
        return None
    values = {
        key: payload.get(key) for key in ("state", "nonce", "verifier", "next")
    }
    if not all(isinstance(value, str) and value for value in values.values()):
        return None
    return PendingLogin(
        state=values["state"],
        nonce=values["nonce"],
        code_verifier=values["verifier"],
        next_url=values["next"],
    )


# ── PKCE ────────────────────────────────────────────────────────────────────


def _b64url(raw: bytes) -> str:
    """Base64url with the padding stripped, which is what RFC 7636 specifies."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def new_code_verifier() -> str:
    """A 43-character verifier — 32 bytes of CSPRNG output, base64url-encoded.

    `token_urlsafe(32)` would also satisfy the grammar, but going through
    `_b64url` keeps the verifier and the challenge produced by one encoder, so
    a future change to one cannot silently desynchronise them.
    """
    return _b64url(secrets.token_bytes(32))


def code_challenge_for(verifier: str) -> str:
    """`S256`: base64url(sha256(verifier)). Never `plain`."""
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


# ── Discovery and JWKS, with TTL caches ─────────────────────────────────────


@dataclass
class _Cached:
    value: object
    fetched_at: float


_discovery: _Cached | None = None
_jwks: _Cached | None = None
_last_jwks_fetch: float = 0.0

# **Two locks, and they must stay two.** `asyncio.Lock` is not reentrant, and
# `_jwk_set` calls `discovery_document()` from inside its own critical section
# to learn `jwks_uri` — under one shared lock that is a self-deadlock, on the
# first verification whose discovery cache has expired. It hangs the request
# rather than failing it, which is the worst shape a bug in a login path can
# take.
#
# The order is one-way and that is what makes two locks safe: the JWKS fetch
# takes `_jwks_lock` and then `_discovery_lock`, and nothing ever goes the
# other way, so there is no cycle to deadlock on.
_discovery_lock = asyncio.Lock()
_jwks_lock = asyncio.Lock()


def reset_caches() -> None:
    """Drop both caches. For tests, and for nothing else.

    Production has no cache-invalidation path on purpose: the TTL and the
    rotation refetch are the whole policy, and an endpoint that let a caller
    clear them would be a free outbound request per call.
    """
    global _discovery, _jwks, _last_jwks_fetch
    _discovery = None
    _jwks = None
    _last_jwks_fetch = 0.0


async def _fetch_json(url: str, *, what: str) -> dict:
    """One GET, bounded in time and size, refused unless it is HTTPS JSON.

    `what` names the document in the refusal so an operator reading the log
    knows whether discovery or the JWKS is the thing that is unreachable.
    """
    if urlparse(url).scheme != "https":
        raise OIDCError("insecure_endpoint", f"{what} at {url!r} is not https")
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise OIDCError("provider_unreachable", f"{what}: {type(exc).__name__}") from exc
    if response.status_code != 200:
        raise OIDCError(
            "provider_error", f"{what} answered {response.status_code}"
        )
    if len(response.content) > _MAX_DOCUMENT_BYTES:
        raise OIDCError("provider_error", f"{what} is over {_MAX_DOCUMENT_BYTES} bytes")
    try:
        document = response.json()
    except ValueError as exc:
        raise OIDCError("provider_error", f"{what} is not JSON") from exc
    if not isinstance(document, dict):
        raise OIDCError("provider_error", f"{what} is not a JSON object")
    return document


def _validate_discovery(document: dict) -> dict:
    """The mix-up check, plus the three endpoints the flow cannot run without.

    **`issuer` must equal the configured one exactly.** That comparison is the
    defence against an issuer whose document points somewhere else: everything
    downstream — the authorization redirect, the token exchange, the JWKS this
    server will trust to verify an identity assertion — is taken from this
    document, so a document that does not claim to *be* the configured issuer
    is a document that must not be used. A trailing slash is normalised away on
    both sides because providers are inconsistent about it and it carries no
    meaning here.

    Each endpoint is required to be HTTPS for the same reason the issuer is:
    the client secret is posted to the token endpoint, and the JWKS decides who
    a person is.
    """
    claimed = str(document.get("issuer") or "").rstrip("/")
    expected = (settings.oidc_issuer or "").rstrip("/")
    if not claimed or claimed != expected:
        raise OIDCError(
            "issuer_mismatch",
            f"discovery claims issuer {claimed!r}, configured {expected!r}",
        )
    for name in _REQUIRED_ENDPOINTS:
        value = document.get(name)
        if not isinstance(value, str) or urlparse(value).scheme != "https":
            raise OIDCError(
                "discovery_incomplete", f"{name} is missing or not https"
            )
    return document


async def discovery_document() -> dict:
    """The provider's discovery document, cached for `DISCOVERY_TTL_SECONDS`.

    The cache is populated only with a document that has already passed
    `_validate_discovery`, so no caller has to re-check it and a bad document
    is never the thing that is remembered for an hour.
    """
    global _discovery
    cached = _discovery
    now = time.monotonic()
    if cached is not None and now - cached.fetched_at < DISCOVERY_TTL_SECONDS:
        return cached.value  # type: ignore[return-value]

    async with _discovery_lock:
        # Re-read under the lock: a concurrent login may have filled it while
        # this coroutine waited, and a second fetch would be pure waste.
        cached = _discovery
        now = time.monotonic()
        if cached is not None and now - cached.fetched_at < DISCOVERY_TTL_SECONDS:
            return cached.value  # type: ignore[return-value]
        issuer = (settings.oidc_issuer or "").rstrip("/")
        document = _validate_discovery(
            await _fetch_json(
                f"{issuer}/.well-known/openid-configuration", what="discovery"
            )
        )
        _discovery = _Cached(value=document, fetched_at=time.monotonic())
        return document


async def _jwk_set(*, force: bool = False) -> PyJWKSet:
    """The provider's signing keys, cached and refreshable on rotation.

    `force` is the rotation path and is rate-limited by
    `_JWKS_REFRESH_MIN_INTERVAL_SECONDS`: it is reached from an unknown `kid`,
    and `kid` is read out of an *unverified* JWT header, so an unbounded forced
    refetch would be an outbound request an attacker can trigger at will.
    """
    global _jwks, _last_jwks_fetch
    cached = _jwks
    now = time.monotonic()
    if (
        not force
        and cached is not None
        and now - cached.fetched_at < JWKS_TTL_SECONDS
    ):
        return cached.value  # type: ignore[return-value]

    async with _jwks_lock:
        cached = _jwks
        now = time.monotonic()
        if cached is not None:
            fresh = now - cached.fetched_at < JWKS_TTL_SECONDS
            throttled = now - _last_jwks_fetch < _JWKS_REFRESH_MIN_INTERVAL_SECONDS
            if (not force and fresh) or (force and throttled):
                return cached.value  # type: ignore[return-value]

        document = await discovery_document()
        raw = await _fetch_json(str(document["jwks_uri"]), what="jwks")
        _last_jwks_fetch = time.monotonic()
        try:
            key_set = PyJWKSet.from_dict(raw)
        except Exception as exc:  # noqa: BLE001 - PyJWT raises several types
            raise OIDCError("jwks_invalid", type(exc).__name__) from exc
        _jwks = _Cached(value=key_set, fetched_at=time.monotonic())
        return key_set


async def _signing_key(kid: str | None):
    """The `PyJWK` for `kid`, refetching the set once if it is not there.

    An absent `kid` is refused rather than falling back to "the only key in the
    set": a set with one key today has two the day the provider starts a
    rotation, and a relying party that guesses would then be picking a key by
    position.
    """
    if not kid:
        raise OIDCError("token_invalid", "the ID token header carries no kid")
    for attempt_force in (False, True):
        key_set = await _jwk_set(force=attempt_force)
        for key in key_set.keys:
            if key.key_id == kid:
                return key
    raise OIDCError("unknown_key", f"no signing key for kid {kid!r}")


# ── The authorization request ───────────────────────────────────────────────


async def authorization_request(next_url: str) -> tuple[str, PendingLogin]:
    """Build the provider redirect and the values the callback must match.

    Returns the absolute authorization URL and the `PendingLogin` the caller
    seals into `LOGIN_COOKIE`. `next_url` is stored **already validated** — the
    caller runs it through `_safe_next` before calling — so the callback can
    redirect to it without re-deriving trust from a value that has by then made
    a round trip through the browser. It is inside the *signed* payload, which
    is what stops a tampered cookie from turning the callback into an open
    redirect.
    """
    document = await discovery_document()
    pending = PendingLogin(
        state=secrets.token_urlsafe(32),
        nonce=secrets.token_urlsafe(32),
        code_verifier=new_code_verifier(),
        next_url=next_url,
    )
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.oidc_client_id,
            "redirect_uri": settings.oidc_redirect_uri,
            "scope": settings.oidc_scopes,
            "state": pending.state,
            "nonce": pending.nonce,
            "code_challenge": code_challenge_for(pending.code_verifier),
            "code_challenge_method": "S256",
        }
    )
    endpoint = str(document["authorization_endpoint"])
    separator = "&" if urlparse(endpoint).query else "?"
    return f"{endpoint}{separator}{query}", pending


# ── The token exchange ──────────────────────────────────────────────────────


async def exchange_code(code: str, code_verifier: str) -> str:
    """Redeem `code` at the token endpoint and return the raw ID token.

    `client_secret_post` rather than HTTP Basic: PocketID accepts both, the
    body form keeps the secret out of an `Authorization` header that proxies
    are more likely to log, and it is the method
    `src/oauth/routes.py` already speaks on its own side of the fence.

    **The response's `access_token` is not read.** Everything this flow needs
    is in the ID token, and a token nobody stores is a token nobody can leak.
    """
    document = await discovery_document()
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.oidc_redirect_uri,
        "client_id": settings.oidc_client_id,
        "client_secret": settings.oidc_client_secret,
        "code_verifier": code_verifier,
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(
                str(document["token_endpoint"]),
                data=payload,
                headers={"Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise OIDCError("provider_unreachable", type(exc).__name__) from exc

    if response.status_code != 200:
        # The provider's own `error` code, when it sent one, and never its
        # `error_description`: that is provider-authored free text on an
        # unauthenticated path, and `reason` is a closed vocabulary.
        raise OIDCError("token_exchange_failed", f"status {response.status_code}")
    try:
        body = response.json()
    except ValueError as exc:
        raise OIDCError("token_exchange_failed", "response is not JSON") from exc
    id_token = body.get("id_token") if isinstance(body, dict) else None
    if not isinstance(id_token, str) or not id_token:
        raise OIDCError("token_exchange_failed", "response carries no id_token")
    return id_token


# ── ID-token verification ───────────────────────────────────────────────────


@dataclass(frozen=True)
class VerifiedIdentity:
    """The claims a verified ID token asserts, and nothing derived from them.

    `subject` is the `sub` claim: stable for the life of the account at the
    provider, which is why it and not the email is what a local row is linked
    by. An email address is reassignable, and a provider that let one person
    take another's former address would otherwise hand them that person's
    vault.
    """

    subject: str
    email: str | None
    preferred_username: str | None
    name: str | None
    groups: tuple[str, ...]


def _claim_str(claims: dict, name: str) -> str | None:
    value = claims.get(name)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _groups(claims: dict) -> tuple[str, ...]:
    """The `groups` claim as a tuple of strings.

    A provider may render it as a list or, less commonly, as one
    space-separated string; both are accepted and anything else yields the
    empty tuple, which is the safe direction — an unparseable claim grants no
    membership, so `OIDC_REQUIRED_GROUP` refuses rather than admits.
    """
    raw = claims.get("groups")
    if isinstance(raw, str):
        return tuple(part for part in raw.split() if part)
    if isinstance(raw, (list, tuple)):
        return tuple(item for item in raw if isinstance(item, str) and item)
    return ()


async def verify_id_token(id_token: str, *, nonce: str) -> VerifiedIdentity:
    """Verify signature, `iss`, `aud`, `exp`, `iat`, `azp` and `nonce`.

    The order matters only in that the signature is checked first: every claim
    below is read from a token whose bytes the provider has already vouched
    for, so none of them is attacker-controlled by the time it is compared.

    Three things PyJWT does not do on its own, done here:

    * **`iat` in the future** is refused. PyJWT treats `iat` as informational
      and only requires it to be a number, so a token stamped a year ahead
      would otherwise pass every other check and stay valid for a year.
    * **`azp`**, when `aud` names more than one audience. OIDC Core §3.1.3.7
      requires it there, and it is the clause that keeps a token minted for a
      *different* client — one that happens to also list this client id as an
      audience — from being replayed here.
    * **`nonce`**, compared in constant time against the value sealed into the
      login cookie. This is what binds the token to *this* browser's login
      attempt rather than to any valid token for this client.
    """
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise OIDCError("token_invalid", type(exc).__name__) from exc

    algorithm = header.get("alg")
    if algorithm not in _ALGORITHMS:
        # Refused before a key is even looked up, so an `alg` of `none` or
        # `HS256` never reaches a decoder that might honour it.
        raise OIDCError("unsupported_algorithm", f"alg {algorithm!r}")

    key = await _signing_key(header.get("kid"))

    try:
        claims = jwt.decode(
            id_token,
            key=key,
            algorithms=_ALGORITHMS,
            audience=settings.oidc_client_id,
            issuer=(settings.oidc_issuer or "").rstrip("/"),
            leeway=CLOCK_SKEW_LEEWAY_SECONDS,
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_iat": True,
                "verify_aud": True,
                "verify_iss": True,
                # Presence, not merely well-formedness: a token missing `sub`
                # asserts no identity, and one missing `exp` never expires.
                "require": ["iss", "aud", "exp", "iat", "sub"],
            },
        )
    except jwt.ExpiredSignatureError as exc:
        raise OIDCError("token_expired", type(exc).__name__) from exc
    except jwt.InvalidAudienceError as exc:
        raise OIDCError("audience_mismatch", type(exc).__name__) from exc
    except jwt.InvalidIssuerError as exc:
        raise OIDCError("issuer_mismatch", type(exc).__name__) from exc
    except jwt.InvalidSignatureError as exc:
        raise OIDCError("bad_signature", type(exc).__name__) from exc
    except jwt.ImmatureSignatureError as exc:
        # PyJWT raises this for an `iat` (or `nbf`) in the future. Mapped to
        # the same reason the explicit check below produces, so the log says
        # one thing about one condition whichever layer caught it — and the
        # check below stays, because *which* of `iat`/`nbf` PyJWT enforces has
        # moved between releases and the guarantee must not be a library's.
        raise OIDCError("token_not_yet_valid", type(exc).__name__) from exc
    except jwt.PyJWTError as exc:
        raise OIDCError("token_invalid", type(exc).__name__) from exc

    issued_at = claims.get("iat")
    if not isinstance(issued_at, (int, float)) or isinstance(issued_at, bool):
        raise OIDCError("token_invalid", "iat is not a number")
    if issued_at > time.time() + CLOCK_SKEW_LEEWAY_SECONDS:
        raise OIDCError("token_not_yet_valid", "iat is in the future")

    audience = claims.get("aud")
    if isinstance(audience, (list, tuple)) and len(audience) > 1:
        if claims.get("azp") != settings.oidc_client_id:
            raise OIDCError(
                "audience_mismatch",
                "multi-audience token whose azp is not this client",
            )

    presented_nonce = claims.get("nonce")
    if not isinstance(presented_nonce, str) or not secrets.compare_digest(
        presented_nonce, nonce
    ):
        raise OIDCError("nonce_mismatch", "the ID token's nonce is not this login's")

    subject = _claim_str(claims, "sub")
    if not subject:
        raise OIDCError("token_invalid", "sub is empty")

    return VerifiedIdentity(
        subject=subject,
        email=_claim_str(claims, "email"),
        preferred_username=_claim_str(claims, "preferred_username"),
        name=_claim_str(claims, "name"),
        groups=_groups(claims),
    )


def group_allows(identity: VerifiedIdentity) -> bool:
    """Whether `OIDC_REQUIRED_GROUP` (if set) is among the token's groups.

    Unset means every account the provider authenticates may sign in, which is
    a deliberate configuration and not a default hole: PocketID gates *client
    assignment* per user, so an unset value defers to the provider's own
    policy. When it is set, the check is exact string membership — no prefix,
    no case folding, no path semantics, because a group name is an opaque
    identifier and every looser comparison here is a way for one group to
    satisfy another's requirement.
    """
    required = settings.oidc_required_group
    if not required:
        return True
    return required in identity.groups


async def end_session_url(post_logout_redirect: str) -> str | None:
    """The provider's RP-initiated logout URL, or `None` when it has none.

    Best-effort by construction: the local session is already revoked by the
    time this is consulted, so a provider that advertises no
    `end_session_endpoint` — or one that is unreachable — costs the user
    nothing but a still-live session *at the provider*, which is the provider's
    to end. Never raises for that reason; a refusal here would turn a
    successful logout into a 500.
    """
    try:
        document = await discovery_document()
    except OIDCError:
        return None
    endpoint = document.get("end_session_endpoint")
    if not isinstance(endpoint, str) or urlparse(endpoint).scheme != "https":
        return None
    query = urlencode(
        {
            "client_id": settings.oidc_client_id,
            "post_logout_redirect_uri": post_logout_redirect,
        }
    )
    separator = "&" if urlparse(endpoint).query else "?"
    return f"{endpoint}{separator}{query}"
