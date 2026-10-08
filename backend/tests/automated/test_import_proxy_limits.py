"""Opt-in real Nginx boundary test; upstream is a disposable local HTTP stub."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import socket
import subprocess
from threading import Thread
import time
from uuid import uuid4

import pytest


def test_real_nginx_import_limits(tmp_path):
    image = os.environ.get('DAI_TEST_NGINX_IMAGE')
    if not image:
        pytest.skip('Set DAI_TEST_NGINX_IMAGE for isolated Nginx upload boundary checks')
    received = []

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers['Content-Length'])
            assert len(self.rfile.read(length)) == length
            received.append((self.path, length))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    source = Path(__file__).resolve().parents[3] / 'frontend/nginx.conf'
    config = source.read_text().replace('listen 80;', f'listen 127.0.0.1:{port};')
    config = config.replace('http://api:8000', f'http://127.0.0.1:{server.server_port}')
    target = tmp_path / 'nginx.conf'
    target.write_text(config)
    name = 'dai-import-proxy-test-' + uuid4().hex[:10]
    try:
        subprocess.run(['docker', 'run', '-d', '--name', name, '--network', 'host',
                        '-v', f'{target}:/etc/nginx/conf.d/default.conf:ro', image],
                       check=True, capture_output=True)
        for _ in range(100):
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.2):
                    break
            except OSError:
                time.sleep(.05)
        mib = 1024 * 1024
        cases = [('/api/v1/admin/roster-imports/preview', 5 * mib + 512, 204),
                 ('/api/v1/courses/1/students/import', 2 * mib + 512, 204),
                 ('/api/v1/admin/roster-imports/preview', 6 * mib + 1, 413),
                 ('/api/v1/courses/1/students/import', 3 * mib + 1, 413),
                 ('/api/v1/ordinary-json', mib + 1, 413)]
        for path, length, expected in cases:
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
            try:
                connection.putrequest('POST', path)
                connection.putheader('Content-Length', str(length))
                connection.endheaders()
                if expected == 204:
                    connection.send(b'x' * length)
                response = connection.getresponse()
                assert response.status == expected, (path, response.status)
                response.read()
            finally:
                connection.close()
        assert received == [(path, length) for path, length, status in cases if status == 204]
    finally:
        subprocess.run(['docker', 'rm', '-f', '-v', name], capture_output=True)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
