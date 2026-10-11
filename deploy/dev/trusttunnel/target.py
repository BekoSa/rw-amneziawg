"""Payload target, reachable only from the endpoint's target bridge."""
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class HTTP(BaseHTTPRequestHandler):
    def do_GET(self):
        data = b'trusttunnel-real-payload'
        self.send_response(200)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


class UDP(socketserver.BaseRequestHandler):
    def handle(self):
        data, connection = self.request
        connection.sendto(data, self.client_address)


class TCP(socketserver.BaseRequestHandler):
    def handle(self):
        while data := self.request.recv(4096):
            self.request.sendall(data)


for server in (socketserver.ThreadingUDPServer(('0.0.0.0', 8081), UDP),
               socketserver.ThreadingTCPServer(('0.0.0.0', 8082), TCP)):
    threading.Thread(target=server.serve_forever, daemon=True).start()
ThreadingHTTPServer(('0.0.0.0', 8080), HTTP).serve_forever()
