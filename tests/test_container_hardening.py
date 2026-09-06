"""The container contract: the Dockerfile and the compose stacks.

None of this is exercised by the rest of the suite, and all of it fails
*quietly*. A container that has lost its memory limit serves traffic exactly
like one that has it, right up until the host OOMs. A `USER` line dropped
during a refactor produces an image that works better, not worse, until
something writes to the bind-mounted vault as root. A compose override that
appends to `ports` instead of resetting it leaves the app on a published port
next to the proxy that was supposed to be its only door.

So the properties below are asserted against the files themselves. They are
cheap, they are hermetic (no docker, no network — this runs inside
`scripts/test-in-docker.sh` with `--network none`), and they are the reason a
future edit that undoes one of them is a red test rather than an incident.

What is deliberately NOT here: `docker compose config`, which needs the docker
CLI and a populated `.env`. That runs in CI's `compose-config` job, which is
the right place for a check that needs a binary this container does not have.
"""
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent

DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"

BASE_COMPOSE = ROOT / "docker-compose.yml"
OVERRIDE_COMPOSE = sorted(
    p for p in ROOT.glob("docker-compose.*.yml") if p != BASE_COMPOSE
)
ALL_COMPOSE = [BASE_COMPOSE, *OVERRIDE_COMPOSE]

