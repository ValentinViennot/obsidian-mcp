#!/usr/bin/env python3
"""Small exact-source release contract shared by CI and its regression tests."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import re
import subprocess

REPOSITORY = 'ValentinViennot/obsidian-mcp'
IMAGE = 'ghcr.io/valentinviennot/obsidian-mcp'
GATES = ('offline', 'schema', 'postgres', 'image', 'security', 'openspec', 'compose')
ROOT = Path(__file__).resolve().parents[1]


def docs_only(paths: list[str]) -> bool:
    # Unknown paths and empty/unknown diffs run every gate. No top-level
    # paths-ignore: docs PRs still produce the required aggregate status.
    return bool(paths) and all(
        p.startswith(('docs/', 'openspec/')) or ('/' not in p and p.endswith('.md'))
        for p in paths
    )


def schema_head(root: Path = ROOT) -> str:
    revisions, parents = set(), set()
    for path in (root / 'alembic/versions').glob('*.py'):
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                name, value = node.target.id, node.value
            elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name, value = node.targets[0].id, node.value
            else:
                continue
            if name not in ('revision', 'down_revision'):
                continue
            val = ast.literal_eval(value)
            if name == 'revision':
                if val in revisions:
                    raise ValueError('duplicate migration revision')
                revisions.add(val)
            elif val is not None:
                parents.update(val if isinstance(val, (tuple, list)) else [val])
    heads = revisions - parents
    if len(heads) != 1 or parents - revisions:
        raise ValueError('release requires one complete migration graph')
    return heads.pop()


def manifest(commit: str, digest: str, run_id: str, run_attempt: str,
             evidence: dict[str, str], head: str) -> dict:
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('invalid source commit')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise ValueError('invalid image digest')
    if not run_id.isdecimal() or not run_attempt.isdecimal():
        raise ValueError('invalid workflow execution')
    if set(evidence) != set(GATES) or any(v != 'success' for v in evidence.values()):
        raise ValueError('every release gate must have succeeded')
    return {
        'schema_version': 1,
        'source': {'repository': REPOSITORY, 'ref': 'refs/heads/main', 'commit': commit},
        'image': {'repository': IMAGE, 'digest': digest},
        'workflow': {'run_id': run_id, 'run_attempt': run_attempt,
                     'url': f'https://github.com/{REPOSITORY}/actions/runs/{run_id}'},
        'evidence': evidence, 'schema': {'head': head},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    changes = sub.add_parser('changes')
    changes.add_argument('--base', required=True)
    changes.add_argument('--head', required=True)
    sub.add_parser('schema-head')
    release = sub.add_parser('manifest')
    for key in ('commit', 'digest', 'run-id', 'run-attempt', 'evidence', 'output'):
        release.add_argument('--' + key, required=True)
    args = parser.parse_args()
    if args.command == 'schema-head':
        print(schema_head())
    elif args.command == 'changes':
        try:
            paths = subprocess.check_output(['git', 'diff', '--name-only', args.base, args.head],
                                            cwd=ROOT, text=True, stderr=subprocess.DEVNULL).splitlines()
        except subprocess.CalledProcessError:
            paths = []
        print('false' if docs_only(paths) else 'true')
    else:
        data = manifest(args.commit, args.digest, args.run_id, args.run_attempt,
                        json.loads(args.evidence), schema_head())
        Path(args.output).write_text(json.dumps(data, sort_keys=True, indent=2) + '\n')


if __name__ == '__main__':
    main()
