"""Federated panel login (`AUTH_MODE=pocketid`) — the whole flow, end to end.

The identity provider is mocked with `respx` (the device
`tests/test_openai_provider.py` already uses for the embedding providers), and
the ID tokens are signed with a **throwaway RSA key generated in the fixture**.
No key material is committed, and a run that could not generate one would fail
rather than fall back to something weaker.

The handlers are driven directly with a hand-built `starlette.requests.Request`
and a fake session, which is `tests/test_auth_routes_real_hasher.py`'s harness:
it keeps the suite database-free while leaving the *real* `start_session`,
`verify_id_token`, `_safe_next` and `_resolve_federated_user` in the path.
Nothing about the cryptography or the redirect validation is stubbed — the
things worth testing here are exactly the things a stub would hide.

What every case is defending, in one line each:

* **`state`** — a callback nobody started must not reach the token endpoint.
* **`nonce`** — a token that is valid for this client must not be replayable
  into *this* browser's login.
* **`aud` / `iss` / signature / `exp`** — the four ways a forged or misdirected
  assertion gets in if any one of them is skipped.
* **the group** — authentication is not authorization.
* **`sub`, not email** — which local row a person lands on.
* **`next`** — an open redirect on the one route an attacker can make a browser
  visit while it is being handed a session.
* **the 404s** — a password form that still worked would be a way around all of
  the above.
"""
import base64
import itertools
import json
import time

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.requests import Request

from src.auth import oidc
from src.auth import routes as auth_routes
from src.config import settings
from src.limiter import limiter
from src.models.db import User

ISSUER = "https://idp.example.com"
CLIENT_ID = "obsidian-mcp-panel"
CLIENT_SECRET = "not-a-real-secret"
REDIRECT_URI = "https://obsidian-mcp.example.com/admin/auth/oidc/callback"
KID = "test-key-1"

DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
JWKS_URL = f"{ISSUER}/jwks"
TOKEN_URL = f"{ISSUER}/token"
AUTHORIZE_URL = f"{ISSUER}/authorize"
END_SESSION_URL = f"{ISSUER}/logout"


# ── The provider ────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def signing_key():
    """A throwaway 2048-bit RSA key, generated once per module.

    Module-scoped because generation is the slowest thing in this file and the
    key is an input to every case rather than state any of them mutates. It
    exists only in this process: nothing is written to disk and nothing is
    committed.
    """
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _b64uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _jwks(public_key, kid: str = KID) -> dict:
    numbers = public_key.public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": kid,
                "n": _b64uint(numbers.n),
                "e": _b64uint(numbers.e),
            }
        ]
    }


def _discovery(**overrides) -> dict:
    document = {
        "issuer": ISSUER,
        "authorization_endpoint": AUTHORIZE_URL,
        "token_endpoint": TOKEN_URL,
        "jwks_uri": JWKS_URL,
        "end_session_endpoint": END_SESSION_URL,
        "response_types_supported": ["code"],
        "id_token_signing_alg_values_supported": ["RS256"],
    }
    document.update(overrides)
    return document


def _id_token(signing_key, *, nonce, key=None, kid=KID, **claim_overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "provider-subject-1",
        "iat": now,
        "exp": now + 300,
        "nonce": nonce,
        "email": "max@example.com",
        "preferred_username": "max",
        "name": "Max",
        "groups": ["obsidian-mcp"],
    }
    claims.update(claim_overrides)
    return jwt.encode(
        claims,
        key if key is not None else signing_key,
        algorithm="RS256",
        headers={"kid": kid},
    )


@pytest.fixture(autouse=True)
def federated_settings(monkeypatch, signing_key):
    """Put the process into `AUTH_MODE=pocketid` and drop the OIDC caches.

    `settings` is a module-level singleton every module imports by reference,
    so patching attributes on it reaches `src.auth.oidc` and `src.auth.routes`
    alike. The cache reset runs on **both** sides of the test: a document
    cached here must not decide a later module's behaviour, and a document
    cached by a previous test must not decide this one's — the TTL is an hour,
    which is longer than any suite.
    """
    monkeypatch.setattr(settings, "auth_mode", "pocketid")
    monkeypatch.setattr(settings, "multi_user_mode", True)
    monkeypatch.setattr(settings, "oidc_issuer", ISSUER)
    monkeypatch.setattr(settings, "oidc_client_id", CLIENT_ID)
    monkeypatch.setattr(settings, "oidc_client_secret", CLIENT_SECRET)
    monkeypatch.setattr(settings, "oidc_redirect_uri", REDIRECT_URI)
    monkeypatch.setattr(settings, "oidc_required_group", None)
    monkeypatch.setattr(settings, "oidc_scopes", "openid profile email groups")
    oidc.reset_caches()
    yield
    oidc.reset_caches()