#: The app's service name, identical in every file.
APP = "obsidian-mcp"


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader that tolerates compose's merge tags.

    `!reset` (Compose >= 2.24) is how an override *deletes* an inherited key.
    `safe_load` raises on the unknown tag, so it is resolved to a sentinel that
    the tests below can recognise — the distinction between "reset" and "empty
    list" is exactly what one of them checks.
    """


class _Reset:
    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<!reset>"


RESET = _Reset()

_ComposeLoader.add_constructor("!reset", lambda loader, node: RESET)
_ComposeLoader.add_constructor("!override", lambda loader, node: RESET)


def load(path: Path) -> dict:
    return yaml.load(path.read_text(), Loader=_ComposeLoader)  # noqa: S506


def services(path: Path) -> dict:
    return load(path).get("services") or {}


def is_full_definition(spec: dict) -> bool:
    """True for a service this file actually defines, not merely patches.

    An override that only adds labels or resets `ports` inherits the base
    file's resource limits; requiring it to restate them would be requiring
    exactly the duplication the override layout exists to remove.
    """
    return bool(spec.get("image") or spec.get("build"))


def published_ports(spec: dict) -> list[str]:
    ports = spec.get("ports")
    if ports is RESET or not ports:
        return []
    return [str(p) for p in ports]


# --------------------------------------------------------------------------
# Compose: resource limits
# --------------------------------------------------------------------------

#: Every knob that has to be present, and why it is not optional.
#:
#: mem_limit/mem_reservation bound and floor the working set; memswap_limit is
#: the one that actually forbids swap (without it Docker grants swap equal to
#: the memory limit, which turns an OOM kill into an unbounded latency cliff);
#: cpus stops one service starving the others on a shared VPS; pids_limit is
#: the only bound on a fork bomb, and this codebase does fork — the git history
#: tools are a subprocess surface.
REQUIRED_LIMITS = (
    "mem_limit",
    "mem_reservation",
    "memswap_limit",
    "cpus",
    "pids_limit",
)


@pytest.mark.parametrize("path", ALL_COMPOSE, ids=lambda p: p.name)
def test_every_defined_service_declares_every_resource_limit(path: Path) -> None:
    missing: list[str] = []
    for name, spec in services(path).items():
        if not is_full_definition(spec):
            continue
        for key in REQUIRED_LIMITS:
            if key not in spec:
                missing.append(f"{path.name}:{name} is missing {key}")
    assert not missing, "\n".join(missing)


@pytest.mark.parametrize("path", ALL_COMPOSE, ids=lambda p: p.name)
def test_memswap_equals_mem_limit_so_no_service_can_swap(path: Path) -> None:
    """Swap is not headroom, it is a slower failure than the one it hides.

    Docker's default when `mem_limit` is set and `memswap_limit` is not is to
    allow swap up to twice the limit. For a database that is the difference
    between a crash you notice and a host that stops responding.
    """
    for name, spec in services(path).items():
        if not is_full_definition(spec):
            continue
        assert spec["memswap_limit"] == spec["mem_limit"], (
            f"{path.name}:{name} allows swap "
            f"(mem_limit={spec['mem_limit']}, memswap_limit={spec['memswap_limit']})"
        )


@pytest.mark.parametrize("path", ALL_COMPOSE, ids=lambda p: p.name)
def test_no_service_mixes_the_deploy_and_short_resource_forms(path: Path) -> None:
    """Compose refuses a project that sets both to different values.

    It is not a warning — `docker compose config` fails with "can't set
    distinct values on 'pids_limit' and 'deploy.resources.limits.pids'", and an
    unset key counts as 0. Keeping every stack on the short form means that
    error can never be what an operator meets first.
    """
    for name, spec in services(path).items():
        assert "deploy" not in spec, (
            f"{path.name}:{name} uses `deploy:`; this stack uses the short-form "
            "resource keys and the two cannot be mixed"
        )


@pytest.mark.parametrize("path", ALL_COMPOSE, ids=lambda p: p.name)
def test_every_defined_service_restarts_and_hardens(path: Path) -> None:
    for name, spec in services(path).items():
        if not is_full_definition(spec):
            continue
        assert spec.get("restart") == "unless-stopped", (
            f"{path.name}:{name} must restart unless-stopped — `always` fights "
            "an operator who stopped it on purpose, and no policy at all means "
            "a host reboot silently takes the vault offline"
        )
        assert "no-new-privileges:true" in (spec.get("security_opt") or []), (
            f"{path.name}:{name} is missing no-new-privileges"
        )


# --------------------------------------------------------------------------
# Compose: what may be reachable
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", ALL_COMPOSE, ids=lambda p: p.name)
def test_postgres_publishes_no_ports(path: Path) -> None:
    """The database holds every note's text and every credential hash.

    Its only client is the app container on the internal network. Publishing it
    puts all of that behind one password on the host's public interface, and
    there is no deployment shape in this repo that needs it — `docker compose
    exec postgres psql` is the supported way in.
    """
    spec = services(path).get("postgres")
    if spec is None:
        return
    assert published_ports(spec) == [], (
        f"{path.name}: postgres publishes {published_ports(spec)}"
    )


@pytest.mark.parametrize("path", ALL_COMPOSE, ids=lambda p: p.name)
def test_the_app_publishes_only_on_loopback(path: Path) -> None:
    """/admin is protected by the *proxy* in single-user mode, not by the app.

    A mapping without a host IP binds 0.0.0.0, which reaches the panel from the
    internet with the proxy's auth chain skipped entirely.
    """
    spec = services(path).get(APP)
    if spec is None:
        return
    for mapping in published_ports(spec):
        assert mapping.startswith("127.0.0.1:"), (
            f"{path.name}: {APP} publishes {mapping!r}, which binds every "
            "interface; bind it to 127.0.0.1 or let the proxy reach it over "
            "the compose network"
        )


@pytest.mark.parametrize("path", OVERRIDE_COMPOSE, ids=lambda p: p.name)
def test_proxy_overrides_reset_the_published_port_rather_than_emptying_it(
    path: Path,
) -> None:
    """`ports: []` does not remove anything. This is the whole trap.

    Compose merges `ports` by CONCATENATION, so an empty list in an override
    contributes nothing and the base file's `127.0.0.1:8000` survives — the app
    stays published beside the proxy that was meant to be its only door. Only
    the `!reset` tag deletes an inherited key.
    """
    spec = services(path).get(APP)
    if spec is None or "ports" not in spec:
        return
    assert spec["ports"] is RESET, (
        f"{path.name}: {APP}.ports is {spec['ports']!r}; a proxy override must "
        "use `ports: !reset null`, because an empty list leaves the base "
        "mapping in place"
    )


# --------------------------------------------------------------------------
# Compose: the --workers 1 contract
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", ALL_COMPOSE, ids=lambda p: p.name)
def test_no_stack_overrides_the_apps_command(path: Path) -> None:
    """The uvicorn invocation exists once, in the image's CMD.

    `--workers 1` is load-bearing: every /mcp rate control is in-process state
    (docs/architecture/rate-limits.md), so a second worker multiplies every
    configured rate rather than splitting it — and fails open, silently. The
    three stacks this layout replaced each restated the command to prepend
    `alembic upgrade head`, and each of them dropped the flag doing it.
    Migrations belong in docker/entrypoint.sh (RUN_MIGRATIONS) for exactly this
    reason.
    """
    spec = services(path).get(APP)
    if spec is None:
        return
    assert "command" not in spec, (
        f"{path.name}: {APP} overrides `command:`. Whatever it needs to do "
        "belongs in docker/entrypoint.sh; an override here silently discards "
        "the image's CMD and with it `--workers 1`."
    )
    assert "entrypoint" not in spec, (
        f"{path.name}: {APP} overrides `entrypoint:`, which bypasses tini and "
        "docker/entrypoint.sh"
    )


@pytest.mark.parametrize("path", ALL_COMPOSE, ids=lambda p: p.name)
def test_the_app_healthcheck_addresses_localhost_not_an_ip(path: Path) -> None:
    """A literal-IP Host header is a 400, not a 200.

    `allowed_hosts` is `[MCP_HOSTNAME, "localhost"]` (src/config.py), and
    Starlette's TrustedHostMiddleware rejects anything else. A healthcheck on
    `http://127.0.0.1:8000/health` therefore marks a perfectly healthy
    container unhealthy forever, which `depends_on: service_healthy` turns into
    a stack that never comes up.
    """
    spec = services(path).get(APP)
    if spec is None or "healthcheck" not in spec:
        return
    test = " ".join(str(x) for x in spec["healthcheck"]["test"])
    assert "127.0.0.1:8000" not in test and "0.0.0.0:8000" not in test, test
    assert "localhost:8000" in test, test


# --------------------------------------------------------------------------
# Compose: the backups invariant (#186)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", ALL_COMPOSE, ids=lambda p: p.name)
def test_no_stack_mounts_the_backups_directory_into_the_app(path: Path) -> None:
    """The server must not be able to see the dumps.

    The panel reports backup age from the `backups_log` table precisely so this
    mount is unnecessary; the dumps hold every tenant's note text and every
    credential hash. It crept back in once (#186) and `make
    check-no-backups-mount` refuses a deploy that reintroduces it — this catches
    it a step earlier, in review.
    """
    spec = services(path).get(APP)
    if spec is None:
        return
    for volume in spec.get("volumes") or []:
        target = str(volume).split(":")[1] if ":" in str(volume) else ""
        assert target != "/app/backups", f"{path.name}: {volume}"


# --------------------------------------------------------------------------
# Dockerfile
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return DOCKERFILE.read_text()


def test_the_build_is_multi_stage(dockerfile: str) -> None:
    """The runtime stage takes /opt/venv from the builder and nothing else.

    This is an attack-surface property first and a size property a distant
    second — measured, the split is worth 7.4 MB unpacked and ~0.4 MB as a
    registry pull, because the single-stage build was already using
    `--no-cache-dir` and a 3.12 venv ships no setuptools. What it removes is
    the install tooling and metadata from the thing that runs.
    """
    froms = re.findall(r"^FROM\s+(\S+)(?:\s+AS\s+(\S+))?", dockerfile, re.M | re.I)
    assert len(froms) >= 2, froms
    names = {alias for _, alias in froms if alias}
    assert {"builder", "runtime"} <= names, names
    assert "COPY --from=builder /opt/venv /opt/venv" in dockerfile


def test_the_image_does_not_run_as_root(dockerfile: str) -> None:
    """The last USER wins, and it must not be root.

    The vault is a bind mount the write tools mutate, so a root process writes
    host files as root. Reverting this produces an image that appears to work
    better — every permission problem disappears — which is why it needs a test
    rather than a comment.
    """
    users = re.findall(r"^USER\s+(\S+)", dockerfile, re.M)
    assert users, "no USER instruction: the image would run as root"
    last = users[-1]
    assert last not in ("root", "0", "0:0"), last
    assert last.split(":")[0] == "1000", (
        f"final USER is {last!r}; the compose files and the ownership "
        "documentation assume uid 1000"
    )


def test_dependencies_are_installed_before_the_source_is_copied(
    dockerfile: str,
) -> None:
    """Otherwise every one-line source change reinstalls ~40 packages."""
    reqs = dockerfile.index("COPY requirements.txt")
    install = dockerfile.index("pip install --no-cache-dir -r requirements.txt")
    src = dockerfile.index("COPY src/ src/")
    assert reqs < install < src


def test_the_image_declares_its_own_healthcheck(dockerfile: str) -> None:
    # Join line continuations so the instruction is one string.
    joined = dockerfile.replace("\\\n", " ")
    match = re.search(r"^HEALTHCHECK\s+(.+)$", joined, re.M)
    assert match is not None, "no HEALTHCHECK instruction"
    instruction = match.group(1)
    assert "/health" in instruction, instruction
    # See test_the_app_healthcheck_addresses_localhost_not_an_ip.
    assert "localhost:8000" in instruction, instruction


def test_pid_one_is_an_init_that_reaps(dockerfile: str) -> None:
    """git subprocesses orphaned mid-kill reparent to PID 1.

    A Python PID 1 does not reap them, so they accumulate as zombies against
    the container's own pids_limit. tini also forwards SIGTERM, which is what
    makes `docker stop` a graceful shutdown instead of a 10s wait and a KILL.
    """
    entrypoint = re.search(r"^ENTRYPOINT\s+(.+)$", dockerfile, re.M)
    assert entrypoint is not None, "no ENTRYPOINT: uvicorn would be PID 1"
    assert "tini" in entrypoint.group(1), entrypoint.group(1)
    assert "entrypoint.sh" in entrypoint.group(1), entrypoint.group(1)


def test_the_cmd_still_pins_one_worker(dockerfile: str) -> None:
    """See docs/architecture/rate-limits.md. This is the single source."""
    cmd = re.search(r"^CMD\s+(.+)$", dockerfile, re.M)
    assert cmd is not None
    assert '"--workers", "1"' in cmd.group(1), cmd.group(1)
    assert '"--forwarded-allow-ips"' in cmd.group(1), cmd.group(1)
    assert '"*"' not in cmd.group(1), (
        "--forwarded-allow-ips=* lets any client spoof its forwarded IP"
    )


# git's presence in the image is asserted by
# tests/test_runtime_dependencies.py, which predates this module and covers it
# against any restructuring — including this one. Not repeated here.


# --------------------------------------------------------------------------
# .dockerignore and the entrypoint
# --------------------------------------------------------------------------


def test_the_build_context_is_an_allow_list() -> None:
    """Deny-first, then name what is read. A deny-list is what rotted before.

    Measured on this tree: 17.33 MB / 776 files -> 3.21 MB / 120 files.
    """
    lines = [
        line.strip()
        for line in DOCKERIGNORE.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines[0] == "*", (
        "the first rule must be `*`; anything else makes this a deny-list again"
    )
    allowed = {line[1:].rstrip("/") for line in lines if line.startswith("!")}
    # Everything both Dockerfiles read.
    assert {
        "requirements.txt",
        "requirements-dev.txt",
        "alembic.ini",
        "alembic",
        "src",
        "scripts",
        "docker/entrypoint.sh",
    } <= allowed, allowed
    # The directories that were the bulk of the old context.
    for never in ("tests", "screenshots", "openspec", "docs", ".git", "README.md"):
        assert never not in allowed, never


def test_the_entrypoint_execs_so_uvicorn_receives_the_signal() -> None:
    """Without `exec` the shell keeps the pid and swallows SIGTERM."""
    body = ENTRYPOINT.read_text()
    assert body.rstrip().endswith('exec "$@"'), body.rstrip()[-80:]


def test_migrations_are_opt_in_and_default_off() -> None:
    """`make deploy` backs up BEFORE it migrates.

    A container that always migrated on start would run the upgrade ahead of
    that backup and leave nothing to roll back to, so the default has to be
    off and the compose stacks — single-replica, schema-owning — opt in.
    """
    body = ENTRYPOINT.read_text()
    assert '"${RUN_MIGRATIONS:-false}" = "true"' in body, body
    assert "alembic upgrade head" in body
    app = services(BASE_COMPOSE)[APP]
    assert app["environment"]["RUN_MIGRATIONS"] == "true"
