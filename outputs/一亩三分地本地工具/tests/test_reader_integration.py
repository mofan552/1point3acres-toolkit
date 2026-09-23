import asyncio
import ctypes
from ctypes import wintypes
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import mycdp
from seleniumbase import sb_cdp
from library import merge_pages
from presentation import render_reader
from settings import CHROME, WINDOWS


def _process_metrics(process):
    handle = getattr(process, '_handle', None)
    if handle is None or not WINDOWS:
        return {'process_metrics_available': False}
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    times = [wintypes.FILETIME() for _ in range(4)]
    metrics = {'process_metrics_available': False}
    if kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
        metrics['main_process_cpu_seconds'] = round(sum(
            (value.dwHighDateTime << 32) + value.dwLowDateTime for value in times[2:]) / 10000000, 3)
        metrics['process_metrics_available'] = True
    class IOCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            'read_operations', 'write_operations', 'other_operations', 'read_bytes', 'write_bytes', 'other_bytes')]
    kernel.GetProcessIoCounters.argtypes = [wintypes.HANDLE, ctypes.POINTER(IOCounters)]
    kernel.GetProcessIoCounters.restype = wintypes.BOOL
    counters = IOCounters()
    if kernel.GetProcessIoCounters(handle, ctypes.byref(counters)):
        metrics.update(main_process_read_bytes=counters.read_bytes,
                       main_process_write_bytes=counters.write_bytes, process_metrics_available=True)
    return metrics


def _read_debug_port(profile):
    port = int((profile / 'DevToolsActivePort').read_text(encoding='ascii').splitlines()[0])
    if not 1 <= port <= 65535:
        raise ValueError('Invalid debugging port')
    return port


def _startup_diagnostics(process, profile, started, failure):
    print(f'Chrome startup diagnostic: exit_code_before_cleanup={process.poll()}')
    try:
        metrics = _process_metrics(process)
    except Exception:
        metrics = {'process_metrics_available': False}
    for key, value in metrics.items():
        print(f'Chrome startup diagnostic: {key}={value}')
    if isinstance(failure, TimeoutError):
        # This observation never turns the original 15-second failure into a pass.
        observation_deadline = time.monotonic() + 5
        ready = False
        while time.monotonic() < observation_deadline and process.poll() is None:
            try:
                _read_debug_port(profile)
            except (OSError, ValueError, IndexError):
                time.sleep(0.1)
                continue
            ready = True
            print(f'Chrome startup diagnostic: late_port_file_ready_seconds={time.monotonic() - started:.3f}')
            break
        print(f'Chrome startup diagnostic: late_port_file_ready={ready}')


def _stderr_diagnostics(profile, diagnostics):
    # Reap Chrome before seeking its inherited stderr handle: file positions are shared.
    diagnostics.seek(0, 2)
    size = diagnostics.tell()
    diagnostics.seek(0)
    log = diagnostics.read(65536).decode('utf-8', errors='replace')
    # Marker presence is evidence to inspect, not proof of the startup failure's cause.
    for label, marker in {
            'devtools_listening': 'DevTools listening on',
            'port_file_write_failed': 'Error writing DevTools active port',
            'debugging_disallowed': 'Remote debugging is disallowed',
            'profile_in_use': 'profile appears to be in use',
            'existing_session': 'Opening in existing browser session',
            'gpu_process_launch_failed': 'GPU process launch failed',
            'gpu_process_crashed': 'GPU process crashed',
            'sandbox_mentioned': 'sandbox',
            'policy_mentioned': 'policy',
            'first_run_mentioned': 'first_run'}.items():
        print(f'Chrome startup diagnostic: {label}={marker in log}')
    print(f'Chrome startup diagnostic: stderr_bytes={size}, stderr_truncated={size > 65536}, profile_exists={profile.exists()}')


