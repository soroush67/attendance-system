"""Security regression tests that need no running PostgreSQL server."""

import asyncio
import hashlib
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from starlette.requests import Request
from starlette.responses import Response

from main import (FONT_URL, STYLE, app, backup_status_page, dashboard, logout, page, robots_txt,
                  security_headers, session_auth_tag)


def fake_request(path='/login', scheme='http'):
    scope = {
        'type': 'http',
        'method': 'GET',
        'scheme': scheme,
        'path': path,
        'raw_path': path.encode(),
        'query_string': b'',
        'headers': [],
        'client': ('127.0.0.1', 55555),
        'server': ('localhost', 8000),
        'session': {},
    }
    return Request(scope)


class PrivacySecurityTests(unittest.TestCase):
    def test_root_sends_unauthenticated_browser_to_login(self):
        response = dashboard(fake_request('/'), db=None)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['location'], '/login')

    def test_backup_page_rejects_guest_without_reading_status(self):
        request = fake_request('/backups')
        with patch('main.read_public_backup_status') as reader:
            with self.assertRaises(HTTPException) as caught:
                backup_status_page(request, db=None)
        self.assertEqual(caught.exception.status_code, 401)
        reader.assert_not_called()

    def test_backup_page_rejects_authenticated_non_admin(self):
        request = fake_request('/backups')
        viewer = SimpleNamespace(id=12, username='viewer', active=True,
                                 role='viewer', password_hash='test-hash')
        request.session.update(uid=viewer.id, auth_tag=session_auth_tag(viewer))
        db = SimpleNamespace(get=lambda model, uid: viewer if uid == viewer.id else None)
        with patch('main.read_public_backup_status') as reader:
            with self.assertRaises(HTTPException) as caught:
                backup_status_page(request, db)
        self.assertEqual(caught.exception.status_code, 403)
        reader.assert_not_called()

    def test_backup_page_allows_authenticated_admin(self):
        request = fake_request('/backups')
        admin = SimpleNamespace(id=1, username='admin', active=True,
                                role='admin', password_hash='test-hash')
        request.session.update(uid=admin.id, auth_tag=session_auth_tag(admin))
        db = SimpleNamespace(get=lambda model, uid: admin if uid == admin.id else None)
        snapshot = {
            'state': 'healthy', 'size_bytes': 32995, 'verified_count': 1,
            'timer_active': True, 'next_run': 'tomorrow',
            'last_backup_at': None, 'checked_at': None,
        }
        with patch('main.read_public_backup_status', return_value=snapshot) as reader:
            with patch('main.page', return_value='allowed'):
                self.assertEqual(backup_status_page(request, db), 'allowed')
        reader.assert_called_once_with()

    def test_logout_clears_admin_session_before_backup_access(self):
        request = fake_request('/backups')
        admin = SimpleNamespace(id=1, username='admin', active=True,
                                role='admin', password_hash='test-hash')
        request.session.update(uid=admin.id, auth_tag=session_auth_tag(admin))
        response = logout(request)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['location'], '/login')
        self.assertFalse(request.session)
        db = SimpleNamespace(get=lambda model, uid: admin if uid == admin.id else None)
        with self.assertRaises(HTTPException) as caught:
            backup_status_page(request, db)
        self.assertEqual(caught.exception.status_code, 401)

    def test_private_api_spec_is_disabled(self):
        self.assertIsNone(app.openapi_url)
        self.assertNotIn('/openapi.json', {route.path for route in app.routes})

    def test_robots_disallows_everything(self):
        self.assertEqual(robots_txt(), 'User-agent: *\nDisallow: /\n')

    def test_login_html_contains_meta_and_two_matching_nonces(self):
        request = fake_request()
        response = page('ورود', '<p>تست</p>', request, None)
        html = response.body.decode()
        nonce = request.state.csp_nonce
        self.assertIn('name="robots" content="noindex,nofollow,noarchive,nosnippet"', html)
        self.assertEqual(html.count(f'<script nonce="{nonce}">'), 2)
        self.assertNotIn('<script>', html)
        self.assertIn(f'<link rel="preload" href="{FONT_URL}" as="font" type="font/woff2" crossorigin>', html)

    def test_vazirmatn_is_bundled_woff2_with_expected_sha(self):
        path = Path(__file__).resolve().parent.parent / 'app' / 'static' / 'fonts' / Path(FONT_URL).name
        blob = path.read_bytes()
        self.assertEqual(blob[:4], b'wOF2')
        self.assertEqual(hashlib.sha256(blob).hexdigest(),
                         '4e3fa217d38fdafc1fea4414ceb58ca5e662cf0ab5fa735a8c8c20e8b42cad92')
        self.assertTrue((path.parent / 'OFL.txt').is_file())

    def test_static_mount_only_exposes_the_font_directory(self):
        mount = next(route for route in app.routes if getattr(route, 'path', None) == '/assets/fonts')
        self.assertTrue(Path(mount.app.directory).is_dir())
        self.assertEqual(Path(mount.app.directory).name, 'fonts')
        self.assertNotIn('https://', STYLE)
        self.assertIn('font-family:"Vazirmatn",Tahoma', STYLE)
        self.assertIn('font-display:swap', STYLE)

    def test_private_headers_and_content_security_policy(self):
        request = fake_request()
        request.state.csp_nonce = 'test-script-nonce'

        async def next_handler(_request):
            return Response('OK')

        response = asyncio.run(security_headers(request, next_handler))
        self.assertEqual(response.headers['X-Robots-Tag'], 'noindex, nofollow, noarchive, nosnippet')
        self.assertEqual(response.headers['Cache-Control'], 'private, no-store, max-age=0')
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(response.headers['X-Frame-Options'], 'DENY')
        self.assertIn("script-src 'nonce-test-script-nonce'", response.headers['Content-Security-Policy'])
        self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
        self.assertIn("font-src 'self'", response.headers['Content-Security-Policy'])
        self.assertNotIn('Strict-Transport-Security', response.headers)

    def test_only_versioned_font_is_publicly_cacheable(self):
        request = fake_request(FONT_URL)

        async def next_handler(_request):
            # Reproduce Debian slim returning text/plain for an unknown .woff2.
            return Response(b'wOF2', media_type='text/plain')

        response = asyncio.run(security_headers(request, next_handler))
        self.assertEqual(response.headers['Content-Type'], 'font/woff2')
        self.assertEqual(response.headers['Cache-Control'], 'public, max-age=31536000, immutable')
        self.assertNotIn('Pragma', response.headers)
        self.assertEqual(response.headers['X-Robots-Tag'], 'noindex, nofollow, noarchive, nosnippet')

    def test_font_error_does_not_become_publicly_cacheable(self):
        request = fake_request(FONT_URL)

        async def not_found(_request):
            return Response('not found', status_code=404, media_type='text/plain')

        response = asyncio.run(security_headers(request, not_found))
        self.assertEqual(response.headers['Cache-Control'], 'private, no-store, max-age=0')
        self.assertEqual(response.headers['Content-Type'], 'text/plain; charset=utf-8')

    def test_https_only_hsts(self):
        request = fake_request(scheme='https')

        async def next_handler(_request):
            return Response('OK')

        response = asyncio.run(security_headers(request, next_handler))
        self.assertEqual(response.headers['Strict-Transport-Security'], 'max-age=31536000')
        self.assertIn("script-src 'none'", response.headers['Content-Security-Policy'])


if __name__ == '__main__':
    unittest.main()
