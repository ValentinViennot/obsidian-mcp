"""Verifies provider config validation at instantiation time."""
import pytest
from pydantic import ValidationError

from src.config import Settings


def test_openai_provider_requires_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValidationError) as exc:
        Settings(
            embedding_provider="openai",
            openai_api_key=None,
            _env_file=None,
        )
    assert "OPENAI_API_KEY" in str(exc.value)


def test_openai_provider_with_empty_string_api_key():
    with pytest.raises(ValidationError):
        Settings(
            embedding_provider="openai",
            openai_api_key="   ",
            _env_file=None,
        )


def test_openai_provider_with_valid_key():
    s = Settings(
        embedding_provider="openai",
        openai_api_key="sk-test",
        _env_file=None,
    )
    assert s.embedding_provider == "openai"


def test_ollama_default_no_key_required():
    s = Settings(_env_file=None)
    assert s.embedding_provider == "ollama"


def test_invalid_provider_value():
    with pytest.raises(ValidationError):
        Settings(embedding_provider="cohere", _env_file=None)


def test_public_base_url_requires_https():
    with pytest.raises(ValidationError) as exc:
        Settings(base_url="http://mcp.example.com", _env_file=None)
    assert "HTTPS" in str(exc.value)


def test_loopback_base_url_may_use_http():
    settings = Settings(base_url="http://127.0.0.1:8000/", _env_file=None)
    assert settings.base_url == "http://127.0.0.1:8000"


def test_explicit_empty_allowed_hosts_still_allows_localhost_healthcheck():
    settings = Settings(allowed_hosts=[], _env_file=None)
    assert settings.allowed_hosts == ["localhost"]


def test_base_url_must_match_public_hostname():
    with pytest.raises(ValidationError):
        Settings(
            mcp_hostname="mcp.example.com",
            base_url="https://other.example.com",
            _env_file=None,
        )


# ── Filtered dotenv source ──────────────────────────────────────────────────
# The repo-root `.env` doubles as the compose env file and carries compose-only
# keys. They must not abort `Settings()` (which happens at import of
# `src.config`, so it breaks collection of any single test file), while
# `extra="forbid"` must stay in force everywhere else.


def test_dotenv_compose_only_keys_are_ignored(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=postgresql+asyncpg://u:p@localhost/db\n"
        "SECRET_KEY=not-a-placeholder\n"
        "EMBEDDING_MODEL=from-dotenv\n"
        "VAULT_HOST_PATH=/x\n"
        "BACKUPS_HOST_PATH=/y/backups\n"
    )
    settings = Settings(_env_file=str(env_file))
    # The compose-only keys are dropped rather than rejected...
    assert not hasattr(settings, "vault_host_path")
    # ...and real settings from the same file are still applied.
    assert settings.embedding_model == "from-dotenv"


def test_misspelled_constructor_kwarg_still_raises():
    with pytest.raises(ValidationError) as exc:
        Settings(databse_url="postgresql+asyncpg://u:p@localhost/db", _env_file=None)
    assert "databse_url" in str(exc.value)


def test_unknown_dotenv_key_does_not_leak_into_settings(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SECRET_KEY=not-a-placeholder\n"
        "SOME_COMPOSE_ONLY_KEY=whatever\n"
    )
    settings = Settings(_env_file=str(env_file))
    assert "some_compose_only_key" not in settings.model_dump()


# A non-placeholder SECRET_KEY, so `_validate_multi_user_secret` (which runs
# before the sandbox guard) never pre-empts the refusal under test. Passing it
# explicitly also keeps these cases independent of the host environment.
_SECRET = "0123456789abcdef0123456789abcdef"


# ── CORS wildcard (#129) ────────────────────────────────────────────────────
# `src/main.py` installs CORSMiddleware with `allow_credentials=True`, which
# makes Starlette treat `allow_origins=["*"]` as "reflect any Origin". The
# derived values are never `*`, so the refusal only fires on an override.


