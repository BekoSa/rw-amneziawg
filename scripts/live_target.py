"""Hermetic TCP, UDP echo and DNS responder reachable only through the AWG server."""
import socket
import socketserver
import struct
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

class HTTP(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'awg-tunnel-payload-ok'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *_):
        pass

class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        data, connection = self.request
        connection.sendto(data, self.client_address)

class DNS(socketserver.BaseRequestHandler):
    def handle(self):
        data, connection = self.request
        if len(data) < 17:
            return
        response = data[:2] + b'\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00' + data[12:] + b'\xc0\x0c\x00\x01\x00\x01' + struct.pack('!I', 30) + b'\x00\x04' + socket.inet_aton('10.240.5.3')
        connection.sendto(response, self.client_address)

for port, handler in [(8081, Echo), (5353, DNS)]:
    server = socketserver.ThreadingUDPServer(('0.0.0.0', port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
HTTPServer(('0.0.0.0', 8080), HTTP).serve_forever()