def start_reader_browser(page, profile):
    # Own Chrome independently of CDP: a connection retry may return an attached driver.
    diagnostics = tempfile.TemporaryFile()
    process = None
    try:
        process = subprocess.Popen([
            str(CHROME), '--headless=new', '--remote-debugging-address=127.0.0.1',
            '--remote-debugging-port=0', f'--user-data-dir={profile}',
            '--no-first-run', '--no-default-browser-check', '--disable-background-networking',
            '--enable-logging=stderr', 'about:blank'], stdout=subprocess.DEVNULL, stderr=diagnostics,
            **({'creationflags': subprocess.CREATE_NO_WINDOW} if WINDOWS else {}))
        started = time.monotonic()
        deadline = started + 15
        while True:
            if process.poll() is not None:
                raise RuntimeError('Test Chrome exited before its debugging endpoint was ready')
            try:
                port = _read_debug_port(profile)
            except (OSError, ValueError, IndexError) as error:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Test Chrome debugging endpoint was not ready: '
                        f'profile_exists={profile.exists()}, port_file_exists={(profile / "DevToolsActivePort").exists()}, '
                        f'read_error={type(error).__name__}, winerror={getattr(error, "winerror", None)}') from None
                time.sleep(0.1)
                continue
            break
        print(f'Chrome startup diagnostic: port_file_ready_seconds={time.monotonic() - started:.3f}')
        browser = sb_cdp.Chrome(page.as_uri(), host='127.0.0.1', port=port, headless=True,
                                user_data_dir=str(profile), browser_executable_path=str(CHROME))
        return browser, process
    except BaseException as failure:
        if process is not None:
            try:
                _startup_diagnostics(process, profile, started, failure)
            except Exception:
                print('Chrome startup diagnostic: diagnostic_unavailable=True')
            finally:
                try:
                    if process.poll() is None:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                    process.wait(timeout=5)
                finally:
                    if process.poll() is not None:
                        try:
                            _stderr_diagnostics(profile, diagnostics)
                        except Exception:
                            print('Chrome startup diagnostic: stderr_diagnostic_unavailable=True')
        raise
    finally:
        diagnostics.close()


