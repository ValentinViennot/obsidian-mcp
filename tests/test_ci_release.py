"""CI partition coverage and the fail-closed release boundary."""
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from scripts import ci_release, ci_tests

ROOT = Path(__file__).resolve().parents[1]


def test_partitions_cover_every_test_file_once_and_keep_fts_in_postgres():
    partitions = ci_tests.files()
    all_files = [p for group in partitions.values() for p in group]
    assert len(all_files) == len(set(all_files))
    assert set(all_files) == {p.relative_to(ROOT).as_posix() for p in (ROOT / 'tests').rglob('test_*.py')}
    assert ci_tests.FTS in partitions['postgres']
    assert partitions['schema'] == [ci_tests.SCHEMA]
    assert not any(p.startswith('tests/integration/') for p in partitions['offline'])


@pytest.mark.parametrize('paths,expected', [
    (['README.md', 'docs/operations/release.md'], True),
    (['openspec/specs/search/spec.md'], True),
    ([], False), (['src/main.py'], False),
    (['.github/workflows/ci.yml'], False), (['tests/test_new.py'], False),
    (['docs/a.md', 'requirements.txt'], False), (['unknown'], False),
])
def test_docs_only_is_narrow_and_unknown_diffs_run_gates(paths, expected):
    assert ci_release.docs_only(paths) is expected


def test_manifest_requires_all_actual_gates_and_binds_source():
    evidence = dict.fromkeys(ci_release.GATES, 'success')
    manifest = ci_release.manifest('a' * 40, 'sha256:' + 'b' * 64, '123', '1', evidence, '044')
    assert manifest['source'] == {'repository': ci_release.REPOSITORY,
                                  'ref': 'refs/heads/main', 'commit': 'a' * 40}
    assert manifest['schema']['head'] == '044'
    for state in ('failure', 'skipped', 'cancelled', ''):
        with pytest.raises(ValueError, match='gate'):
            ci_release.manifest('a' * 40, 'sha256:' + 'b' * 64, '123', '1',
                                {**evidence, 'postgres': state}, '044')
    with pytest.raises(ValueError, match='gate'):
        ci_release.manifest('a' * 40, 'sha256:' + 'b' * 64, '123', '1', {}, '044')


def test_schema_head_reads_graph_and_refuses_parallel_heads(tmp_path):
    folder = tmp_path / 'alembic/versions'
    folder.mkdir(parents=True)
    (folder / 'a.py').write_text("revision = '001'\ndown_revision = None\n")
    (folder / 'b.py').write_text("revision: str = '002'\ndown_revision = '001'\n")
    assert ci_release.schema_head(tmp_path) == '002'
    (folder / 'c.py').write_text("revision = '003'\ndown_revision = '001'\n")
    with pytest.raises(ValueError, match='graph'):
        ci_release.schema_head(tmp_path)


def test_missing_or_skipped_pg_tests_cannot_be_evidence(tmp_path):
    report = tmp_path / 'report.xml'
    report.write_text('<testsuite><testcase classname="tests.test_fts_integration" name="fts"><skipped/></testcase></testsuite>')
    with pytest.raises(ValueError, match='skipped'):
        ci_tests.validate_report(report, 'postgres', [ci_tests.FTS])
    report.write_text('<testsuite><testcase classname="tests.other" name="a"/></testsuite>')
    with pytest.raises(ValueError, match='not collected'):
        ci_tests.validate_report(report, 'postgres', [ci_tests.FTS])
    report.write_text('<testsuite><testcase classname="tests.test_fts_integration" name="fts"/></testsuite>')
    ci_tests.validate_report(report, 'postgres', [ci_tests.FTS])


def test_workflow_has_one_build_no_ssh_and_checks_before_publish():
    # This is the execution contract, not a snapshot of action formatting.
    import yaml
    workflow = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())
    jobs = workflow['jobs']
    builds = [(name, step) for name, job in jobs.items() for step in job.get('steps', [])
              if step.get('uses', '').startswith('docker/build-push-action@')]
    assert [name for name, _ in builds] == ['image']
    assert builds[0][1]['with']['load'] is True
    assert 'pr-gate' in jobs['publish']['needs']
    assert set(jobs['pr-gate']['needs']) == {'changes', 'offline', 'database', 'security', 'openspec', 'compose', 'image'}
    assert jobs['database']['strategy']['matrix']['partition'] == ['schema', 'postgres']
    publish_text = json.dumps(jobs['publish'])
    assert 'docker load' in publish_text and 'docker build' not in publish_text
    assert 'GITOPS_PIPELINE_TRIGGER_TOKEN' in publish_text
    assert 'OBSIDIAN_RELEASE_MANIFEST' in publish_text
    assert 'ssh ' not in publish_text
    assert list((ROOT / '.github/workflows').glob('*.yml')) == [ROOT / '.github/workflows/ci.yml']


def test_offline_skips_require_exact_case_and_intentional_reason(tmp_path):
    report = tmp_path / 'report.xml'
    def check(module, name, reason):
        root = ET.Element('testsuite')
        ET.SubElement(root, 'testcase', classname='tests.test_nested_mount_publication', name=ci_tests.MOUNT_CASE)
        case = ET.SubElement(root, 'testcase', classname='tests.' + module, name=name)
        ET.SubElement(case, 'skipped', message=reason)
        ET.ElementTree(root).write(report)
        ci_tests.validate_report(report, 'offline', [])
    check('test_issue_358_scoped_tools_list', 'test_oracle_capture', 'regeneration only')
    check('test_vault_reconcile_engine', 'test_root_sweep_preserves_different_service_uid_and_additions',
          'requires isolated root container for actual different-uid ownership')
    for module, name, reason in (
        ('test_git_vault', 'test_commit', 'git is not available'),
        ('test_issue_332_create_dialog_reset', 'test_reopening_after_cancel_shows_the_first_open_state', 'node is not on PATH'),
        ('test_issue_358_scoped_tools_list', 'test_oracle_capture', 'unexpected failure'),
        ('test_new', 'test_guard', 'regeneration only'),
    ):
        with pytest.raises(ValueError, match='required offline case skipped'):
            check(module, name, reason)