@pytest.fixture(autouse=True)
def _clean_rate_limiter():
    """slowapi's bucket store is a session-wide singleton; empty it both ways.

    `oidc_callback` carries `@limiter.limit("10/minute")` and this module has
    more than ten callback cases, so without this the later ones would 429 in
    file order — and hits recorded here would leak into unrelated modules.
    """
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def provider(signing_key):
    """respx routes for discovery, JWKS and the token endpoint.

    Yields the mock router so a test can override one route (a 500 on
    discovery, a rotated JWKS, a token response with no `id_token`) or count
    calls on it — the caching cases assert on `call_count` and would otherwise
    have nothing to read.
    """
    with respx.mock(assert_all_called=False) as mock:
        mock.get(DISCOVERY_URL).mock(
            return_value=httpx.Response(200, json=_discovery())
        )
        mock.get(JWKS_URL).mock(
            return_value=httpx.Response(200, json=_jwks(signing_key.public_key()))
        )
        yield mock


def _token_response(id_token: str, **extra):
    body = {
        "access_token": "an-access-token-nobody-reads",
        "token_type": "Bearer",
        "expires_in": 300,
        "id_token": id_token,
    }
    body.update(extra)
    return httpx.Response(200, json=body)


# ── The request / session harness ───────────────────────────────────────────

# `oidc_callback` is wrapped in slowapi's limiter, which buckets by client
# address in a process-wide store. A distinct address per request keeps the
# cases independent of each other and of collection order, exactly as
# `tests/test_auth_routes_real_hasher.py` does it.
_client_ips = itertools.count(1)


def _make_request(path: str, *, query: str = "", cookies: dict | None = None) -> Request:
    headers = [(b"host", b"testserver")]
    if cookies:
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers.append((b"cookie", cookie_header.encode()))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": query.encode(),
            "headers": headers,
            "client": (f"10.1.0.{next(_client_ips) % 250 + 1}", 12345),
            "server": ("testserver", 443),
            "session": {},
            "state": {},
        }
    )


class _Result:
    """The two accessors the handlers use on an `execute()` result."""

    def __init__(self, row=None):
        self._row = row

    def scalar_one_or_none(self):
        return self._row

    def scalar(self):
        return self._row

    def first(self):
        return self._row


class _FakeSession:
    """An in-memory `AsyncSession` that answers by *what was asked for*.

    A blanket `AsyncMock` cannot serve this flow: `_resolve_federated_user`
    issues three different `SELECT`s against `users` in one call — by
    `oidc_subject`, then by `oidc_subject` again under the lock, then by
    `username` — and a single canned `return_value` would answer all three the
    same way, which is precisely the adoption-versus-creation distinction the
    tests exist to check.

    So the statement is compiled and dispatched on its bound parameter name.
    That is stable across SQLAlchemy's rendering (the name comes from the
    column) and keeps the real query construction in the path, rather than
    replacing `_resolve_federated_user` with a stub of itself.
    """

    def __init__(self, users=()):
        self.users = list(users)
        self.added = []
        self.commits = 0
        self.rollbacks = 0
        self._next_id = itertools.count(100)

    async def execute(self, statement, params=None):
        rendered = str(statement)
        if "pg_advisory" in rendered:
            return _Result(None)
        try:
            bound = dict(statement.compile().params)
        except Exception:  # noqa: BLE001 - a text() UPDATE, say
            return _Result(None)

        if "oidc_subject_1" in bound:
            wanted = bound["oidc_subject_1"]
            return _Result(
                next((u for u in self.users if u.oidc_subject == wanted), None)
            )
        if "username_1" in bound:
            wanted = bound["username_1"]
            return _Result(next((u for u in self.users if u.username == wanted), None))
        if "id_1" in bound:
            wanted = bound["id_1"]
            row = next((u for u in self.users if u.id == wanted), None)
            if "users.password_hash" not in rendered:
                # Not a whole-entity select: this is
                # `warm_user_vault_cache`'s `(id, vault_path)` projection,
                # which filters on `vault_path IS NOT NULL` and calls
                # `.first()`. It must answer "no usable assignment" for a row
                # with no vault, which is every user in this module. The
                # discriminator is a column the entity select has and the
                # projection does not — checking for `vault_path` instead
                # would match *both*, which is how `start_session`'s re-read
                # first came back empty and refused every mint.
                return _Result(row if row is not None and row.vault_path else None)
            return _Result(row)
        return _Result(None)

    def add(self, obj):
        self.added.append(obj)
        if isinstance(obj, User) and obj.id is None:
            obj.id = next(self._next_id)
            self.users.append(obj)

    async def flush(self):
        for obj in self.added:
            if isinstance(obj, User) and obj.id is None:
                obj.id = next(self._next_id)
                self.users.append(obj)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _user(**kwargs) -> User:
    row = User(
        username=kwargs.pop("username", "max"),
        password_hash=kwargs.pop("password_hash", "$2b$12$" + "x" * 53),
        is_admin=kwargs.pop("is_admin", False),
        is_active=kwargs.pop("is_active", True),
        oidc_subject=kwargs.pop("oidc_subject", None),
        **kwargs,
    )
    # Columns with Python-side defaults are unset on an un-flushed instance.
    row.id = kwargs.get("id", 1)
    row.session_version = 1
    row.vault_path = None
    return row