class ReaderIntegrationTests(unittest.TestCase):
    def test_offline_reader_search_permissions_empty_state_and_mobile(self):
        self.assertTrue(CHROME.is_file(), 'Chrome is required for reader integration checks')
        rows = []
        for tid, text, restricted, next_url in [
                (1, '合成滑动窗口 </script><script>window.injected=true</script>', False, None),
                (2, '合成受限内容占位', True, None), (3, '合成缺页内容', False, 'https://example.org/next')]:
            page = {'tid': tid, 'title': 'Synthetic ' + str(tid), 'url': 'https://example.org/thread',
                    'expected_posts': 1, 'posts': [{'pid': tid, 'text': text, 'restricted': restricted}], 'next_url': next_url}
            rows.append(merge_pages([page], 'Stripe'))
        collected = {'views': 1, 'replies': 0, 'favorites': None, 'fetched_at': '2026-09-15T00:00:00+00:00'}
        rows[0]['listed_date'] = '2026-9-9'
        rows[1]['stats'] = {**collected, 'published_at': '2026-9-22 13:21'}
        rows[2]['role'] = 'SWE'
        rows[2]['stats'] = collected  # collected on 9-15 with no posting date at all
        payload = {'company': 'Stripe', 'records': rows, 'exported_at': '2026-09-10T00:00:00Z', 'notes': 'synthetic test only'}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = root / 'reader.html'
            page.write_text(render_reader(payload), encoding='utf-8')
            browser, process = start_reader_browser(page, root / 'profile')
            try:
                browser.loop.run_until_complete(browser.page.send(mycdp.network.enable()))
                browser.loop.run_until_complete(browser.page.send(mycdp.network.set_blocked_urls(['http://*', 'https://*'])))
                browser.set_window_rect(0, 0, 1440, 1080)
                deadline = time.monotonic() + 10
                while browser.evaluate('document.querySelectorAll(".card").length') != 3 and time.monotonic() < deadline:
                    browser.sleep(0.1)
                self.assertEqual(browser.evaluate('document.querySelectorAll(".card").length'), 3)
                self.assertFalse(browser.evaluate('Boolean(window.injected)'))
                browser.evaluate("const q=document.getElementById('query');q.value='滑动窗口';q.dispatchEvent(new Event('input',{bubbles:true}));")
                self.assertEqual(browser.evaluate('document.querySelectorAll(".card").length'), 1)
                browser.evaluate("document.getElementById('query').value='';document.getElementById('query').dispatchEvent(new Event('input'));document.getElementById('status').value='restricted';document.getElementById('status').dispatchEvent(new Event('change'));")
                self.assertEqual(browser.evaluate('document.querySelectorAll(".card").length'), 1)
                self.assertTrue(browser.evaluate('Boolean(document.querySelector("#detail .notice"))'))
                browser.evaluate("document.getElementById('status').value='complete';document.getElementById('status').dispatchEvent(new Event('change'));")
                self.assertEqual(browser.evaluate('document.querySelectorAll(".card").length'), 1)
                self.assertFalse(browser.evaluate('Boolean(document.querySelector("#detail .notice"))'))
                browser.evaluate("document.getElementById('query').value='no-matching-synthetic-record';document.getElementById('query').dispatchEvent(new Event('input'));")
                self.assertEqual(browser.evaluate('document.querySelectorAll(".card").length'), 0)
                self.assertFalse(browser.evaluate('Boolean(document.querySelector("#detail h2"))'))
                browser.evaluate("document.getElementById('query').value='';document.getElementById('query').dispatchEvent(new Event('input'));document.getElementById('status').value='all';document.getElementById('status').dispatchEvent(new Event('change'));")
                self.assertEqual(browser.evaluate('document.querySelectorAll(".card").length'), 3)
                cards = 'document.querySelectorAll(".card").length'
                heading = 'document.querySelector("#detail h2").textContent'
                set_control = "{const c=document.getElementById('%s');c.value='%s';c.dispatchEvent(new Event('change',{bubbles:true}));}"
                self.assertEqual(browser.evaluate("Array.from(document.querySelectorAll('#role option')).map(o=>o.value)"), ['', 'SWE', '未标注'])
                browser.evaluate(set_control % ('role', 'SWE'))
                self.assertEqual((browser.evaluate(cards), browser.evaluate(heading)), (1, 'Synthetic 3'))
                browser.evaluate(set_control % ('role', ''))
                browser.evaluate(set_control % ('date_from', '2026-09-10'))
                self.assertEqual((browser.evaluate(cards), browser.evaluate(heading)), (1, 'Synthetic 2'))
                browser.evaluate(set_control % ('date_from', '2026-09-01') + set_control % ('date_to', '2026-09-09'))
                self.assertEqual((browser.evaluate(cards), browser.evaluate(heading)), (1, 'Synthetic 1'))
                # Thread 3 was collected on 9-15; a collection time never satisfies a posting-date range.
                browser.evaluate(set_control % ('date_from', '2026-09-10') + set_control % ('date_to', '2026-09-20'))
                self.assertEqual(browser.evaluate(cards), 0)
                browser.evaluate("document.getElementById('reset').click();")
                self.assertEqual(browser.evaluate(cards), 3)
                self.assertEqual(browser.evaluate("['query','role','level','date_from','date_to'].map(id=>document.getElementById(id).value)"), ['', '', '', '', ''])
                self.assertEqual(browser.evaluate("document.getElementById('status').value"), 'all')
                for width in (420, 320):
                    browser.loop.run_until_complete(browser.page.send(mycdp.emulation.set_device_metrics_override(
                        width=width, height=900, device_scale_factor=1, mobile=True)))
                    self.assertEqual(browser.evaluate('innerWidth'), width)
                    self.assertTrue(browser.evaluate('matchMedia("(max-width:480px)").matches'))
                    self.assertFalse(browser.evaluate('document.body.scrollWidth>innerWidth'))
            finally:
                try:
                    browser.loop.run_until_complete(asyncio.wait_for(
                        browser.driver.connection.send(mycdp.browser.close()), timeout=5))
                finally:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        # Reap only this test's owned process, then preserve the shutdown failure.
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                        process.wait(timeout=5)
                        raise
                self.assertIsNotNone(process.returncode, 'Chrome must exit before deleting its profile')
        self.assertFalse(root.exists(), 'The temporary reader and browser profile must be removed')