def test_wildcard_allowed_origin_is_refused():
    with pytest.raises(ValidationError) as exc:
        Settings(allowed_origins=["*"], secret_key=_SECRET, _env_file=None)
    assert "ALLOWED_ORIGINS" in str(exc.value)
    assert "allow_credentials" in str(exc.value)


def test_wildcard_among_other_allowed_origins_is_refused():
    with pytest.raises(ValidationError):
        Settings(
            allowed_origins=["https://mcp.example.com", " * "],
            secret_key=_SECRET, _env_file=None,
        )


def test_explicit_allowed_origins_without_wildcard_still_boot():
    settings = Settings(
        allowed_origins=["https://mcp.example.com"],
        secret_key=_SECRET, _env_file=None,
    )
    assert settings.allowed_origins == ["https://mcp.example.com"]


def test_derived_allowed_origins_never_trip_the_wildcard_guard():
    settings = Settings(mcp_hostname="mcp.example.com", secret_key=_SECRET, _env_file=None)
    assert settings.allowed_origins == ["https://mcp.example.com"]
    assert Settings(secret_key=_SECRET, _env_file=None).allowed_origins == ["http://localhost:8000"]


# ── Sandbox mode may only ever be loopback (#129) ───────────────────────────
# MCP_SANDBOX_MODE bypasses authentication on /mcp, so every setting that can
# admit outside traffic has to be loopback — not just MCP_HOSTNAME.


def test_sandbox_mode_refuses_public_hostname():
    with pytest.raises(ValidationError) as exc:
        Settings(mcp_sandbox_mode=True, mcp_hostname="mcp.example.com", secret_key=_SECRET, _env_file=None)
    assert "MCP_HOSTNAME" in str(exc.value)


def test_sandbox_mode_refuses_public_base_url():
    with pytest.raises(ValidationError) as exc:
        Settings(
            mcp_sandbox_mode=True,
            base_url="https://mcp.example.com",
            secret_key=_SECRET, _env_file=None,
        )
    assert "BASE_URL" in str(exc.value)


def test_sandbox_mode_refuses_public_allowed_host():
    with pytest.raises(ValidationError) as exc:
        Settings(
            mcp_sandbox_mode=True,
            allowed_hosts=["mcp.example.com"],
            secret_key=_SECRET, _env_file=None,
        )
    assert "ALLOWED_HOSTS" in str(exc.value)


def test_sandbox_mode_refuses_wildcard_allowed_host():
    # `*` makes TrustedHostMiddleware answer for every hostname, which is the
    # most public setting there is.
    with pytest.raises(ValidationError) as exc:
        Settings(mcp_sandbox_mode=True, allowed_hosts=["*"], secret_key=_SECRET, _env_file=None)
    assert "ALLOWED_HOSTS" in str(exc.value)


def test_sandbox_mode_with_defaults_still_boots():
    settings = Settings(mcp_sandbox_mode=True, secret_key=_SECRET, _env_file=None)
    assert settings.base_url == "http://localhost:8000"
    assert settings.allowed_hosts == ["localhost"]


@pytest.mark.parametrize("base_url", ["http://localhost:8000", "http://127.0.0.1:9000"])
def test_sandbox_mode_with_loopback_base_url_still_boots(base_url):
    settings = Settings(mcp_sandbox_mode=True, base_url=base_url, secret_key=_SECRET, _env_file=None)
    assert settings.mcp_sandbox_mode is True


def test_sandbox_mode_with_loopback_allowed_hosts_still_boots():
    settings = Settings(
        mcp_sandbox_mode=True,
        allowed_hosts=["127.0.0.1", "::1"],
        secret_key=_SECRET, _env_file=None,
    )
    assert settings.allowed_hosts == ["127.0.0.1", "::1", "localhost"]


def test_public_hostname_without_sandbox_mode_is_unaffected():
    settings = Settings(mcp_hostname="mcp.example.com", secret_key=_SECRET, _env_file=None)
    assert settings.allowed_hosts == ["mcp.example.com", "localhost"]