async def _begin_login(next_url="/admin/", session=None):
    """Drive `GET /admin/auth/login` and return `(response, PendingLogin)`."""
    request = _make_request("/admin/auth/login")
    response = await auth_routes.login_form(
        request=request, next=next_url, session=session or _FakeSession()
    )
    sealed = _sealed_cookie(response)
    return response, (oidc.open_pending(sealed) if sealed else None)


def _sealed_cookie(response) -> str | None:
    """The `oidc_login` value a response sets, or None when it deletes/omits it.

    A deletion renders as `oidc_login=""` (Starlette quotes the empty value),
    so the empty and quoted-empty forms both mean "there is no login cookie any
    more" and must not read as a payload.
    """
    for raw in response.headers.getlist("set-cookie"):
        if raw.startswith(f"{oidc.LOGIN_COOKIE}="):
            value = raw.split("=", 1)[1].split(";", 1)[0].strip('"')
            return value or None
    return None


async def _run_callback(session, *, state, cookie, code="an-auth-code", error=""):
    request = _make_request(
        "/admin/auth/oidc/callback",
        cookies={oidc.LOGIN_COOKIE: cookie} if cookie else None,
    )
    return request, await auth_routes.oidc_callback(
        request=request, code=code, state=state, error=error, session=session
    )


# ── The authorization request ───────────────────────────────────────────────


async def test_login_redirects_to_the_provider_with_pkce_and_a_sealed_cookie(provider):
    response, pending = await _begin_login()

    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(AUTHORIZE_URL)

    query = dict(
        part.split("=", 1) for part in location.split("?", 1)[1].split("&")
    )
    assert query["response_type"] == "code"
    assert query["client_id"] == CLIENT_ID
    assert query["code_challenge_method"] == "S256"
    # The challenge on the wire is the S256 hash of the verifier in the cookie
    # — never the verifier itself, which is the whole of PKCE.
    assert query["code_challenge"] == oidc.code_challenge_for(pending.code_verifier)
    assert query["code_challenge"] != pending.code_verifier
    assert query["state"] == pending.state
    assert query["nonce"] == pending.nonce


async def test_the_login_cookie_is_httponly_and_short_lived(provider):
    response, _ = await _begin_login()
    raw = next(
        r
        for r in response.headers.getlist("set-cookie")
        if r.startswith(f"{oidc.LOGIN_COOKIE}=")
    )
    assert "httponly" in raw.lower()
    assert f"max-age={oidc.LOGIN_COOKIE_MAX_AGE}" in raw.lower()


async def test_state_nonce_and_verifier_differ_between_two_logins(provider):
    _, first = await _begin_login()
    _, second = await _begin_login()
    assert first.state != second.state
    assert first.nonce != second.nonce
    assert first.code_verifier != second.code_verifier


async def test_the_next_parameter_is_validated_before_it_is_sealed(provider):
    """An open redirect would be worth the most on exactly this route."""
    _, pending = await _begin_login(next_url="https://evil.example/steal")
    assert pending.next_url == "/admin/"

    _, pending = await _begin_login(next_url="//evil.example/steal")
    assert pending.next_url == "/admin/"

    # And a legitimate one survives — `GET /authorize` puts its whole URL here.
    _, pending = await _begin_login(next_url="/authorize?client_id=abc")
    assert pending.next_url == "/authorize?client_id=abc"


async def test_a_tampered_next_in_the_cookie_cannot_produce_an_open_redirect(
    provider, signing_key
):
    """The seal is the first guard; `_safe_next` on the way out is the second.

    Forging the signature is not possible, so the case that matters is a
    *validly signed* payload whose `next` is hostile — which is what a bug in
    the sealing path, or a future caller that seals an unvalidated value, would
    produce. The callback must still refuse to send the browser off-site.
    """
    pending = oidc.PendingLogin(
        state="s" * 32,
        nonce="n" * 32,
        code_verifier=oidc.new_code_verifier(),
        next_url="https://evil.example/steal",
    )
    cookie = oidc.seal_pending(pending)
    session = _FakeSession()
    provider.post(TOKEN_URL).mock(
        return_value=_token_response(_id_token(signing_key, nonce=pending.nonce))
    )

    _, response = await _run_callback(session, state=pending.state, cookie=cookie)
    assert response.status_code == 302
    assert response.headers["location"] == "/admin/"


async def test_the_provider_being_down_is_a_503_and_not_a_500(provider):
    provider.get(DISCOVERY_URL).mock(side_effect=httpx.ConnectError("refused"))
    oidc.reset_caches()
    response, pending = await _begin_login()
    assert response.status_code == 503
    assert pending is None


# ── Discovery caching ───────────────────────────────────────────────────────


async def test_discovery_is_fetched_once_and_then_served_from_the_cache(provider):
    first = await oidc.discovery_document()
    second = await oidc.discovery_document()
    assert first == second
    assert provider.get(DISCOVERY_URL).call_count == 1


async def test_resetting_the_cache_forces_a_refetch(provider):
    await oidc.discovery_document()
    oidc.reset_caches()
    await oidc.discovery_document()
    assert provider.get(DISCOVERY_URL).call_count == 2


