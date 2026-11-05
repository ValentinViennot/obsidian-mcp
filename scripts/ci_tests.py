#!/usr/bin/env python3
"""Disjoint CI partitions and fail-closed test evidence; no service management."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = 'tests/integration/test_schema_check.py'
FTS = 'tests/test_fts_integration.py'
# These two explicitly opt into an external real vault or expensive RSS runs.
OPTIONAL_PG = {'tests.integration.test_vault_history_repo',
               'tests.integration.test_body_budget_stack_pg'}
MOUNT_CASE = 'test_nested_mount_cases_pass_in_a_mount_namespace'

# Exact cases and reasons only: missing Git/Node/Make/Docker, TLS fixtures,
# /proc or namespace support are required CI prerequisites, never exemptions.
OPTIONAL_OFFLINE = {
    ('test_issue_358_scoped_tools_list', 'test_oracle_capture'): ('regeneration only',),
    ('test_vault_reconcile_engine', 'test_root_sweep_preserves_different_service_uid_and_additions'):
        ('requires isolated root container for actual different-uid ownership',),
    ('test_internal_transport_config', 'test_an_unreadable_ca_file_is_refused'): ('root reads any file',),
    ('test_oidc_settings', 'test_ca_file_unreadable_refused'): ('root reads any file',),
    ('test_issue_309_prune_guards', 'test_an_unlistable_subdirectory_is_recorded_as_its_prefix'):
        ('root ignores directory modes',),
    ('test_issue_88_root_confirmed_before_publish', 'test_every_single_publication_tool_re_reads_exactly_once[move_note]'):
        ('move_note publishes more than once; covered below',),
    ('test_issue_87_beneath_root_lookup', 'test_glibc_exports_no_openat2_wrapper'):
        ('this glibc exports an openat2 wrapper; the raw syscall is still used',),
    ('test_issue_91_indexed_root', 'test_a_contradicting_handle_downgrades_a_keep_to_a_re_derive'):
        ('this filesystem cannot produce a file handle', 'the handle never changed across a rmdir/mkdir cycle'),
    ('test_issue_91_indexed_root', 'test_the_realpath_encoding_round_trips_a_non_utf8_pathname'):
        ('this filesystem refuses a non-UTF-8 directory name:',),
    ('test_issue_91_indexed_root', 'test_a_non_utf8_realpath_still_discards_and_the_next_pass_keeps'):
        ('this filesystem refuses a non-UTF-8 directory name:',),
    ('test_issue_91_indexed_root', 'test_a_directory_walk_failure_withholds_the_stamp'):
        ('permissions are not enforced for this user',),
    ('test_issue_91_indexed_root', 'test_a_directory_walk_failure_over_no_row_does_not_withhold'):
        ('permissions are not enforced for this user',),
    ('test_issue_308_nul_derivation', 'test_the_scan_classifies_unstorable_paths_and_content_as_not_indexable'):
        ('this filesystem refuses a non-UTF-8 filename:',),
}


def allowed_offline_skip(case: ET.Element) -> bool:
    key = (case.get('classname', '').removeprefix('tests.'), case.get('name', ''))
    message = case.find('skipped').get('message', '')
    return any(message == reason or (reason.endswith(':') and message.startswith(reason))
               for reason in OPTIONAL_OFFLINE.get(key, ()))


def partition(path: str) -> str:
    if path == SCHEMA:
        return 'schema'
    if path.startswith('tests/integration/') or path == FTS:
        return 'postgres'
    return 'offline'


def files(root: Path = ROOT) -> dict[str, list[str]]:
    result = {name: [] for name in ('offline', 'schema', 'postgres')}
    for path in sorted((root / 'tests').rglob('test_*.py')):
        name = path.relative_to(root).as_posix()
        result[partition(name)].append(name)
    if any(not group for group in result.values()):
        raise ValueError('an empty CI test partition cannot be evidence')
    return result


def validate_report(report: Path, group: str, selected: list[str]) -> None:
    cases = list(ET.parse(report).iter('testcase'))
    if not cases:
        raise ValueError('no collected test cases')
    modules = {c.get('classname', '') for c in cases}
    for path in selected:
        module = path[:-3].replace('/', '.')
        if not any(m == module or m.startswith(module + '.') for m in modules):
            raise ValueError(f'{path} was not collected')
    if group == 'offline':
        mount = [c for c in cases if c.get('name') == MOUNT_CASE]
        if not mount or any(c.find('skipped') is not None for c in mount):
            raise ValueError('the nested-mount namespace harness did not execute')
        for case in cases:
            if case.find('skipped') is not None and not allowed_offline_skip(case):
                raise ValueError(f"required offline case skipped: {case.get('classname')}::{case.get('name')}")
    else:
        for case in cases:
            if case.find('skipped') is not None and case.get('classname') not in OPTIONAL_PG:
                raise ValueError(f"required database case skipped: {case.get('classname')}::{case.get('name')}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('partition', choices=('offline', 'schema', 'postgres'))
    parser.add_argument('--list', action='store_true')
    args = parser.parse_args()
    selected = files()[args.partition]
    if args.list:
        print('\n'.join(selected))
        return 0
    env = dict(os.environ)
    if args.partition == 'offline':
        env.pop('PGVECTOR_TEST_ADMIN_URL', None)
        env.pop('TEST_DATABASE_URL', None)
    elif not env.get('PGVECTOR_TEST_ADMIN_URL'):
        parser.error('database partition requires PGVECTOR_TEST_ADMIN_URL')
    if args.partition == 'postgres' and not env.get('TEST_DATABASE_URL'):
        parser.error('postgres partition requires TEST_DATABASE_URL for FTS')
    env['OMCP_REQUIRE_SCHEMA_INTEGRATION'] = '1'
    env.pop('OMCP_ALLOW_SKIP_TRANSFER_INTEGRATION', None)
    report = ROOT / f'pytest-{args.partition}.xml'
    result = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-rs',
                             f'--junitxml={report}', *selected], cwd=ROOT, env=env)
    if result.returncode:
        return result.returncode
    validate_report(report, args.partition, selected)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
