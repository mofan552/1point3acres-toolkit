import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import secure


def _fake_transform(raw, encrypt):
    """Reversible stand-in for DPAPI so the file-backed flow is testable on any platform."""
    if encrypt:
        return b'ENVELOPE:' + raw[::-1] + b':END'
    if not raw.startswith(b'ENVELOPE:') or not raw.endswith(b':END'):
        raise RuntimeError('windows_credential_encryption_failed')
    return raw[len(b'ENVELOPE:'):-len(b':END')][::-1]


class SecureTests(unittest.TestCase):
    def test_windows_backend_round_trips_through_dpapi_file(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(secure, 'PLATFORM', 'win32'), patch.object(secure, 'STATE', Path(directory)), \
                patch.object(secure, 'USERNAME', 'test_member'), patch.object(secure, 'transform', _fake_transform):
            self.assertEqual(secure.save_credentials('test_member', 'synthetic-only'), 'windows_dpapi')
            stored = (Path(directory) / 'credentials.dpapi').read_bytes()
            self.assertNotIn(b'synthetic-only', stored)
            self.assertEqual(secure.load_credentials(), {'username': 'test_member', 'password': 'synthetic-only'})
            (Path(directory) / 'credentials.dpapi').write_bytes(stored[:-9])
            with self.assertRaises(RuntimeError):
                secure.load_credentials()

    def test_macos_backend_uses_login_keychain_without_files(self):
        calls = []

        def fake_run(command, **options):
            calls.append((command, options.get('input')))
            return SimpleNamespace(returncode=0, stdout='synthetic-only\n' if 'find-generic-password' in command else '')

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(secure, 'PLATFORM', 'darwin'), patch.object(secure, 'STATE', Path(directory)), \
                patch.object(secure, 'USERNAME', 'test_member'), patch.object(secure.subprocess, 'run', fake_run):
            self.assertEqual(secure.save_credentials('test_member', 'synthetic-only'), 'macos_keychain')
            self.assertEqual(secure.load_credentials(), {'username': 'test_member', 'password': 'synthetic-only'})
            self.assertEqual(list(Path(directory).iterdir()), [])
        (save, typed), (load, _) = calls
        self.assertEqual(save[:2], [secure.SECURITY, 'add-generic-password'])
        self.assertIn('-U', save)
        self.assertEqual(save[-1], '-w')
        self.assertNotIn('synthetic-only', save)  # The password reaches security on stdin, never the process list.
        self.assertEqual(typed, 'synthetic-only\nsynthetic-only\n')
        self.assertEqual(load[:2], [secure.SECURITY, 'find-generic-password'])
        self.assertEqual(load[-1], '-w')

    def test_macos_rejects_passwords_security_would_hand_back_as_hex(self):
        with patch.object(secure, 'PLATFORM', 'darwin'), patch.object(secure, 'USERNAME', 'test_member'), \
                patch.object(secure.subprocess, 'run') as run:
            for password in ['密码', 'line\nbreak', 'tab\there']:
                with self.subTest(password=password), self.assertRaisesRegex(ValueError, '^unsupported_password_characters$'):
                    secure.save_credentials('test_member', password)
            run.assert_not_called()

    def test_macos_missing_or_rejected_item_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(secure, 'PLATFORM', 'darwin'), patch.object(secure, 'STATE', Path(directory)), \
                patch.object(secure, 'USERNAME', 'test_member'), \
                patch.object(secure.subprocess, 'run', return_value=SimpleNamespace(returncode=44, stdout='')):
            with self.assertRaisesRegex(RuntimeError, '^login_required_credentials_not_configured$'):
                secure.load_credentials()
            with self.assertRaisesRegex(RuntimeError, '^keychain_credential_save_failed$'):
                secure.save_credentials('test_member', 'synthetic-only')

    def test_unsupported_platform_fails_closed(self):
        with patch.object(secure, 'PLATFORM', 'linux'), patch.object(secure, 'USERNAME', 'test_member'):
            with self.assertRaisesRegex(RuntimeError, '^unsupported_credential_platform$'):
                secure.load_credentials()

    def test_mismatched_or_empty_credentials_are_rejected(self):
        with patch.object(secure, 'USERNAME', 'test_member'):
            for username, password in [('someone_else', 'synthetic-only'), ('test_member', '')]:
                with self.subTest(username=username), self.assertRaises(ValueError):
                    secure.save_credentials(username, password)

    if sys.platform == 'darwin':
        def test_real_keychain_round_trip(self):
            import subprocess
            import uuid
            service = 'local-test-' + uuid.uuid4().hex
            value = 'local-test-value-not-a-user-password'
            with patch.object(secure, 'PLATFORM', 'darwin'), patch.object(secure, 'USERNAME', 'test_member'), \
                    patch.object(secure, 'CREDENTIAL_SERVICE', service):
                try:
                    self.assertEqual(secure.save_credentials('test_member', value), 'macos_keychain')
                    self.assertEqual(secure.load_credentials(), {'username': 'test_member', 'password': value})
                finally:
                    removed = subprocess.run([secure.SECURITY, 'delete-generic-password', '-a', 'test_member', '-s', service],
                                             capture_output=True)
                self.assertEqual(removed.returncode, 0)
                with self.assertRaisesRegex(RuntimeError, '^login_required_credentials_not_configured$'):
                    secure.load_credentials()

    if sys.platform == 'win32':
        def test_real_dpapi_round_trip(self):
            raw = b'local-test-value-not-a-user-password'
            encrypted = secure.transform(raw, True)
            self.assertNotIn(raw, encrypted)
            self.assertEqual(secure.transform(encrypted, False), raw)
            with self.assertRaises(RuntimeError):
                secure.transform(encrypted[:-9], False)


if __name__ == '__main__':
    unittest.main()
