#!/usr/bin/env python3
"""Create a working vault clone without replacing an existing vault."""
import os
import subprocess
import sys
from pathlib import Path


def main():
    root = os.environ.get("VAULT_PATH")
    remote = os.environ.get("VAULT_GIT_REMOTE")
    branch = os.environ.get("VAULT_GIT_BRANCH", "main")
    if not root or not remote:
        print("Set VAULT_PATH and VAULT_GIT_REMOTE; credentials belong in an SSH agent or credential helper.", file=sys.stderr)
        return 2
    destination = Path(root)
    if destination.exists():
        print("VAULT_PATH already exists. Nothing was changed; choose a new directory or reconcile the existing clone.", file=sys.stderr)
        return 2
    # Never print a remote: even a misconfigured URL can carry credentials.
    try:
        subprocess.run(
            ["git", "clone", "--branch", branch, "--", remote, str(destination)],
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=120, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        print("Clone failed. Check repository access and the selected branch. A partial directory may remain; inspect it before retrying.", file=sys.stderr)
        return 1
    print("Vault clone ready. Run the app and reconciliation as the user that owns this directory.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
