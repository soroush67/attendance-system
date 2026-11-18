"""Isolated authentication throttle regression tests; no production database."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from main import (Audit, Base, LoginRateLimit, User, login, password_hash,
                  session_auth_tag)


def request_for(ip='192.0.2.10'):
    return Request({
        'type': 'http', 'method': 'POST', 'scheme': 'http',
        'path': '/login', 'query_string': b'', 'headers': [],
        'client': (ip, 5000), 'server': ('localhost', 8000),
        'session': {},
    })


class LoginThrottleTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite+pysqlite:///:memory:')
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.user = User(username='r71-admin', active=True, role='admin',
                         password_hash=password_hash('StrongSyntheticPassword!'))
        self.db.add(self.user)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def attempt(self, username='r71-admin', password='wrong', ip='192.0.2.10'):
        request = request_for(ip)
        response = login(request, username, password, self.db)
        return request, response

    def test_threshold_blocks_valid_password_with_html_429(self):
        for attempt in range(4):
            _, response = self.attempt()
            self.assertEqual(response.status_code, 303, attempt)
        _, response = self.attempt()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers['retry-after'], '900')
        self.assertIn('role="alert"', response.body.decode('utf-8'))
        request, response = self.attempt(password='StrongSyntheticPassword!')
        self.assertEqual(response.status_code, 429)
        self.assertNotIn('uid', request.session)

    def test_account_limit_shared_between_ips(self):
        for attempt in range(5):
            _, response = self.attempt(ip=f'192.0.2.{attempt + 1}')
        self.assertEqual(response.status_code, 429)
        _, response = self.attempt(password='StrongSyntheticPassword!', ip='198.51.100.3')
        self.assertEqual(response.status_code, 429)

    def test_global_ip_limit_applies_to_multiple_accounts(self):
        with patch('main.LOGIN_IP_FAILURES', 3), patch('main.LOGIN_ACCOUNT_FAILURES', 10):
            for attempt in range(3):
                _, response = self.attempt(username=f'unknown-{attempt}')
            self.assertEqual(response.status_code, 429)
            _, response = self.attempt(password='StrongSyntheticPassword!')
            self.assertEqual(response.status_code, 429)

    def test_unknown_account_and_case_variants_share_anonymous_limit(self):
        for spelling in ('DOES-NOT-EXIST', 'does-not-exist', 'Does-Not-Exist',
                         'does-not-exist', 'does-not-exist'):
            _, response = self.attempt(username=spelling)
        self.assertEqual(response.status_code, 429)
        body = response.body.decode('utf-8')
        self.assertNotIn('does-not-exist', body.lower())
        self.assertEqual(self.db.scalar(select(User).where(User.username == 'does-not-exist')), None)

    def test_success_resets_account_limit_but_preserves_peer_failures(self):
        for _ in range(2):
            self.attempt()
        request, response = self.attempt(password='StrongSyntheticPassword!')
        self.assertEqual(response.status_code, 303)
        self.assertEqual(request.session['auth_tag'], session_auth_tag(self.user))
        limits = list(self.db.scalars(select(LoginRateLimit)))
        self.assertEqual(len(limits), 2)
        self.assertEqual(sorted(row.failures for row in limits), [0, 2])
        for _ in range(4):
            _, response = self.attempt()
            self.assertEqual(response.status_code, 303)

    def test_expired_lock_allows_valid_login(self):
        for _ in range(5):
            self.attempt()
        past = datetime.now(timezone.utc) - timedelta(seconds=1000)
        for row in self.db.scalars(select(LoginRateLimit)):
            row.blocked_until = past
            row.window_started_at = past
        self.db.commit()
        request, response = self.attempt(password='StrongSyntheticPassword!')
        self.assertEqual(response.status_code, 303)
        self.assertEqual(request.session['uid'], self.user.id)

    def test_failed_login_audit_contains_no_identifiers_or_password(self):
        self.attempt(username='SECRET_ACCOUNT_NAME', password='SECRET_PASSWORD_VALUE',
                     ip='203.0.113.42')
        event = self.db.scalar(select(Audit).where(Audit.event == 'login_failed'))
        self.assertIsNotNone(event)
        self.assertEqual(event.actor, 'anonymous')
        self.assertNotIn('SECRET', event.details)
        self.assertNotIn('203.0.113.42', event.details)
        self.assertEqual(len(self.db.scalars(select(LoginRateLimit)).all()), 2)


if __name__ == '__main__':
    unittest.main()
