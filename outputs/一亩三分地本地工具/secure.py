"""Credentials protected by the OS: Windows DPAPI, or the macOS login keychain via /usr/bin/security."""
import ctypes
import json
import subprocess
import sys
from ctypes import wintypes
from settings import STATE, USERNAME, CREDENTIAL_SERVICE

PLATFORM = sys.platform
SECURITY = '/usr/bin/security'


class Blob(ctypes.Structure):
    _fields_ = [('length', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def transform(raw, encrypt):
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    function = ctypes.windll.crypt32.CryptProtectData if encrypt else ctypes.windll.crypt32.CryptUnprotectData
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise RuntimeError('windows_credential_encryption_failed')
    try:
        return ctypes.string_at(target.data, target.length)
    finally:
        release = ctypes.windll.kernel32.LocalFree
        release.argtypes = [ctypes.c_void_p]
        release.restype = ctypes.c_void_p
        release(ctypes.cast(target.data, ctypes.c_void_p))


def storage_backend():
    if PLATFORM == 'win32':
        return 'windows_dpapi'
    if PLATFORM == 'darwin':
        return 'macos_keychain'
    raise RuntimeError('unsupported_credential_platform')


def _security(*arguments, stdin=None):
    result = subprocess.run([SECURITY, *arguments], capture_output=True, text=True, input=stdin)
    return result.returncode, result.stdout


def save_credentials(username, password):
    if username != USERNAME or not password:
        raise ValueError('unexpected_or_empty_credentials')
    backend = storage_backend()
    STATE.mkdir(parents=True, exist_ok=True)
    if backend == 'windows_dpapi':
        encrypted = transform(json.dumps({'username': username, 'password': password}).encode('utf-8'), True)
        (STATE / 'credentials.dpapi').write_bytes(encrypted)
        return backend
    # /usr/bin/security prints anything but printable ASCII back as hex, which a later read could not tell apart.
    if not password.isascii() or not password.isprintable():
        raise ValueError('unsupported_password_characters')
    # A bare -w makes security read the password (twice) from stdin, so it never appears in the process list.
    # The item is created by /usr/bin/security itself, so later reads by the same tool need no keychain prompt.
    code, _ = _security('add-generic-password', '-U', '-a', username, '-s', CREDENTIAL_SERVICE, '-w',
                        stdin=password + '\n' + password + '\n')
    if code:
        raise RuntimeError('keychain_credential_save_failed')
    return backend


def load_credentials():
    backend = storage_backend()
    if backend == 'windows_dpapi':
        path = STATE / 'credentials.dpapi'
        if not path.exists():
            raise RuntimeError('login_required_credentials_not_configured')
        return json.loads(transform(path.read_bytes(), False))
    code, output = _security('find-generic-password', '-a', USERNAME, '-s', CREDENTIAL_SERVICE, '-w')
    if code:
        raise RuntimeError('login_required_credentials_not_configured')
    return {'username': USERNAME, 'password': output.rstrip('\n')}