async def test_the_jwks_is_cached_across_verifications(provider, signing_key):
    for _ in range(3):
        _, pending = await _begin_login()
        provider.post(TOKEN_URL).mock(
            return_value=_token_response(_id_token(signing_key, nonce=pending.nonce))
        )
        await _run_callback(
            _FakeSession(), state=pending.state, cookie=oidc.seal_pending(pending)
        )
    assert provider.get(JWKS_URL).call_count == 1
    assert provider.get(DISCOVERY_URL).call_count == 1


async def test_a_discovery_document_naming_another_issuer_is_refused(provider):
    provider.get(DISCOVERY_URL).mock(
        return_value=httpx.Response(200, json=_discovery(issuer="https://evil.example"))
    )
    oidc.reset_caches()
    with pytest.raises(oidc.OIDCError) as exc:
        await oidc.discovery_document()
    assert exc.value.reason == "issuer_mismatch"


async def test_a_discovery_document_with_a_plaintext_endpoint_is_refused(provider):
    provider.get(DISCOVERY_URL).mock(
        return_value=httpx.Response(
            200, json=_discovery(token_endpoint="http://idp.example.com/token")
        )
    )
    oidc.reset_caches()
    with pytest.raises(oidc.OIDCError) as exc:
        await oidc.discovery_document()
    assert exc.value.reason == "discovery_incomplete"


async def test_a_bad_discovery_document_is_not_cached(provider):
    provider.get(DISCOVERY_URL).mock(return_value=httpx.Response(500))
    oidc.reset_caches()
    with pytest.raises(oidc.OIDCError):
        await oidc.discovery_document()
    provider.get(DISCOVERY_URL).mock(
        return_value=httpx.Response(200, json=_discovery())
    )
    assert (await oidc.discovery_document())["issuer"] == ISSUER


# ── ID-token verification ───────────────────────────────────────────────────


async def _verified(signing_key, provider, **overrides):
    nonce = overrides.pop("nonce_used", "the-nonce")
    token = _id_token(signing_key, nonce=nonce, **overrides)
    return await oidc.verify_id_token(token, nonce=nonce)


async def test_a_well_formed_token_verifies_and_yields_its_claims(
    provider, signing_key
):
    identity = await _verified(signing_key, provider)
    assert identity.subject == "provider-subject-1"
    assert identity.email == "max@example.com"
    assert identity.groups == ("obsidian-mcp",)


async def test_a_token_signed_by_another_key_is_refused(provider, signing_key):
    impostor = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = _id_token(signing_key, nonce="n", key=impostor)
    with pytest.raises(oidc.OIDCError) as exc:
        await oidc.verify_id_token(token, nonce="n")
    # The signature is what failed; the point is that it is refused, and that
    # the refusal is a classified `OIDCError` rather than a leaked PyJWT type.
    assert exc.value.reason in ("bad_signature", "token_invalid")


async def test_a_token_for_another_audience_is_refused(provider, signing_key):
    with pytest.raises(oidc.OIDCError) as exc:
        await _verified(signing_key, provider, aud="some-other-client")
    assert exc.value.reason == "audience_mismatch"


async def test_a_token_from_another_issuer_is_refused(provider, signing_key):
    with pytest.raises(oidc.OIDCError) as exc:
        await _verified(signing_key, provider, iss="https://evil.example")
    assert exc.value.reason == "issuer_mismatch"


async def test_an_expired_token_is_refused(provider, signing_key):
    now = int(time.time())
    with pytest.raises(oidc.OIDCError) as exc:
        await _verified(
            signing_key, provider, iat=now - 4000, exp=now - 3600
        )
    assert exc.value.reason == "token_expired"


async def test_a_token_issued_in_the_future_is_refused(provider, signing_key):
    """PyJWT treats `iat` as informational; this server does not.

    Without the explicit check a token stamped a year ahead passes every other
    verification and stays valid for a year.
    """
    now = int(time.time())
    with pytest.raises(oidc.OIDCError) as exc:
        await _verified(
            signing_key, provider, iat=now + 86400, exp=now + 90000
        )
    assert exc.value.reason == "token_not_yet_valid"


async def test_a_token_missing_sub_is_refused(provider, signing_key):
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "iat": now,
            "exp": now + 300,
            "nonce": "n",
        },
        signing_key,
        algorithm="RS256",
        headers={"kid": KID},
    )
    with pytest.raises(oidc.OIDCError) as exc:
        await oidc.verify_id_token(token, nonce="n")
    assert exc.value.reason == "token_invalid"


async def test_a_multi_audience_token_needs_a_matching_azp(provider, signing_key):
    with pytest.raises(oidc.OIDCError) as exc:
        await _verified(
            signing_key,
            provider,
            aud=[CLIENT_ID, "another-client"],
            azp="another-client",
        )
    assert exc.value.reason == "audience_mismatch"

    identity = await _verified(
        signing_key,
        provider,
        aud=[CLIENT_ID, "another-client"],
        azp=CLIENT_ID,
    )
    assert identity.subject == "provider-subject-1"


