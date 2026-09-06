"""`AUTH_MODE=pocketid` without `MULTI_USER_MODE=true` must never ship.

This one is not hypothetical. A production deployment set `AUTH_MODE=pocketid`
and left `MULTI_USER_MODE` at its default, `src/config.py` refused to boot on
exactly that combination, and the container crash-looped until somebody read
the logs. The refusal worked; what was missing was anything that would have
caught the pairing before it reached a host.

`tests/test_config_validation.py::test_pocketid_requires_multi_user_mode`
already covers the validator, but it constructs `Settings` from keyword
arguments. The deployment sets these as **environment variables**, through
pydantic-settings' parsing, which is a different code path and the one that
actually failed — so the first half of this module re-asserts the constraint
the way a deployment expresses it.

The second half is the part that turns the fix into something durable: every
compose file and env template this repository ships is read, and any place that
*declares* an `AUTH_MODE` is pushed through the real validator rather than
through a re-implementation of its rule here. Today nothing declares one — the
compose files defer to `.env` — so those tests pass vacuously. That is the
point: they are a tripwire for the next person who adds `AUTH_MODE: pocketid`
to a shipped file, in this repository or by copying one of these files onto a
host, and the tripwire costs nothing while it is not needed.

The GitOps repository that actually holds the production stack cannot be
checked from here — it is a separate, private repository. Its compose file
carries the same rule as a comment next to the two variables. This module is
what the public half of that pairing looks like.
"""
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader that tolerates Compose's own YAML tags.

    Compose defines `!reset` and `!override` for merge control — an override
    file uses `ports: !reset null` to *remove* a port, because compose merges
    `ports` by concatenation and `ports: []` removes nothing. PyYAML has no
    constructor for them and raises, so a test that merely wanted to read the
    environment block died parsing the file instead, reporting a failure that
    had nothing to do with what it was asserting.
    """


def _ignore_unknown_tag(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_ComposeLoader.add_multi_constructor("!", _ignore_unknown_tag)


def _load_compose(path):
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_ComposeLoader) or {}


from src.config import Settings


REPO_ROOT = Path(__file__).resolve().parent.parent

COMPOSE_FILES = sorted(REPO_ROOT.glob("docker-compose*.yml"))

#: `Settings` refuses a placeholder SECRET_KEY, and every case below needs to
#: get past that guard to reach the one being tested. Only the four known
#: placeholders are refused, so this deliberately reads as prose rather than as
#: a hex string: gitleaks' generic-api-key rule flags a high-entropy literal
#: assigned to a name like this one, and `.gitleaksignore` pins fingerprints to
#: a commit — so suppressing it there would need re-pinning every time the file
#: is touched. Do not "improve" this into something that looks like a key.
SECRET = "not-a-real-secret-only-a-test-fixture"

#: A complete, otherwise-valid PocketID configuration. Only the two variables
#: under test vary between cases.
POCKETID_ENV = {
    "SECRET_KEY": SECRET,
    "AUTH_MODE": "pocketid",
    "OIDC_ISSUER": "https://auth.example.com",
    "OIDC_CLIENT_ID": "client-id",
    "OIDC_CLIENT_SECRET": "client-secret",
    "OIDC_REDIRECT_URI": "https://mcp.example.com/admin/auth/oidc/callback",
}


def _settings_from_env(monkeypatch, env):
    """Build `Settings` the way a container does: from the environment only."""
    for key in (
        "SECRET_KEY", "AUTH_MODE", "MULTI_USER_MODE", "OIDC_ISSUER",
        "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "OIDC_REDIRECT_URI",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


# ---------------------------------------------------------------------------
# The constraint, as a deployment expresses it
# ---------------------------------------------------------------------------


def test_pocketid_without_multi_user_mode_is_refused_from_the_environment(monkeypatch):
    """The exact crash-loop: AUTH_MODE set, MULTI_USER_MODE simply absent.

    Absent, not `false` — that is what the deployment did, and it is the
    reading that is easiest to get wrong, because the variable being missing
    looks like "not configured" rather than like "configured to the value that
    makes the other setting inert".
    """
    with pytest.raises(ValidationError) as exc:
        _settings_from_env(monkeypatch, POCKETID_ENV)
    assert "MULTI_USER_MODE" in str(exc.value)


@pytest.mark.parametrize("value", ["false", "False", "0", "no"])
def test_pocketid_with_multi_user_mode_explicitly_off_is_refused(monkeypatch, value):
    """Every spelling of "off" a `.env` or a compose file might carry."""
    with pytest.raises(ValidationError) as exc:
        _settings_from_env(monkeypatch, {**POCKETID_ENV, "MULTI_USER_MODE": value})
    assert "MULTI_USER_MODE" in str(exc.value)


@pytest.mark.parametrize("value", ["true", "True", "1"])
def test_the_deployed_pairing_boots(monkeypatch, value):
    """The other half of the assertion: the combination that IS correct must
    keep working, or a future tightening of the rule would be undetectable."""
    settings = _settings_from_env(monkeypatch, {**POCKETID_ENV, "MULTI_USER_MODE": value})
    assert settings.auth_mode == "pocketid"
    assert settings.multi_user_mode is True


# ---------------------------------------------------------------------------
# The same constraint, asserted about the files this repository ships
# ---------------------------------------------------------------------------


def _compose_environments(path):
    """(service, {VAR: value}) for every service in a compose file.

    Compose accepts `environment:` as a mapping or as a list of `KEY=value`
    strings; both spellings appear in the wild and both are normalised here.
    Values are left as written, including any `${...}` interpolation — an
    interpolated AUTH_MODE is reported rather than guessed at.
    """
    document = _load_compose(path)
    for service, definition in (document.get("services") or {}).items():
        raw = (definition or {}).get("environment")
        if raw is None:
            continue
        if isinstance(raw, dict):
            env = {str(k): "" if v is None else str(v) for k, v in raw.items()}
        else:
            env = {}
            for entry in raw:
                key, _, value = str(entry).partition("=")
                env[key] = value
        yield service, env


def test_there_are_compose_files_to_check():
    """A glob that silently matches nothing is not a gate. If the compose files
    are renamed, this fails and points at the rename."""
    assert COMPOSE_FILES, "no docker-compose*.yml found at the repository root"


@pytest.mark.parametrize(
    "compose_file", COMPOSE_FILES, ids=lambda p: p.name
)
def test_shipped_compose_files_never_configure_pocketid_alone(compose_file, monkeypatch):
    """Any service that declares AUTH_MODE must declare a set of variables the
    real validator accepts.

    The check runs the declared pair through `Settings` rather than restating
    the rule, so it cannot drift from `src/config.py`. Only the two variables
    under test are taken from the file; the rest of a valid PocketID
    configuration is supplied, because a compose file that legitimately leaves
    `OIDC_CLIENT_SECRET` to `.env` is not the bug being policed here.
    """
    for service, env in _compose_environments(compose_file):
        if "AUTH_MODE" not in env:
            continue
        auth_mode = env["AUTH_MODE"]
        if auth_mode.startswith("${"):
            pytest.fail(
                f"{compose_file.name}: service {service!r} interpolates AUTH_MODE "
                f"({auth_mode}). Whether it is safe then depends on a value this "
                "file does not contain, which is exactly the ambiguity that "
                "produced the crash-loop. Set it literally, or leave it to .env."
            )
        candidate = {
            **POCKETID_ENV,
            "AUTH_MODE": auth_mode,
            **({"MULTI_USER_MODE": env["MULTI_USER_MODE"]} if "MULTI_USER_MODE" in env else {}),
        }
        try:
            _settings_from_env(monkeypatch, candidate)
        except ValidationError as exc:
            pytest.fail(
                f"{compose_file.name}: service {service!r} ships an AUTH_MODE / "
                f"MULTI_USER_MODE pair the application refuses to boot on. "
                f"A container started from this file would crash-loop.\n{exc}"
            )


def test_env_example_never_configures_pocketid_alone(monkeypatch):
    """`.env.example` is copied to `.env` by every deployment path in the docs,
    so an *active* (uncommented) line here is shipped configuration."""
    active = {}
    for line in (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        active[key.strip()] = value.strip()

    if active.get("AUTH_MODE") is None:
        return

    candidate = {
        **POCKETID_ENV,
        "AUTH_MODE": active["AUTH_MODE"],
        **({"MULTI_USER_MODE": active["MULTI_USER_MODE"]} if "MULTI_USER_MODE" in active else {}),
    }
    try:
        _settings_from_env(monkeypatch, candidate)
    except ValidationError as exc:
        pytest.fail(
            ".env.example activates an AUTH_MODE / MULTI_USER_MODE pair the "
            f"application refuses to boot on:\n{exc}"
        )
