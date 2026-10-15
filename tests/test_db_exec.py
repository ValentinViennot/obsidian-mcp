"""`docker/db-exec.sh` — the one channel db-backup, db-restore and
record-backup.sh use to reach PostgreSQL.

`docker` and `kubectl` are replaced by shims on PATH that record their argv and
play back a canned answer, so these tests run anywhere (no daemon, no cluster)
and pin the exact command each backend runs.
"""

import os
import stat
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "docker", "db-exec.sh")


def _shim(bindir, name, body):
    path = bindir / name
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _run(tmp_path, args, *, env=None, pods="pg-1\n", get_rc=0, stdin=None):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "argv.log"
    record = f'printf "%s\\n" "$0 $*" >> "{log}"\n'
    _shim(bindir, "docker", record + 'echo docker-ran\n')
    podsfile = tmp_path / "pods.txt"
    podsfile.write_text(pods)
    _shim(
        bindir,
        "kubectl",
        record
        + 'for a in "$@"; do\n'
        + f'  if [ "$a" = get ]; then cat "{podsfile}"; exit {get_rc}; fi\n'
        + '  if [ "$a" = exec ]; then echo kubectl-exec-ran; exit 0; fi\n'
        + "done\nexit 97\n",
    )
    full_env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", **(env or {})}
    for var in ("DB_BACKEND", "DB_CONTAINER", "CNPG_NAMESPACE", "CNPG_CLUSTER", "KUBECTL"):
        if env is None or var not in env:
            full_env.pop(var, None)
    result = subprocess.run(
        ["bash", SCRIPT, *args],
        env=full_env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return result, [c.split(" ", 1)[1] if " " in c else "" for c in calls]


def test_docker_is_the_default_backend(tmp_path):
    result, calls = _run(tmp_path, ["pg_dump", "-U", "postgres", "obsidian_mcp"], env={"DB_CONTAINER": "postgres"})
    assert result.returncode == 0, result.stderr
    assert calls == ["exec postgres pg_dump -U postgres obsidian_mcp"]


def test_docker_keeps_stdin_with_dash_i(tmp_path):
    result, calls = _run(
        tmp_path, ["-i", "psql", "-U", "postgres", "obsidian_mcp"],
        env={"DB_BACKEND": "docker", "DB_CONTAINER": "postgres"}, stdin="SELECT 1;\n",
    )
    assert result.returncode == 0, result.stderr
    assert calls == ["exec -i postgres psql -U postgres obsidian_mcp"]


def test_docker_without_a_container_is_refused(tmp_path):
    result, calls = _run(tmp_path, ["pg_dump"], env={"DB_BACKEND": "docker"})
    assert result.returncode == 2
    assert calls == []


def test_cnpg_execs_into_the_one_running_primary(tmp_path):
    result, calls = _run(
        tmp_path, ["-i", "psql", "-U", "postgres", "-d", "obsidian_mcp"],
        env={"DB_BACKEND": "cnpg"}, pods="pg-2\n",
    )
    assert result.returncode == 0, result.stderr
    assert "kubectl-exec-ran" in result.stdout
    get, run = calls
    assert get.startswith("-n db get pods -l cnpg.io/cluster=pg,cnpg.io/instanceRole=primary ")
    assert "--field-selector=status.phase=Running" in get
    # The primary is whatever the label says — pg-2 after a failover, not pg-1.
    assert run == "-n db exec -i pg-2 -c postgres -- psql -U postgres -d obsidian_mcp"


def test_cnpg_namespace_and_cluster_are_configurable(tmp_path):
    result, calls = _run(
        tmp_path, ["pg_dump", "x"],
        env={"DB_BACKEND": "cnpg", "CNPG_NAMESPACE": "data", "CNPG_CLUSTER": "main"}, pods="main-1\n",
    )
    assert result.returncode == 0, result.stderr
    assert "cnpg.io/cluster=main," in calls[0] and calls[0].startswith("-n data ")
    assert calls[1] == "-n data exec main-1 -c postgres -- pg_dump x"


def test_cnpg_refuses_no_primary(tmp_path):
    result, calls = _run(tmp_path, ["pg_dump", "x"], env={"DB_BACKEND": "cnpg"}, pods="")
    assert result.returncode == 1
    assert "found 0" in result.stderr
    assert len(calls) == 1  # listed, never exec'd


def test_cnpg_refuses_two_primaries(tmp_path):
    result, calls = _run(tmp_path, ["pg_dump", "x"], env={"DB_BACKEND": "cnpg"}, pods="pg-1\npg-2\n")
    assert result.returncode == 1
    assert "found 2" in result.stderr
    assert len(calls) == 1


def test_cnpg_refuses_when_the_listing_fails(tmp_path):
    result, calls = _run(tmp_path, ["pg_dump", "x"], env={"DB_BACKEND": "cnpg"}, get_rc=1)
    assert result.returncode == 1
    assert len(calls) == 1


def test_unknown_backend_is_refused(tmp_path):
    result, calls = _run(tmp_path, ["pg_dump"], env={"DB_BACKEND": "k8s", "DB_CONTAINER": "postgres"})
    assert result.returncode == 2
    assert calls == []