async def test_an_unsigned_token_is_refused_before_any_key_lookup(provider, signing_key):
    """`alg: none` must never reach a decoder that might honour it."""
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "none", "kid": KID}).encode()
    ).decode().rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {
                "iss": ISSUER,
                "aud": CLIENT_ID,
                "sub": "x",
                "iat": int(time.time()),
                "exp": int(time.time()) + 300,
                "nonce": "n",
            }
        ).encode()
    ).decode().rstrip("=")
    with pytest.raises(oidc.OIDCError) as exc:
        await oidc.verify_id_token(f"{header}.{payload}.", nonce="n")
    assert exc.value.reason == "unsupported_algorithm"
    assert provider.get(JWKS_URL).call_count == 0


async def test_an_unknown_kid_inside_the_throttle_window_costs_no_refetch(
    provider, signing_key
):
    """`kid` is read from an *unverified* header, so it is attacker-chosen.

    Without the throttle, a stream of forged tokens naming random `kid`s is one
    outbound request to the provider each — an amplifier this server hands out
    on an unauthenticated route. Inside the window the refusal is answered from
    the cache and nothing leaves the process. The cost is bounded and
    deliberate: a key rotation is invisible for at most
    `_JWKS_REFRESH_MIN_INTERVAL_SECONDS`, which the next test covers.
    """
    await oidc.verify_id_token(_id_token(signing_key, nonce="n"), nonce="n")
    assert provider.get(JWKS_URL).call_count == 1

    for forged in ("a-kid-nobody-published", "another-forged-kid"):
        with pytest.raises(oidc.OIDCError) as exc:
            await oidc.verify_id_token(
                _id_token(signing_key, nonce="n", kid=forged), nonce="n"
            )
        assert exc.value.reason == "unknown_key"
    assert provider.get(JWKS_URL).call_count == 1


async def test_a_rotated_key_is_picked_up_once_the_throttle_has_elapsed(
    monkeypatch, provider
):
    """The other half of the same mechanism: rotation must actually work.

    The throttle is patched to zero rather than slept through — the behaviour
    under test is "an unknown `kid` refetches", and the window's length is the
    previous test's subject.
    """
    monkeypatch.setattr(oidc, "_JWKS_REFRESH_MIN_INTERVAL_SECONDS", 0)

    old = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    provider.get(JWKS_URL).mock(
        return_value=httpx.Response(200, json=_jwks(old.public_key(), kid="old"))
    )
    await oidc.verify_id_token(_id_token(old, nonce="n", kid="old"), nonce="n")
    assert provider.get(JWKS_URL).call_count == 1

    new = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    provider.get(JWKS_URL).mock(
        return_value=httpx.Response(200, json=_jwks(new.public_key(), kid="new"))
    )
    identity = await oidc.verify_id_token(
        _id_token(new, nonce="n", kid="new"), nonce="n"
    )
    assert identity.subject == "provider-subject-1"
    assert provider.get(JWKS_URL).call_count == 2


# ── The callback ────────────────────────────────────────────────────────────