# ── MCP_HOSTNAME is folded to a bare lowercase host (#130) ──────────────────
# `allowed_hosts` is derived from it and Starlette's TrustedHostMiddleware
# compares the Host header exactly, while browsers and proxies send it
# lowercased. So a capitalised hostname booted clean, kept its casing into the
# derivation, and then 400'd every public request while the localhost health
# check stayed green.


def test_mcp_hostname_is_lowercased_before_anything_is_derived():
    settings = Settings(mcp_hostname="Vault.Example.COM", secret_key=_SECRET, _env_file=None)

    assert settings.mcp_hostname == "vault.example.com"
    assert settings.allowed_hosts == ["vault.example.com", "localhost"]
    assert settings.allowed_origins == ["https://vault.example.com"]
    assert settings.base_url == "https://vault.example.com"


def test_mcp_hostname_is_stripped():
    settings = Settings(mcp_hostname="  vault.example.com \n", secret_key=_SECRET, _env_file=None)

    assert settings.mcp_hostname == "vault.example.com"
    assert settings.base_url == "https://vault.example.com"


def test_a_whitespace_only_hostname_is_no_hostname():
    # Not `None`-ing it derived `https://` as the base URL and reported a
    # public origin the operator never configured.
    settings = Settings(mcp_hostname="   ", secret_key=_SECRET, _env_file=None)

    assert settings.mcp_hostname is None
    assert settings.base_url == "http://localhost:8000"
    assert settings.public_base_url is None


def test_a_capitalised_hostname_still_matches_its_base_url():
    # `_validate_public_transport` compares BASE_URL's host with MCP_HOSTNAME;
    # an operator who capitalises both must still boot.
    settings = Settings(
        mcp_hostname="Vault.Example.com",
        base_url="https://vault.example.com",
        secret_key=_SECRET,
        _env_file=None,
    )

    assert settings.mcp_hostname == "vault.example.com"


def test_sandbox_mode_still_refuses_a_capitalised_public_hostname():
    with pytest.raises(ValidationError) as exc:
        Settings(
            mcp_sandbox_mode=True,
            mcp_hostname="Vault.Example.com",
            secret_key=_SECRET,
            _env_file=None,
        )
    assert "MCP_HOSTNAME" in str(exc.value)


# ── Federated login (`AUTH_MODE=pocketid`) ──────────────────────────────────
#
# Every case below is a **startup** refusal, and they all defend one thing: in
# `pocketid` mode the local password form and self-registration are withdrawn,
# so a configuration this validator lets through and the request path then
# cannot use is a deployment nobody can sign in to, with no way back except
# shell access. A locked door is not a degraded mode.

_OIDC = {
    "auth_mode": "pocketid",
    "multi_user_mode": True,
    "oidc_issuer": "https://auth.example.com",
    "oidc_client_id": "obsidian-mcp",
    "oidc_client_secret": "s3cr3t",
    "oidc_redirect_uri": "https://mcp.example.com/admin/auth/oidc/callback",
}


def _oidc_settings(**overrides):
    return Settings(**{**_OIDC, **overrides}, secret_key=_SECRET, _env_file=None)


def test_auth_mode_defaults_to_local_and_needs_no_oidc_settings():
    """The default is what keeps every pre-existing deployment, and every
    pre-existing test, on exactly the path it was on."""
    settings = Settings(secret_key=_SECRET, _env_file=None)
    assert settings.auth_mode == "local"
    assert settings.oidc_issuer is None


def test_an_unknown_auth_mode_is_refused():
    with pytest.raises(ValidationError):
        Settings(auth_mode="ldap", secret_key=_SECRET, _env_file=None)


def test_a_complete_pocketid_configuration_is_accepted():
    settings = _oidc_settings()
    assert settings.auth_mode == "pocketid"
    assert settings.oidc_issuer == "https://auth.example.com"


@pytest.mark.parametrize(
    "missing",
    ["oidc_issuer", "oidc_client_id", "oidc_client_secret", "oidc_redirect_uri"],
)
def test_pocketid_refuses_a_missing_required_field(missing):
    with pytest.raises(ValidationError) as exc:
        _oidc_settings(**{missing: None})
    assert missing.upper() in str(exc.value)


