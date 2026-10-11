"""Test-only management harness around the actual node runtime, not a fake endpoint.

Bind control exclusively to management bridge. This image is never a production service.
"""
import json
import os
import re
import subprocess
import socket
import ssl
from pathlib import Path
import threading
from uuid import UUID, uuid4
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import httpx
from awg_agent.certificates import CertificateManager
from awg_agent.trusttunnel import TrustTunnelService
from awg_agent.trusttunnel_runtime import TrustTunnelRuntime
import awg_agent.trusttunnel_runtime as runtime_module
from awg_contracts import TrustTunnelApplyRequest, TrustTunnelDesired

NODE = UUID('00000000-0000-4000-8000-000000000001')
service = None
run_directory = None
lock = threading.RLock()
solver = None
solver_stop = threading.Event()
os.umask(0o077)
normal_ingress_config = runtime_module.ingress_config
lego_diagnostics = []
normal_popen = subprocess.Popen


def diagnostic_popen(argv, *args, **kwargs):
    process = normal_popen(argv, *args, **kwargs)
    if argv[0] == 'lego' and 'run' in argv:
        normal_wait = process.wait
        def wait(*args, **wait_kwargs):
            result = normal_wait(*args, **wait_kwargs)
            if result and not getattr(process, '_lab_diagnostic_captured', False):
                process._lab_diagnostic_captured = True
                stream = kwargs['stdout']
                stream.flush()
                stream.seek(0)
                message = stream.read(131072).decode(errors='replace')
                message = re.sub(r'https?://\S+|[\w.+-]+@[\w.-]+|[A-Za-z0-9_-]{24,}', '[redacted]', message)
                lego_diagnostics.append(message[-4000:])
            return result
        process.wait = wait
    return process


subprocess.Popen = diagnostic_popen


def new_service(directory):
    directory.mkdir(parents=True, exist_ok=True)
    api_ca = Path('/opt/tt-lab/pebble-ca.pem')
    root = httpx.get('https://pebble:15000/roots/0', verify=str(api_ca),
                     trust_env=False, timeout=5).raise_for_status().text
    ca_bundle = directory / 'test-ca.pem'
    ca_bundle.write_text(api_ca.read_text() + '\n' + root)
    os.environ['TT_TEST_ACME_CA'] = str(ca_bundle)
    return TrustTunnelService(directory / 'journal', NODE,
        TrustTunnelRuntime(directory / 'runtime'), CertificateManager(directory / 'acme'))


def action(path, body):
    global service, run_directory, solver
    if path == '/reset':
        if service:
            service.close()
        # Failure injection at real HTTP frontend; TLS listener and actual CA stay live.
        def ingress_config(*args):
            config = normal_ingress_config(*args)
            if body.get('http_failure'):
                config = config.replace('  default_backend acme_http',
                    '  http-request return status 503\n  default_backend acme_http')
            return config
        runtime_module.ingress_config = ingress_config
        run_directory = Path('/state') / uuid4().hex
        service = new_service(run_directory)
        return {'reset': True}
    if path == '/apply':
        return service.apply(TrustTunnelApplyRequest.model_validate(body)).model_dump(mode='json')
    if path == '/state':
        return service.state(UUID(body['id'])).model_dump(mode='json')
    if path == '/export':
        # Test-only: Pebble staging deliberately never passes production publication gate.
        desired = TrustTunnelDesired.model_validate(service._load(UUID(body['id']))['effective'])
        peer = next(client for client in desired.clients if str(client.peer_id) == body['peer_id'])
        return {format: service.runtime.export(desired, peer, format)
                for format in ('toml', 'deeplink')}
    if path == '/tick':
        service.run_once()
        return {'tick': True}
    if path == '/renew':
        desired = TrustTunnelDesired.model_validate(service._load(UUID(body['id']))['desired'])
        record = service.certificates.metadata(desired.profile.tls)
        record['next_attempt_at'] = None
        service.certificates._save(desired.profile.tls, record)
        # Test-only pinned lego5 flag: a fresh Pebble certificate is otherwise
        # correctly retained until ARI/renewal policy says it is due.
        run = service.certificates._run
        service.certificates._run = lambda argv: run(argv + ['--renew-force'])
        try:
            service.run_once()
        finally:
            service.certificates._run = run
        return service.state(desired.deployment_id).model_dump(mode='json')
    if path == '/recover':
        service.close()
        service = new_service(run_directory)
        service.recover()
        return {'recovered': True}
    if path == '/ca':
        response = httpx.get('https://pebble:15000/roots/0',
                            verify='/opt/tt-lab/pebble-ca.pem', trust_env=False, timeout=5)
        return {'ca': response.raise_for_status().text}
    if path == '/method':
        desired = TrustTunnelDesired.model_validate(service._load(UUID(body['id']))['desired'])
        return {'method': service.certificates.metadata(desired.profile.tls).get('method')}
    if path == '/diagnostic':
        return {'lego': lego_diagnostics}
    if path == '/health':
        return {'alive': True}
    if path == '/solver-start':
        desired = TrustTunnelDesired.model_validate(service._load(UUID(body['id']))['desired'])
        bundle = service.certificates.current(desired.profile.tls)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(bundle['certificate_path'], bundle['key_path'])
        context.set_alpn_protocols(['acme-tls/1'])
        solver = socket.socket()
        solver.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        solver.bind(('127.0.0.1', 10443))
        solver.listen()
        solver.settimeout(.2)
        solver_stop.clear()
        def listen():
            while not solver_stop.is_set():
                try:
                    connection, _ = solver.accept()
                except (TimeoutError, OSError):
                    continue
                try:
                    with context.wrap_socket(connection, server_side=True) as tls:
                        tls.recv(1)
                except (OSError, ssl.SSLError):
                    connection.close()
        threading.Thread(target=listen, daemon=True).start()
        return {'solver': True}
    if path == '/solver-stop':
        solver_stop.set()
        if solver:
            solver.close()
        return {'solver': False}
    raise ValueError('unknown lab action')


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
            with lock:
                response = action(self.path, body)
            self.send_response(200)
        except Exception as error:
            response = {'error': type(error).__name__}
            self.send_response(500)
        data = json.dumps(response).encode()
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


ThreadingHTTPServer(('10.241.12.2', 9000), Handler).serve_forever()
