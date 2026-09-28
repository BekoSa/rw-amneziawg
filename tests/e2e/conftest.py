import os

import pytest


def pytest_collection_modifyitems(config, items):
    """E2E phases run only from `scripts/lab.sh e2e`, one phase per invocation."""
    phase = os.environ.get('E2E_PHASE')
    selected, deselected = [], []
    for item in items:
        if 'tests/e2e/' not in str(item.fspath).replace(os.sep, '/'):
            selected.append(item)
        elif phase and item.name == f'test_{phase}':
            selected.append(item)
        else:
            deselected.append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected


@pytest.fixture(scope='session', autouse=True)
def _require_lab():
    if not os.environ.get('E2E_PHASE'):
        pytest.skip('E2E runs only inside the Docker live lab (scripts/lab.sh e2e)')
