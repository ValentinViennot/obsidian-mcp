"""External binaries this fork cannot work without.

These exist because of a real near-miss. The runtime image installed `curl` and
nothing else, which is fine for upstream — but this fork shells out to `git` on
every write-class tool call and for all three history tools. With no `git` on
PATH the code did exactly what it was designed to do: logged a warning, let the
write stand, and returned successfully.

That is the correct *failure* behaviour and the worst possible *silent* one. The
vault kept working, commits stopped happening, and nothing surfaced until
someone asked the history a question it could no longer answer.

So the dependency is asserted rather than assumed. A test that only passes on a
developer's laptop would be worthless here — the point is to fail in CI and in
the image build, which is where the mistake was actually made.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_git_is_on_path():
    """The environment running the suite must have git.

    In CI and in the release image this is the real assertion. On a developer
    machine it is nearly always true, which is precisely why it went unnoticed.
    """
    assert shutil.which("git") is not None, (
        "git is not on PATH. Commit-on-write and the history tools "
        "(note_history, note_blame, find_when_written) all shell out to it and "
        "will silently do nothing without it."
    )


def test_git_is_usable_not_merely_present():
    """Present-but-broken is a distinct failure from absent."""
    proc = subprocess.run(
        ["git", "--version"], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("git version"), proc.stdout


def test_the_runtime_image_installs_git():
    """The Dockerfile must install git, whatever else it is restructured into.

    Asserted against the file rather than a built image so it holds in a plain
    unit run, and so a rewrite of the Dockerfile (multi-stage, different base,
    different package manager) cannot quietly drop the package.
    """
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    # Tolerate any install form and any layout — what matters is that `git`
    # is installed as its own package somewhere in the final image.
    installs_git = re.search(
        r"^\s*(RUN|&&)?[^\n#]*\b(apt-get|apk|dnf|yum)\b[^\n]*\binstall\b[^\n]*",
        dockerfile,
        re.MULTILINE,
    )
    assert installs_git, "no package installation found in the Dockerfile"

    install_lines = [
        line
        for line in dockerfile.splitlines()
        if not line.strip().startswith("#")
    ]
    body = "\n".join(install_lines)
    assert re.search(r"(?<![\w./-])git(?![\w./-])", body), (
        "The Dockerfile no longer installs git. Commit-on-write and all three "
        "history tools shell out to it and degrade silently without it — see "
        "this module's docstring."
    )


@pytest.mark.parametrize("binary", ["git"])
def test_required_binaries_are_declared_once_in_one_place(binary):
    """A cheap guard against the dependency being satisfied only by accident.

    If someone removes it from the Dockerfile but the suite still passes because
    the CI runner happens to ship it, the test above catches the Dockerfile and
    this one catches the environment. Both have to hold.
    """
    assert shutil.which(binary) is not None
