"""Read-only migration probes and legacy deploy refusal on pending schemas."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("current,heads,fail,status", [
    ("043 (head)\n", "043 (head)\n", "", "current"),
    ("036\n", "043 (head)\n", "", "pending"),
    ("", "043 (head)\n", "", "pending"),
    ("036\n", "", "", None),
    ("036\n", "043 (head)\n044 (head)\n", "", None),
    ("036\n037\n", "043 (head)\n", "", None),
    ("unexpected output!", "043 (head)\n", "", None),
    ("043 (head)\n", "043 (head)\n", "current", None),
    ("043 (head)\n", "043 (head)\n", "heads", None),
])
def test_migration_probe_never_upgrades_and_fails_closed(tmp_path, current, heads, fail, status):
    cli = tmp_path / "alembic"
    cli.write_text(f"#!{sys.executable}\n" + '''import os,sys
from pathlib import Path
cmd=sys.argv[1]
with Path(os.environ['CALLS']).open('a') as f: f.write(cmd+'\\n')
assert cmd in ('current','heads'), 'probe attempted a mutation'
if os.environ.get('FAIL')==cmd: sys.exit(1)
print(os.environ[cmd.upper()],end='')
''')
    cli.chmod(0o755)
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}", CURRENT=current,
               HEADS=heads, FAIL=fail, CALLS=str(tmp_path / "calls"))
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/migration_status.py')],
                            env=env, capture_output=True, text=True)
    assert result.returncode == (0 if status else 1)
    assert result.stdout.strip() == (status or '')
    assert set((tmp_path / 'calls').read_text().splitlines()) <= {'current', 'heads'}


@pytest.mark.parametrize("probe,code", [("pending", "0"), ("", "1"), ("unknown", "0")])
def test_legacy_rollout_restores_pin_and_refuses_pending_or_failed_probe(tmp_path, probe, code):
    work = tmp_path / 'stack'
    work.mkdir()
    (work / 'compose.yaml').write_text('services: {}\n')
    docker = tmp_path / 'docker'
    docker.write_text(f"#!{sys.executable}\n" + '''import os,sys
from pathlib import Path
a=sys.argv[1:]
with Path(os.environ['CALLS']).open('a') as f: f.write(' '.join(a)+'\\n')
if a[:2]==['container','inspect']:
 text=' '.join(a)
 if 'working_dir' in text: print(os.environ['STACK'])
 elif 'config_files' in text: print('compose.yaml')
 elif 'compose.project' in text: print('synthetic')
 elif '{{.Image}}' in text: print('old-image-id')
elif a[:2]==['image','inspect']: print(os.environ['OLD_REF'])
elif a[0]=='compose':
 assert 'run' in a and 'migration_status.py' in a[-1], 'unexpected deployment mutation'
 assert 'RUN_MIGRATIONS=false' in a
 print(os.environ['PROBE'])
 sys.exit(int(os.environ['PROBE_CODE']))
elif a[0]!='pull': raise AssertionError(a)
''')
    docker.chmod(0o755)
    script = tmp_path / 'deploy.sh'
    script.write_text((ROOT / 'scripts/vps-deploy.sh').read_text().replace(
        'STATE_DIR="/var/lib/obsidian-mcp-deploy"', f'STATE_DIR="{tmp_path}/state"'))
    old = 'ghcr.io/synthetic/server@sha256:' + 'a' * 64
    new = 'ghcr.io/synthetic/server@sha256:' + 'b' * 64
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}", STACK=str(work),
               CALLS=str(tmp_path / 'calls'), OLD_REF=old, PROBE=probe, PROBE_CODE=code)
    result = subprocess.run(['bash', str(script), new, '--skip-backup'], env=env,
                            capture_output=True, text=True)
    assert result.returncode == 1
    assert 'planned' in result.stderr
    assert old in (tmp_path / 'state/pin.yaml').read_text()
    calls = (tmp_path / 'calls').read_text()
    assert 'alembic upgrade' not in calls and ' up ' not in calls and ' stop ' not in calls


def test_make_probe_bypasses_entrypoint_and_rejects_unknown_state():
    make = (ROOT / 'Makefile').read_text()
    deploy = make.split('deploy: image', 1)[1].split('\nup:', 1)[0]
    assert 'run --rm --no-deps -e RUN_MIGRATIONS=false obsidian-mcp python scripts/migration_status.py' in deploy
    assert '|| exit 1' in deploy
    assert '*) echo "Cannot establish migration status; refusing deployment." >&2; exit 1' in deploy
    assert 'run --rm -e RUN_MIGRATIONS=false obsidian-mcp alembic upgrade head' in deploy