def test_pocketid_refuses_a_blank_required_field():
    """A blank is a missing value that reads as a set one in a `.env`."""
    with pytest.raises(ValidationError) as exc:
        _oidc_settings(oidc_client_secret="   ")
    assert "OIDC_CLIENT_SECRET" in str(exc.value)


def test_pocketid_requires_multi_user_mode():
    # The auth router that owns the login and callback routes is mounted only
    # in multi-user mode, so this combination is configured-and-unreachable —
    # a setting that reads as applied and changes nothing, which is worse than
    # a refusal because it looks like it worked.
    with pytest.raises(ValidationError) as exc:
        _oidc_settings(multi_user_mode=False)
    assert "MULTI_USER_MODE" in str(exc.value)


@pytest.mark.parametrize(
    "issuer",
    [
        "http://auth.example.com",
        # No loopback exemption, deliberately: unlike BASE_URL this names
        # somebody else's origin across a network the operator does not own,
        # and everything the flow trusts arrives over it.
        "http://localhost:8080",
        "https://user:pass@auth.example.com",
        "https://auth.example.com?x=1",
        "https://auth.example.com#frag",
        "not-a-url",
    ],
)
def test_pocketid_refuses_an_unusable_issuer(issuer):
    with pytest.raises(ValidationError) as exc:
        _oidc_settings(oidc_issuer=issuer)
    assert "OIDC_ISSUER" in str(exc.value)


def test_a_trailing_slash_on_the_issuer_is_normalised_away():
    # Discovery appends a path to it and the `iss` claim is compared to it as a
    # string, so the two spellings must not be two configurations.
    assert (
        _oidc_settings(oidc_issuer="https://auth.example.com/").oidc_issuer
        == "https://auth.example.com"
    )


@pytest.mark.parametrize(
    "redirect",
    [
        "http://mcp.example.com/admin/auth/oidc/callback",
        "/admin/auth/oidc/callback",
        "https://mcp.example.com/cb#frag",
    ],
)
def test_pocketid_refuses_an_unusable_redirect_uri(redirect):
    with pytest.raises(ValidationError) as exc:
        _oidc_settings(oidc_redirect_uri=redirect)
    assert "OIDC_REDIRECT_URI" in str(exc.value)


def test_a_loopback_redirect_uri_may_use_http():
    # Unlike the issuer: this one is a browser destination on the developer's
    # own machine, and every OAuth profile permits plaintext there.
    settings = _oidc_settings(
        oidc_redirect_uri="http://localhost:8000/admin/auth/oidc/callback"
    )
    assert settings.oidc_redirect_uri.startswith("http://localhost:8000")


def test_openid_is_added_back_to_the_scopes_rather_than_refused():
    # An operator who trimmed it made a mistake with exactly one correct
    # repair, and refusing to start over a repair the validator can perform is
    # not fail-fast, it is just a stop. Without `openid` the provider owes us
    # no ID token at all, and the ID token is the whole identity assertion.
    settings = _oidc_settings(oidc_scopes="profile email")
    assert settings.oidc_scopes.split()[0] == "openid"
    assert "profile" in settings.oidc_scopes


def test_a_blank_required_group_means_no_group_requirement():
    assert _oidc_settings(oidc_required_group="   ").oidc_required_group is None
    assert _oidc_settings(oidc_required_group="admins").oidc_required_group == "admins"


def test_local_mode_ignores_an_incomplete_oidc_configuration():
    """Half-configured OIDC settings must not stop a `local` deployment booting.

    An operator who tried federated login, backed it out by flipping
    `AUTH_MODE` and left the rest in `.env` has a working deployment, not a
    container that will not start.
    """
    settings = Settings(
        auth_mode="local",
        oidc_issuer="http://not-https",
        secret_key=_SECRET,
        _env_file=None,
    )
    assert settings.auth_mode == "local"