async def test_the_happy_path_creates_a_user_and_starts_a_session(
    provider, signing_key
):
    _, pending = await _begin_login(next_url="/admin/keys")
    provider.post(TOKEN_URL).mock(
        return_value=_token_response(_id_token(signing_key, nonce=pending.nonce))
    )
    session = _FakeSession()

    request, response = await _run_callback(
        session, state=pending.state, cookie=oidc.seal_pending(pending)
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/admin/keys"

    created = next(obj for obj in session.added if isinstance(obj, User))
    assert created.username == "max"
    assert created.oidc_subject == "provider-subject-1"
    # **Never auto-granted.** The provider says who somebody is, not what they
    # may do here, and a fresh row has no vault until an administrator assigns
    # one.
    assert created.is_admin is False
    assert created.vault_path is None

    # A real panel session was minted, not just a redirect.
    assert request.session["user_id"] == created.id
    assert request.session["sid"]

    # The login cookie does not outlive the callback.
    assert _sealed_cookie(response) is None


async def test_the_token_exchange_carries_the_verifier_and_the_client_secret(
    provider, signing_key
):
    _, pending = await _begin_login()
    route = provider.post(TOKEN_URL).mock(
        return_value=_token_response(_id_token(signing_key, nonce=pending.nonce))
    )
    await _run_callback(
        _FakeSession(), state=pending.state, cookie=oidc.seal_pending(pending)
    )

    body = dict(
        part.split("=", 1)
        for part in route.calls[0].request.content.decode().split("&")
    )
    assert body["grant_type"] == "authorization_code"
    assert body["code_verifier"] == pending.code_verifier
    assert body["client_secret"] == CLIENT_SECRET
    # The redirect URI is sent on the exchange as well as on the request, so
    # the two cannot disagree.
    assert body["redirect_uri"].replace("%3A", ":").replace("%2F", "/") == REDIRECT_URI


async def test_a_second_login_links_the_existing_local_account(
    provider, signing_key
):
    """Adoption: an account that already exists keeps everything it had."""
    existing = _user(username="max", is_admin=True, oidc_subject=None)
    existing.vault_path = None
    session = _FakeSession([existing])

    _, pending = await _begin_login()
    provider.post(TOKEN_URL).mock(
        return_value=_token_response(_id_token(signing_key, nonce=pending.nonce))
    )
    _, response = await _run_callback(
        session, state=pending.state, cookie=oidc.seal_pending(pending)
    )

    assert response.status_code == 302
    assert existing.oidc_subject == "provider-subject-1"
    # Adopted, not replaced: no new row, and the admin flag it already carried
    # is neither granted nor revoked by the link.
    assert not [obj for obj in session.added if isinstance(obj, User)]
    assert existing.is_admin is True


async def test_a_third_login_resolves_by_subject_and_touches_nothing(
    provider, signing_key
):
    linked = _user(username="max", oidc_subject="provider-subject-1")
    session = _FakeSession([linked])

    _, pending = await _begin_login()
    provider.post(TOKEN_URL).mock(
        return_value=_token_response(_id_token(signing_key, nonce=pending.nonce))
    )
    _, response = await _run_callback(
        session, state=pending.state, cookie=oidc.seal_pending(pending)
    )

    assert response.status_code == 302
    assert not [obj for obj in session.added if isinstance(obj, User)]


async def test_the_link_follows_the_subject_when_the_email_changes(
    provider, signing_key
):
    """`sub` is the link, which is the whole reason it is not the email.

    The same provider account arriving with a new address must land on the same
    local row rather than creating a second one — and, more importantly, a
    *different* provider account inheriting that old address must not land on
    it, which is the case below.
    """
    linked = _user(username="max", oidc_subject="provider-subject-1")
    session = _FakeSession([linked])
    _, pending = await _begin_login()
    provider.post(TOKEN_URL).mock(
        return_value=_token_response(
            _id_token(
                signing_key,
                nonce=pending.nonce,
                email="maximilian@example.com",
                preferred_username="maximilian",
            )
        )
    )
    _, response = await _run_callback(
        session, state=pending.state, cookie=oidc.seal_pending(pending)
    )
    assert response.status_code == 302
    assert not [obj for obj in session.added if isinstance(obj, User)]


async def test_a_different_subject_cannot_take_over_a_linked_username(
    provider, signing_key
):
    """The account-takeover case the derivation would otherwise create.

    `max@a.example` and `max@b.example` both fold to the local name `max`. Once
    the first has linked, the second must be refused rather than adopted, or
    the second person inherits the first person's vault.
    """
    linked = _user(username="max", oidc_subject="provider-subject-1")
    session = _FakeSession([linked])

    _, pending = await _begin_login()
    provider.post(TOKEN_URL).mock(
        return_value=_token_response(
            _id_token(signing_key, nonce=pending.nonce, sub="a-different-subject")
        )
    )
    _, response = await _run_callback(
        session, state=pending.state, cookie=oidc.seal_pending(pending)
    )

    assert response.status_code == 403
    assert linked.oidc_subject == "provider-subject-1"
    assert not [obj for obj in session.added if isinstance(obj, User)]


async def test_a_deactivated_account_is_refused(provider, signing_key):
    disabled = _user(username="max", oidc_subject="provider-subject-1", is_active=False)
    session = _FakeSession([disabled])
    _, pending = await _begin_login()
    provider.post(TOKEN_URL).mock(
        return_value=_token_response(_id_token(signing_key, nonce=pending.nonce))
    )
    request, response = await _run_callback(
        session, state=pending.state, cookie=oidc.seal_pending(pending)
    )
    assert response.status_code == 403
    assert "user_id" not in request.session


async def test_a_token_with_no_usable_name_claim_is_refused(provider, signing_key):
    _, pending = await _begin_login()
    provider.post(TOKEN_URL).mock(
        return_value=_token_response(
            _id_token(
                signing_key, nonce=pending.nonce, email=None, preferred_username=None
            )
        )
    )
    session = _FakeSession()
    _, response = await _run_callback(
        session, state=pending.state, cookie=oidc.seal_pending(pending)
    )
    assert response.status_code == 403
    assert not [obj for obj in session.added if isinstance(obj, User)]


# ── The bindings: state, nonce, the cookie ──────────────────────────────────


async def test_a_callback_with_no_cookie_is_refused_without_calling_the_provider(
    provider, signing_key
):
    route = provider.post(TOKEN_URL).mock(
        return_value=_token_response(_id_token(signing_key, nonce="n"))
    )
    _, response = await _run_callback(_FakeSession(), state="anything", cookie=None)
    assert response.status_code == 400
    assert route.call_count == 0


async def test_a_state_mismatch_is_refused_before_the_token_exchange(
    provider, signing_key
):
    """CSRF: a callback the browser did not start must not reach the provider."""
    _, pending = await _begin_login()
    route = provider.post(TOKEN_URL).mock(
        return_value=_token_response(_id_token(signing_key, nonce=pending.nonce))
    )
    _, response = await _run_callback(
        _FakeSession(),
        state="a-state-from-somewhere-else",
        cookie=oidc.seal_pending(pending),
    )
    assert response.status_code == 400
    assert route.call_count == 0


async def test_an_empty_state_is_refused(provider, signing_key):
    _, pending = await _begin_login()
    _, response = await _run_callback(
        _FakeSession(), state="", cookie=oidc.seal_pending(pending)
    )
    assert response.status_code == 400


async def test_a_forged_cookie_is_refused(provider):
    _, response = await _run_callback(
        _FakeSession(), state="s", cookie="not.a.valid.signature"
    )
    assert response.status_code == 400


async def test_a_nonce_mismatch_is_refused(provider, signing_key):
    """A token that is otherwise perfectly valid, for a *different* login."""
    _, pending = await _begin_login()
    provider.post(TOKEN_URL).mock(
        return_value=_token_response(
            _id_token(signing_key, nonce="a-nonce-from-another-attempt")
        )
    )
    session = _FakeSession()
    request, response = await _run_callback(
        session, state=pending.state, cookie=oidc.seal_pending(pending)
    )
    assert response.status_code == 400
    assert "user_id" not in request.session
    assert not [obj for obj in session.added if isinstance(obj, User)]


async def test_every_refusal_clears_the_login_cookie(provider, signing_key):
    """A leftover state/nonce/verifier triple is a replayable one."""
    _, pending = await _begin_login()
    provider.post(TOKEN_URL).mock(
        return_value=_token_response(_id_token(signing_key, nonce="wrong"))
    )
    _, response = await _run_callback(
        _FakeSession(), state=pending.state, cookie=oidc.seal_pending(pending)
    )
    assert response.status_code == 400
    raw = next(
        r
        for r in response.headers.getlist("set-cookie")
        if r.startswith(f"{oidc.LOGIN_COOKIE}=")
    )
    assert 'oidc_login=""' in raw or "oidc_login=;" in raw


async def test_a_consent_denial_from_the_provider_is_refused(provider, signing_key):
    _, pending = await _begin_login()
    route = provider.post(TOKEN_URL).mock(
        return_value=_token_response(_id_token(signing_key, nonce=pending.nonce))
    )
    _, response = await _run_callback(
        _FakeSession(),
        state=pending.state,
        cookie=oidc.seal_pending(pending),
        code="",
        error="access_denied",
    )
    assert response.status_code == 400
    assert route.call_count == 0


async def test_a_token_endpoint_error_is_a_503(provider):
    _, pending = await _begin_login()
    provider.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "x"}))
    _, response = await _run_callback(
        _FakeSession(), state=pending.state, cookie=oidc.seal_pending(pending)
    )
    # `token_exchange_failed` is a caller-shaped failure, not a provider
    # outage, so it stays a 400.
    assert response.status_code == 400


