"""Isolated offline HTTP smoke test for the exact web image before activation.

Starts uvicorn on loopback with lifespan disabled and synthetic database env.
No PostgreSQL access, external network, production bind ports or mounted volumes.
"""

import hashlib
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener


BASE = 'http://127.0.0.1:18081'
FONT = '/assets/fonts/Vazirmatn-Variable-4e3fa217.woff2'
FONT_SHA = '4e3fa217d38fdafc1fea4414ceb58ca5e662cf0ab5fa735a8c8c20e8b42cad92'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


opener = build_opener(ProxyHandler({}), NoRedirect())


def request(path):
    try:
        response = opener.open(BASE + path, timeout=4)
    except HTTPError as error:
        response = error
    with response:
        return response.status, response.headers, response.read()


def smoke():
    proc = subprocess.Popen(
        ['python', '-m', 'uvicorn', 'main:app', '--host', '127.0.0.1',
         '--port', '18081', '--lifespan', 'off', '--log-level', 'error'],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    passed = False
    try:
        for attempt in range(60):
            if proc.poll() is not None:
                raise RuntimeError('Isolated uvicorn process exited before becoming ready')
            try:
                status, _, _ = request('/robots.txt')
                if status == 200:
                    break
            except (URLError, OSError):
                pass
            time.sleep(0.15)
        else:
            raise RuntimeError('Isolated uvicorn did not become ready on /robots.txt')

        expected = (
            ('/robots.txt', 200, None),
            ('/login', 200, None),
            ('/', 303, '/login'),
            ('/backups', 401, None),
            ('/openapi.json', 404, None),
        )
        for path, code, location in expected:
            observed, headers, _ = request(path)
            if observed != code or (location is not None and headers.get('Location') != location):
                raise RuntimeError(f'Unexpected isolated response: {path} HTTP {observed}')
            print(f'PASS: {path} HTTP {code}', flush=True)

        status, headers, payload = request(FONT)
        if status != 200:
            raise RuntimeError(f'Font returned HTTP {status}')
        if headers.get_content_type() != 'font/woff2':
            raise RuntimeError('Font MIME mismatch: ' + str(headers.get('Content-Type')))
        if hashlib.sha256(payload).hexdigest() != FONT_SHA:
            raise RuntimeError('Font contents SHA256 mismatch')
        if headers.get('Cache-Control') != 'public, max-age=31536000, immutable':
            raise RuntimeError('Font cache headers are not as expected')
        if 'Pragma' in headers:
            raise RuntimeError('Font response unexpectedly includes private Pragma')
        if headers.get('X-Content-Type-Options') != 'nosniff':
            raise RuntimeError('Font response must retain nosniff')
        print('PASS: real HTTP font/woff2, content SHA256, cache and nosniff', flush=True)
        passed = True
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            logs, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            logs, _ = proc.communicate()
        if not passed and logs:
            print('Isolated uvicorn stderr (tail):', file=sys.stderr)
            print(logs.decode('utf-8', 'replace')[-1800:], file=sys.stderr)


if __name__ == '__main__':
    try:
        smoke()
    except Exception as exc:
        print('FAIL:', str(exc), file=sys.stderr)
        raise SystemExit(1)
