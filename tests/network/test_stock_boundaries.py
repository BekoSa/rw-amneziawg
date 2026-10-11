"""Exercise lab orchestration with a recording Docker CLI, without accessing any database."""
import os
from pathlib import Path
import shutil
import subprocess


def test_lab_never_queries_stock_postgresql(tmp_path):
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    shutil.copyfile(Path(__file__).resolve().parents[2] / 'scripts/lab.sh', scripts / 'lab.sh')
    for name in ('host-snapshot.sh', 'live-preflight.sh'):
        path = scripts / name
        path.write_text('#!/bin/sh\n' + ('mkdir -p "$1"\n' if name == 'host-snapshot.sh' else '') + 'exit 0\n')
        path.chmod(0o700)
    binary = tmp_path / 'bin'
    binary.mkdir()
    docker = binary / 'docker'
    docker.write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$TEST_DOCKER_LOG"
case "$*" in *psql*) echo 0 ;; esac
exit 0
''')
    docker.chmod(0o700)
    log = tmp_path / 'commands.log'
    result = subprocess.run(['sh', str(scripts / 'lab.sh'), 'e2e'],
        env={**os.environ, 'PATH': f'{binary}:{os.environ["PATH"]}', 'TEST_DOCKER_LOG': str(log)},
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    commands = log.read_text().splitlines()
    assert any('E2E_PHASE=removed' in command for command in commands)
    assert not any('remnawave-db' in command and 'exec' in command for command in commands), (
        'Stock storage must not be queried by extension orchestration'
    )