# ── The group requirement ───────────────────────────────────────────────────


async def test_a_missing_required_group_is_refused(monkeypatch, provider, signing_key):
    monkeypatch.setattr(settings, "oidc_required_group", "obsidian-mcp-admins")
    _, pending = await _begin_login()
    provider.post(TOKEN_URL).mock(
        return_value=_token_response(
            _id_token(signing_key, nonce=pending.nonce, groups=["something-else"])
        )
    )
    session = _FakeSession()
    _, response = await _run_callback(
        session, state=pending.state, cookie=oidc.seal_pending(pending)
    )
    # Authenticated, not authorized: 403, and no row is created for somebody
    # who may not sign in.
    assert response.status_code == 403
    assert not [obj for obj in session.added if isinstance(obj, User)]


async def test_the_required_group_being_present_admits(
    monkeypatch, provider, signing_key
):
    monkeypatch.setattr(settings, "oidc_required_group", "obsidian-mcp-admins")
    _, pending = await _begin_login()
    provider.post(TOKEN_URL).mock(
        return_value=_token_response(
            _id_token(
                signing_key,
                nonce=pending.nonce,
                groups=["other", "obsidian-mcp-admins"],
            )
        )
    )
    session = _FakeSession()
    _, response = await _run_callback(
        session, state=pending.state, cookie=oidc.seal_pending(pending)
    )
    assert response.status_code == 302
    assert [obj for obj in session.added if isinstance(obj, User)]


async def test_an_absent_groups_claim_refuses_when_a_group_is_required(
    monkeypatch, provider, signing_key
):
    monkeypatch.setattr(settings, "oidc_required_group", "obsidian-mcp-admins")
    _, pending = await _begin_login()
    provider.post(TOKEN_URL).mock(
        return_value=_token_response(
            _id_token(signing_key, nonce=pending.nonce, groups=None)
        )
    )
    _, response = await _run_callback(
        _FakeSession(), state=pending.state, cookie=oidc.seal_pending(pending)
    )
    assert response.status_code == 403


async def test_no_required_group_admits_a_token_with_no_groups(
    provider, signing_key
):
    _, pending = await _begin_login()
    provider.post(TOKEN_URL).mock(
        return_value=_token_response(
            _id_token(signing_key, nonce=pending.nonce, groups=None)
        )
    )
    _, response = await _run_callback(
        _FakeSession(), state=pending.state, cookie=oidc.seal_pending(pending)
    )
    assert response.status_code == 302


async def test_a_group_requirement_is_exact_and_not_a_prefix(monkeypatch):
    monkeypatch.setattr(settings, "oidc_required_group", "admins")
    identity = oidc.VerifiedIdentity(
        subject="s",
        email=None,
        preferred_username=None,
        name=None,
        groups=("admins-readonly", "not-admins"),
    )
    assert oidc.group_allows(identity) is False


