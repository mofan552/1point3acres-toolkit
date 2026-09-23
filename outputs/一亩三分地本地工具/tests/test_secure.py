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

        def fake_run(command, **_):
            calls.append(command)
            return SimpleNamespace(returncode=0, stdout='synthetic-only\n' if 'find-generic-password' in command else '')

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(secure, 'PLATFORM', 'darwin'), patch.object(secure, 'STATE', Path(directory)), \
                patch.object(secure, 'USERNAME', 'test_member'), patch.object(secure.subprocess, 'run', fake_run):
            self.assertEqual(secure.save_credentials('test_member', 'synthetic-only'), 'macos_keychain')
            self.assertEqual(secure.load_credentials(), {'username': 'test_member', 'password': 'synthetic-only'})
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertEqual(calls[0][:2], [secure.SECURITY, 'add-generic-password'])
        self.assertIn('-U', calls[0])
        self.assertEqual(calls[0][-2:], ['-w', 'synthetic-only'])
        self.assertEqual(calls[1][:2], [secure.SECURITY, 'find-generic-password'])
        self.assertEqual(calls[1][-1], '-w')

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
