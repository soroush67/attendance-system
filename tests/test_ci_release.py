"""CI release guards; no Docker, Jenkins, GitLab or production access required."""

import importlib.util
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


CI = Path(__file__).resolve().parent.parent / 'ci'
SPEC = importlib.util.spec_from_file_location('digest_unit', CI / 'digest_ref.py')
digest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(digest)
RELEASE_SPEC = importlib.util.spec_from_file_location('release_guard_unit', CI / 'remote_release.py')
release_guard = importlib.util.module_from_spec(RELEASE_SPEC)
RELEASE_SPEC.loader.exec_module(release_guard)


class ImmutableReferenceTests(unittest.TestCase):
    def test_excel_template_and_synthetic_fixtures_are_ci_requirements(self):
        root = Path(__file__).resolve().parents[1]
        jenkins = (root / 'Jenkinsfile').read_text(encoding='utf-8')
        gitignore = (root / '.gitignore').read_text(encoding='utf-8')
        dockerignore = (root / 'app' / '.dockerignore').read_text(encoding='utf-8')
        self.assertIn('test -s app/.dockerignore', jenkins)
        self.assertIn('**/*.pyc', dockerignore)
        self.assertIn('**/*.key', dockerignore)
        self.assertNotIn('^certs/company-root-ca.crt', dockerignore)
        for name in (
            'app/templates/manager-attendance-template.xlsx',
            'tests/fixtures/manager-excel-150-valid.xlsx',
            'tests/fixtures/manager-excel-duplicate.xlsx',
            'tests/fixtures/manager-excel-formula.xlsx',
        ):
            self.assertIn('test -s ' + name, jenkins)
            self.assertIn('!' + name, gitignore)

    def test_valid_digest(self):
        ref = 'docker-hosted.hamainsurance.net/attendance-web@sha256:' + 'a' * 64
        self.assertEqual(digest.select_digest('docker-hosted.hamainsurance.net/attendance-web:develop-11-abcdef01', [ref]), ref)

    def test_wrong_registry_not_allowed(self):
        ref = 'attacker.example/attendance-web@sha256:' + 'a' * 64
        with self.assertRaises(ValueError):
            digest.select_digest('docker-hosted.hamainsurance.net/attendance-web:develop-11-abcdef01', [ref])

    def test_mutable_tag_not_allowed_for_release(self):
        with self.assertRaises(ValueError):
            digest.select_digest('docker-hosted.hamainsurance.net/attendance-web:latest;echo', [])

    def test_duplicate_digest_fails_closed(self):
        ref = 'docker-hosted.hamainsurance.net/attendance-web@sha256:' + 'a' * 64
        with self.assertRaises(ValueError):
            digest.select_digest('docker-hosted.hamainsurance.net/attendance-web:develop-11-abcdef01', [ref, ref])

    def test_unmigrated_database_blocks_web_release_before_recreate(self):
        with patch.object(release_guard, 'command', return_value=SimpleNamespace(returncode=1)) as cmd:
            with self.assertRaisesRegex(RuntimeError, 'Migration 0006_login_throttling'):
                release_guard.release_schema_preflight()
        self.assertEqual(cmd.call_args.args[:4], ('docker', 'exec', release_guard.WEB, 'python'))
        self.assertIn("SELECT version_num FROM alembic_version", cmd.call_args.args[-1])

    def test_migrated_database_allows_read_only_preflight(self):
        with patch.object(
            release_guard, 'command',
            return_value=SimpleNamespace(returncode=0),
        ) as mocked:
            release_guard.release_schema_preflight()

        check_source = mocked.call_args.args[-1]

        for marker in (
            '0006_login_throttling',
            'login_rate_limits',
            'ix_login_rate_limits_last_seen',
            'manager_attendance_requests',
            'pending_import_rows',
            'attendance_group',
            'uq_manager_requests_one_pending',
        ):
            self.assertIn(marker, check_source)

    def test_previous_web_image_uses_running_container_not_pointer(self):
        info = {
            'Config': {'Image': 'attendance-web:ui-v1.8.5'},
            'Image': 'sha256:' + 'a' * 64,
        }
        response = SimpleNamespace(returncode=0, stdout=(info['Image'] + '\n').encode())
        with patch.object(release_guard, 'command', return_value=response) as mocked:
            self.assertEqual(release_guard.previous_web_image(info), info['Config']['Image'])
        self.assertEqual(mocked.call_args.args[:4],
                         ('docker', 'image', 'inspect', 'attendance-web:ui-v1.8.5'))

    def test_previous_web_image_fails_closed_on_wrong_image_id(self):
        info = {
            'Config': {'Image': 'attendance-web:ui-v1.8.5'},
            'Image': 'sha256:' + 'a' * 64,
        }
        response = SimpleNamespace(returncode=0, stdout=('sha256:' + 'b' * 64).encode())
        with patch.object(release_guard, 'command', return_value=response):
            with self.assertRaisesRegex(RuntimeError, 'exact rollback'):
                release_guard.previous_web_image(info)

    def test_previous_web_image_refuses_untrusted_image(self):
        info = {
            'Config': {'Image': 'attacker.example/attendance-web:latest'},
            'Image': 'sha256:' + 'a' * 64,
        }
        with patch.object(release_guard, 'command') as mocked:
            with self.assertRaisesRegex(RuntimeError, 'approved rollback'):
                release_guard.previous_web_image(info)
        mocked.assert_not_called()

    def test_previous_image_overlay_does_not_fall_back_to_stale_compose(self):
        expected = b'services:\n  web:\n    image: attendance-web:ui-v1.8.5\n'
        self.assertEqual(release_guard.image_overlay('attendance-web:ui-v1.8.5'), expected)
        with self.assertRaisesRegex(RuntimeError, 'Invalid web image'):
            release_guard.image_overlay('evil.example/web:latest')

    def test_compose_uses_installed_standalone_cli(self):
        with patch.object(release_guard.shutil, 'which', return_value='/usr/bin/docker-compose'):
            result = release_guard.compose(Path('/tmp/isolated-release.yml'))
        self.assertEqual(result[0], 'docker-compose')
        self.assertEqual(result[-2:], ['-f', '/tmp/isolated-release.yml'])

    def test_failed_release_rolls_back_exact_running_image(self):
        original = 'attendance-web:ui-v1.8.5'
        candidate = 'docker-hosted.hamainsurance.net/attendance-web@sha256:' + 'c' * 64
        state = {'Config': {'Image': original}, 'Image': 'sha256:' + 'a' * 64}
        before = {release_guard.WEB: state}

        def fake_command(*args, **kwargs):
            if 'up' in args:
                # First recreate fails; rollback recreate succeeds.
                up_calls.append(args)
                if len(up_calls) == 1:
                    raise RuntimeError('Synthetic candidate recreate failure')
                return SimpleNamespace(returncode=0)
            if '--services' in args:
                return SimpleNamespace(returncode=0, stdout=b'web\n')
            return SimpleNamespace(returncode=0, stdout=b'')

        up_calls = []
        with tempfile.TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            release_pointer = tmp / 'compose.ci-release.yml'
            # A stale release pointer should never determine the rollback image.
            release_pointer.write_text('services:\n  web:\n    image: attendance-web:ui-v1.8.2\n')
            with (patch.object(release_guard, 'PROJECT', tmp),
                  patch.object(release_guard, 'RELEASE', release_pointer),
                  patch.object(release_guard, 'preflight', return_value=before),
                  patch.object(release_guard, 'previous_web_image', return_value=original),
                  patch.object(release_guard, 'command', side_effect=fake_command),
                  patch.object(release_guard, 'verify_after') as verify,
                  patch.object(release_guard.sys, 'stdin') as stdin):
                stdin.buffer.readline.return_value = b'fake-test-password\n'
                with self.assertRaisesRegex(RuntimeError, 'Synthetic candidate'):
                    release_guard.deploy(candidate, 'nexususer')
                self.assertEqual(release_pointer.read_bytes(),
                                 release_guard.image_overlay(original))
                self.assertEqual(len(up_calls), 2)
                verify.assert_called_once_with(before, original, rollback=True)

    def test_failed_smoke_check_rolls_back_after_successful_recreate(self):
        original = 'attendance-web:ui-v1.8.5'
        candidate = 'docker-hosted.hamainsurance.net/attendance-web@sha256:' + 'b' * 64
        before = {release_guard.WEB: {'Config': {'Image': original}}}
        with tempfile.TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            release_pointer = tmp / 'compose.ci-release.yml'

            def fake_command(*args, **kwargs):
                if '--services' in args:
                    return SimpleNamespace(returncode=0, stdout=b'web\n')
                return SimpleNamespace(returncode=0, stdout=b'')

            with (patch.object(release_guard, 'PROJECT', tmp),
                  patch.object(release_guard, 'RELEASE', release_pointer),
                  patch.object(release_guard, 'preflight', return_value=before),
                  patch.object(release_guard, 'previous_web_image', return_value=original),
                  patch.object(release_guard, 'command', side_effect=fake_command) as mocked,
                  patch.object(release_guard, 'verify_after',
                               side_effect=[RuntimeError('Synthetic smoke failure'), None]) as verify,
                  patch.object(release_guard.sys, 'stdin') as stdin):
                stdin.buffer.readline.return_value = b'fake-test-password\n'
                with self.assertRaisesRegex(RuntimeError, 'Synthetic smoke failure'):
                    release_guard.deploy(candidate, 'nexususer')
                self.assertEqual(release_pointer.read_bytes(),
                                 release_guard.image_overlay(original))
                self.assertEqual(verify.call_args_list[0].args, (before, candidate))
                self.assertEqual(verify.call_args_list[1].args, (before, original))
                self.assertEqual(verify.call_args_list[1].kwargs, {'rollback': True})
                self.assertEqual(sum('up' in call.args for call in mocked.call_args_list), 2)

    def test_offline_test_runner_excludes_only_browser_fixture(self):
        runner_file = Path(__file__).resolve().parents[1] / 'scripts' / 'run_offline_tests.py'
        spec = importlib.util.spec_from_file_location('offline_runner_unit', runner_file)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ('test_ci_release.py', 'test_security.py',
                         'test_report_actions_visual.py'):
                (root / name).touch()
            self.assertEqual(runner.discover_modules(root),
                             ['test_ci_release', 'test_security'])


if __name__ == '__main__':
    unittest.main()
