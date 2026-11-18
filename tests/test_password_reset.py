"""Admin password reset and session revocation, using only an in-memory DB."""

import unittest
from urllib.parse import unquote

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from main import (
    Audit, Base, User, actor, login, password_hash, password_ok,
    reset_user_password, session_auth_tag, users_page,
)


def request_for(user=None, *, csrf='test-csrf', path='/users/reset-password'):
    session = {'csrf': csrf}
    if user is not None:
        session.update({'uid': user.id, 'auth_tag': session_auth_tag(user)})
    scope = {
        'type': 'http', 'method': 'POST', 'scheme': 'http',
        'path': path, 'query_string': b'', 'headers': [],
        'client': ('127.0.0.1', 55000), 'server': ('localhost', 8000),
        'session': session,
    }
    return Request(scope)


class AdminPasswordResetTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite+pysqlite:///:memory:')
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.admin = User(username='admin-sandbox', role='admin', active=True,
                          password_hash=password_hash('AdminPassword123!'))
        self.target = User(username='viewer-sandbox', role='viewer', active=True,
                           password_hash=password_hash('OldPassword123!'))
        self.db.add_all([self.admin, self.target])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def reset(self, request=None, **kwargs):
        args = {
            'request': request or request_for(self.admin),
            'target_user_id': self.target.id,
            'admin_password': 'AdminPassword123!',
            'new_password': 'NewPassword456!',
            'confirm_password': 'NewPassword456!',
            'csrf_token': 'test-csrf',
            'db': self.db,
        }
        args.update(kwargs)
        return reset_user_password(**args)

    def test_reset_changes_only_target_password_and_records_safe_audit(self):
        prior_role, prior_active = self.target.role, self.target.active
        admin_request = request_for(self.admin)
        old_target_request = request_for(self.target)
        old_target_hash = self.target.password_hash

        response = self.reset(request=admin_request)

        self.assertEqual(response.status_code, 303)
        self.assertIn('/users', response.headers['location'])
        self.assertNotEqual(self.target.password_hash, old_target_hash)
        self.assertFalse(password_ok('OldPassword123!', self.target.password_hash))
        self.assertTrue(password_ok('NewPassword456!', self.target.password_hash))
        self.assertEqual((self.target.role, self.target.active), (prior_role, prior_active))
        audit = self.db.scalar(select(Audit).where(Audit.event == 'user_password_reset'))
        self.assertIsNotNone(audit)
        self.assertEqual(audit.actor, 'admin-sandbox')
        self.assertIn('viewer-sandbox', audit.details)
        self.assertNotIn('NewPassword456!', audit.details)
        self.assertIsNone(actor(old_target_request, self.db))
        self.assertEqual(old_target_request.session, {})
        self.assertEqual(actor(admin_request, self.db).id, self.admin.id)

    def test_new_password_login_issues_current_session_tag(self):
        self.reset()
        request = request_for(path='/login')
        response = login(request, 'viewer-sandbox', 'NewPassword456!', self.db)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(request.session['auth_tag'], session_auth_tag(self.target))
        self.assertIsNotNone(actor(request, self.db))

    def test_admin_screen_contains_safe_reset_form(self):
        self.db.add(User(username='<script>alert(1)</script>', role='viewer',
                         active=False, password_hash=password_hash('AnotherPassword!')))
        self.db.commit()
        request = request_for(self.admin, path='/users')
        response = users_page(request, self.db)
        html = response.body.decode('utf-8')
        self.assertIn('action="/users/reset-password"', html)
        self.assertIn('name="admin_password"', html)
        self.assertIn('name="confirm_password"', html)
        self.assertIn('name="csrf_token"', html)
        self.assertIn('viewer-sandbox', html)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', html)
        self.assertNotIn('<script>alert(1)</script>', html)

    def test_account_creation_avoids_autofill_and_reset_requires_explicit_target(self):
        request = request_for(self.admin, path='/users')
        html = users_page(request, self.db).body.decode('utf-8')
        self.assertIn('action="/users" autocomplete="off"', html)
        self.assertIn('id="create-username" name="username" autocomplete="off"', html)
        self.assertIn('id="create-password" name="password" type="password" autocomplete="new-password"', html)
        self.assertIn('<option value="" selected disabled>لطفاً پرسنل را انتخاب کنید</option>', html)
        self.assertIn('id="reset-target" name="target_user_id" required', html)

    def test_non_admin_cannot_reset_password(self):
        with self.assertRaises(HTTPException) as caught:
            self.reset(request=request_for(self.target))
        self.assertEqual(caught.exception.status_code, 403)
        self.assertTrue(password_ok('OldPassword123!', self.target.password_hash))

    def test_csrf_mismatch_rejected(self):
        with self.assertRaises(HTTPException) as caught:
            self.reset(csrf_token='invalid-token')
        self.assertEqual(caught.exception.status_code, 403)
        self.assertTrue(password_ok('OldPassword123!', self.target.password_hash))

    def test_bad_admin_password_and_mismatch_leave_target_unchanged(self):
        for overrides in (
            {'admin_password': 'wrong'},
            {'confirm_password': 'DifferentPassword789!'},
        ):
            with self.subTest(overrides=overrides):
                response = self.reset(**overrides)
                self.assertEqual(response.status_code, 303)
                self.assertTrue(password_ok('OldPassword123!', self.target.password_hash))
        self.assertIsNone(self.db.scalar(select(Audit).where(Audit.event == 'user_password_reset')))

    def test_short_or_long_password_rejected(self):
        for value in ('short', 'x' * 129, ' ' * 12):
            with self.subTest(length=len(value)):
                with self.assertRaises(HTTPException) as caught:
                    self.reset(new_password=value, confirm_password=value)
                self.assertEqual(caught.exception.status_code, 400)
                self.assertTrue(password_ok('OldPassword123!', self.target.password_hash))

    def test_reset_same_password_rejected(self):
        response = self.reset(new_password='OldPassword123!',
                              confirm_password='OldPassword123!')
        self.assertEqual(response.status_code, 303)
        self.assertTrue(password_ok('OldPassword123!', self.target.password_hash))

    def test_reset_self_logs_out_admin(self):
        request = request_for(self.admin)
        response = self.reset(request=request,
                              target_user_id=self.admin.id,
                              new_password='MyNewAdminPassword!',
                              confirm_password='MyNewAdminPassword!')
        self.assertEqual(response.status_code, 303)
        self.assertIn('/login', unquote(response.headers['location']))
        self.assertEqual(request.session, {})
        self.assertTrue(password_ok('MyNewAdminPassword!', self.admin.password_hash))

    def test_legacy_uid_only_session_must_log_in_again(self):
        request = request_for(path='/users')
        request.session.update({'uid': self.admin.id})
        self.assertIsNone(actor(request, self.db))
        self.assertEqual(request.session, {})


if __name__ == '__main__':
    unittest.main()
