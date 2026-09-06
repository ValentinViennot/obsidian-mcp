"""The `GIT_*` settings: defaults, bounds, and the identity validator."""
import pytest
from pydantic import ValidationError

from src.config import Settings


BASE = {"secret_key": "not-a-placeholder"}


def build(**overrides):
    return Settings(**BASE, **overrides)


def test_the_feature_is_off_by_default():
    """Turning this on must be a decision, never an upgrade side effect.

    A deployment whose vault is not a git repository is the default and is
    fully supported; a deployment whose vault *is* one (because the operator
    happened to `git init` it once) must not silently start committing.
    """
    settings = build()
    assert settings.git_vault_enabled is False
    # The write half defaults on, so enabling the feature is one variable.
    assert settings.git_commit_on_write is True


def test_the_default_agent_address_cannot_resolve():
    """`.invalid` is reserved (RFC 2606).

    A plausible-looking address on a real domain, in every commit of a
    repository that may later be pushed anywhere, is a small permanent lie.
    """
    assert build().git_agent_email.endswith(".invalid")


def test_the_commit_timeout_is_bounded_at_both_ends():
    with pytest.raises(ValidationError):
        build(git_commit_timeout_seconds=0)
    with pytest.raises(ValidationError):
        build(git_commit_timeout_seconds=-1)
    # A several-minute timeout is not a longer grace period, it is a thread
    # pool this feature can exhaust on its own.
    with pytest.raises(ValidationError):
        build(git_commit_timeout_seconds=121)
    assert build(git_commit_timeout_seconds=120).git_commit_timeout_seconds == 120


@pytest.mark.parametrize("field", ["git_agent_name", "git_agent_email"])
@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "Agent\nname",
        "Agent\rname",
        "Agent\x00name",
        "Agent <sneaky@example.invalid>",
        "Agent\x07name",
    ],
)
def test_an_unusable_git_identity_is_refused_at_startup(field, value):
    """git's commit-object grammar is line-oriented.

    A newline in an author field either makes git refuse the commit or — worse,
    depending on where it lands — produces an author line the operator never
    wrote. Refused at settings construction, so it is a container that will not
    start rather than a history that quietly lies.
    """
    with pytest.raises(ValidationError):
        build(**{field: value})


@pytest.mark.parametrize("field", ["git_agent_name", "git_agent_email"])
def test_a_usable_identity_is_stripped_and_kept(field):
    assert getattr(build(**{field: "  Agent Smith  "}), field) == "Agent Smith"