# ── The withdrawn local routes ──────────────────────────────────────────────


async def test_local_password_login_is_refused_under_pocketid():
    """The bypass this mode exists to close."""
    from fastapi import HTTPException

    request = _make_request("/admin/auth/login")
    with pytest.raises(HTTPException) as exc:
        await auth_routes.login_submit(
            request=request,
            username="max",
            password="correct horse battery staple",
            next="/admin/",
            session=_FakeSession([_user()]),
        )
    assert exc.value.status_code == 404


async def test_self_registration_is_refused_under_pocketid(tmp_path):
    from fastapi import HTTPException

    request = _make_request("/admin/register")
    with pytest.raises(HTTPException) as exc:
        await auth_routes.register_form(request=request, session=_FakeSession())
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException) as exc:
        await auth_routes.register_submit(
            request=request,
            username="newadmin",
            password="correct horse battery staple",
            password_confirm="correct horse battery staple",
            vault_path=str(tmp_path),
            session=_FakeSession(),
        )
    assert exc.value.status_code == 404


async def test_the_callback_does_not_exist_under_auth_mode_local(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(settings, "auth_mode", "local")
    with pytest.raises(HTTPException) as exc:
        await _run_callback(_FakeSession(), state="s", cookie="c")
    assert exc.value.status_code == 404


async def test_local_mode_still_renders_the_password_form(monkeypatch, provider):
    """The default path is untouched — the whole point of `AUTH_MODE=local`."""
    monkeypatch.setattr(settings, "auth_mode", "local")
    request = _make_request("/admin/auth/login")
    response = await auth_routes.login_form(
        request=request, next="/admin/", session=_FakeSession()
    )
    assert response.status_code == 200
    assert b"password" in response.body


# ── Logout ──────────────────────────────────────────────────────────────────


async def test_logout_redirects_to_the_provider_end_session_endpoint(provider):
    request = _make_request("/admin/auth/logout")
    response = await auth_routes.logout(request=request, session=_FakeSession())
    assert response.status_code == 302
    assert response.headers["location"].startswith(END_SESSION_URL)
    assert "post_logout_redirect_uri" in response.headers["location"]


async def test_logout_falls_back_to_the_login_page_without_an_end_session_endpoint(
    provider,
):
    provider.get(DISCOVERY_URL).mock(
        return_value=httpx.Response(
            200, json={k: v for k, v in _discovery().items() if k != "end_session_endpoint"}
        )
    )
    oidc.reset_caches()
    request = _make_request("/admin/auth/logout")
    response = await auth_routes.logout(request=request, session=_FakeSession())
    assert response.headers["location"] == "/admin/auth/login"


async def test_logout_survives_an_unreachable_provider(provider):
    """A logout that has already revoked the local session may not 500."""
    provider.get(DISCOVERY_URL).mock(side_effect=httpx.ConnectError("refused"))
    oidc.reset_caches()
    request = _make_request("/admin/auth/logout")
    response = await auth_routes.logout(request=request, session=_FakeSession())
    assert response.headers["location"] == "/admin/auth/login"


# ── Username derivation ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "email, preferred, expected",
    [
        ("max@example.com", None, "max"),
        ("Max.Mustermann@example.com", None, "max_mustermann"),
        ("max+tag@example.com", None, "max_tag"),
        (None, "someone", "someone"),
        # Neither claim yields anything in the alphabet: refused rather than
        # given a server-invented name no operator would recognise.
        ("@@@@@@@", None, None),
        (None, None, None),
    ],
)
def test_username_derivation(email, preferred, expected):
    identity = oidc.VerifiedIdentity(
        subject="s", email=email, preferred_username=preferred, name=None, groups=()
    )
    assert auth_routes._local_username_for(identity) == expected


# ── The sealed cookie itself ────────────────────────────────────────────────


def test_a_payload_sealed_under_the_oauth_salt_is_not_a_login_payload():
    """The two signed-cookie mechanisms share a key and must not share a use."""
    from src.oauth.routes import _state_serializer

    foreign = _state_serializer().dumps(
        {"state": "s", "nonce": "n", "verifier": "v", "next": "/admin/"}
    )
    assert oidc.open_pending(foreign) is None


def test_an_expired_seal_is_refused(monkeypatch):
    pending = oidc.PendingLogin(
        state="s", nonce="n", code_verifier="v", next_url="/admin/"
    )
    sealed = oidc.seal_pending(pending)
    assert oidc.open_pending(sealed) is not None
    monkeypatch.setattr(
        oidc, "LOGIN_COOKIE_MAX_AGE", -1
    )
    assert oidc.open_pending(sealed) is None


def test_a_seal_missing_a_field_is_refused():
    from src.auth.oidc import _login_serializer

    assert oidc.open_pending(_login_serializer().dumps({"state": "s"})) is None
    assert oidc.open_pending(None) is None
    assert oidc.open_pending("") is None


def test_pkce_challenge_is_s256_and_not_the_verifier():
    verifier = oidc.new_code_verifier()
    challenge = oidc.code_challenge_for(verifier)
    assert challenge != verifier
    assert len(challenge) == 43
    assert "=" not in challenge
