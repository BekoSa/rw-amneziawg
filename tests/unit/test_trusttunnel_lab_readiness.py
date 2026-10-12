"""Delayed CA must block lab readiness, without retrying scenario assertions."""
import ast
import importlib.util
import ssl
from pathlib import Path

import httpx
import pytest


def harness_action():
    # Load the action only: importing the harness starts its management listener.
    tree = ast.parse(Path('deploy/dev/trusttunnel/server.py').read_text())
    action = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                  and node.name == 'action')
    namespace = {'httpx': httpx, 'ssl': ssl}
    exec(compile(ast.Module(body=[action], type_ignores=[]), '<lab-action>', 'exec'), namespace)
    return namespace['action']


@pytest.mark.parametrize('error', [httpx.ConnectError, httpx.ReadTimeout])
def test_delayed_pebble_is_not_ready(monkeypatch, error):
    action = harness_action()
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        if len(calls) == 1:
            raise error('Pebble is still starting')
        return httpx.Response(200, text='CA root', request=httpx.Request('GET', url))
    monkeypatch.setattr(httpx, 'get', get)
    assert action('/health', {}) == {'alive': True, 'ca_ready': False}
    assert action('/health', {}) == {'alive': True, 'ca_ready': True}
    assert calls[0][1]['verify'] == '/opt/tt-lab/pebble-ca.pem'
    assert calls[0][1]['trust_env'] is False


def test_unexpected_ca_response_is_not_hidden(monkeypatch):
    action = harness_action()
    monkeypatch.setattr(httpx, 'get', lambda url, **kwargs: httpx.Response(
        403, request=httpx.Request('GET', url)))
    with pytest.raises(httpx.HTTPStatusError):
        action('/health', {})


def test_ca_tls_verification_failure_is_not_startup_delay(monkeypatch):
    action = harness_action()
    def get(*args, **kwargs):
        try:
            raise ssl.SSLCertVerificationError('wrong CA')
        except ssl.SSLCertVerificationError as error:
            raise httpx.ConnectError('TLS verification failed') from error
    monkeypatch.setattr(httpx, 'get', get)
    with pytest.raises(httpx.ConnectError, match='TLS verification failed'):
        action('/health', {})


def test_wait_service_requires_ca_readiness(monkeypatch):
    spec = importlib.util.spec_from_file_location('tt_lab_tests', 'tests/integration/test_trusttunnel.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    responses = iter([{'alive': True, 'ca_ready': False}, {'alive': True, 'ca_ready': True}])
    calls = []
    def call(*args, **kwargs):
        calls.append(args)
        return next(responses)
    monkeypatch.setattr(module, 'call', call)
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    assert module.wait_service()['ca_ready'] is True
    assert len(calls) == 2


def test_missing_pebble_has_bounded_readiness_wait(monkeypatch):
    spec = importlib.util.spec_from_file_location('tt_lab_timeout', 'tests/integration/test_trusttunnel.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    clock = iter([0, 0, 31])
    monkeypatch.setattr(module.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    monkeypatch.setattr(module, 'call', lambda *args, **kwargs: {'alive': True, 'ca_ready': False})
    with pytest.raises(AssertionError, match='Pebble CA did not become ready'):
        module.wait_service()
