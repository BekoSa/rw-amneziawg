"""Exercise the real uninstall CLI without touching Docker or a production installation."""
import base64
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize('state,original', [('absent', ''), ('present', ''), ('present', 'original-value')])
def test_restoring_empty_journal_fields_restarts_stock(tmp_path, state, original):
    installation = tmp_path / 'extension'
    installation.mkdir()
    env_file = tmp_path / 'stock.env'
    env_file.write_text('WEBHOOK_ENABLED=true\nUNRELATED=keep\n')
    encoded = base64.b64encode(original.encode()).decode()
    (installation / 'stock-changes').write_text(
        f'id\t{env_file}\tWEBHOOK_ENABLED\t{state}\t{encoded}\tstock-panel\n'
    )
    binary = tmp_path / 'bin'
    binary.mkdir()
    log = tmp_path / 'docker.log'
    log.write_text('')
    docker = binary / 'docker'
    docker.write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$TEST_DOCKER_LOG"
case "$*" in
  *project.config_files*) echo /test/compose.yaml ;;
  *project.working_dir*) echo /test ;;
  *compose.project*) echo stock ;;
  *compose.service*) echo panel ;;
esac
''')
    docker.chmod(0o700)
    result = subprocess.run(
        ['sh', str(Path(__file__).resolve().parents[2] / 'uninstall.sh'),
         '--dir', str(installation), '--stock-only'],
        env={**os.environ, 'PATH': f'{binary}:{os.environ["PATH"]}', 'TEST_DOCKER_LOG': str(log)},
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert env_file.read_text() == (
        'UNRELATED=keep\n' if state == 'absent' else f'WEBHOOK_ENABLED={original}\nUNRELATED=keep\n'
    )
    assert 'inspect stock-panel' in log.read_text(), (result.stdout, result.stderr)
    assert 'up -d --no-deps panel' in log.read_text()
