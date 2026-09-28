import os
from urllib.parse import urlsplit, urlunsplit

import pytest


@pytest.fixture(scope='session', autouse=True)
def _fresh_test_database():
    """Recreate the disposable `*_test` database so DB tests never see lab or earlier-run state."""
    url = os.environ.get('TEST_DATABASE_URL')
    if not url:
        yield
        return
    import psycopg
    parts = urlsplit(url)
    name = parts.path.lstrip('/')
    if not name.endswith('_test'):
        raise RuntimeError('TEST_DATABASE_URL must name a disposable *_test database')
    maintenance = urlunsplit(parts._replace(path='/postgres'))
    with psycopg.connect(maintenance, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{name}"')
    yield
