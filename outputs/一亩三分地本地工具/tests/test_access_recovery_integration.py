import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import mycdp
import browser
from tests.test_reader_integration import start_reader_browser


class AccessRecoveryIntegrationTests(unittest.TestCase):
    def test_browser_preserves_rejection_headers_when_response_body_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = root / 'fixture.html'
            page.write_text('<html><body>Synthetic response fixture</body></html>', encoding='utf-8')
            sb, process = start_reader_browser(page, root / 'profile')
            try:
                for method, status, challenge, reason in [
                        ('user.me', 401, False, 'login_required'),
                        ('dailyQuestion.get', 401, False, 'login_required'),
                        ('credit.getCreditLogs', 401, False, 'login_required'),
                        ('user.me', 403, True, 'api_challenge_not_resolved'),
                        ('user.me', 500, False, 'account_http_error'),
                        ('credit.getCreditLogs', 500, False, 'api_http_error'),
                        (None, 403, False, 'thread_http_403')]:
                    with self.subTest(method=method, status=status, challenge=challenge):
                        response = json.dumps({'status': status, 'challenge': challenge})
                        sb.evaluate("window.readCount=0;window.fetch=async()=>{window.readCount++;const config="
                            + response + ";return {status:config.status,url:location.href,"
                            "headers:new Headers(config.challenge?{'cf-mitigated':'challenge'}:{}),"
                            "text:async()=>{throw new DOMException('synthetic','TimeoutError')},"
                            "arrayBuffer:async()=>{throw new DOMException('synthetic','TimeoutError')}};}")
                        session = browser.Browser(recover_login=False)
                        session.sb = sb
                        with self.assertRaisesRegex(RuntimeError, '^' + reason + '$'):
                            if method is None:
                                session.read_html(browser.SITE + '/bbs/thread-123-1-1.html')
                            else:
                                session.rpc(method)
                        self.assertEqual(session.read_retries, 0)
                        self.assertEqual(sb.evaluate('window.readCount'), 1)
                for error_name in ['TimeoutError', 'TypeError']:
                    with self.subTest(transport_error=error_name):
                        sb.evaluate("window.readCount=0;window.fetch=async(url)=>{window.readCount++;window.lastReadUrl=url;"
                            "if(window.readCount===1)throw new DOMException('synthetic'," + json.dumps(error_name)
                            + ");return new Response('[{\"result\":{\"data\":{\"json\":{\"uid\":123456}}}}]');}")
                        session = browser.Browser(recover_login=False)
                        session.sb = sb
                        self.assertEqual(session.rpc('user.me', {'value': '__HTML____TIMEOUT__'}), {'uid': 123456})
                        self.assertEqual(session.read_retries, 1)
                        self.assertEqual(sb.evaluate('window.readCount'), 2)
                        self.assertIn('__HTML____TIMEOUT__', sb.evaluate('window.lastReadUrl'))
                sb.evaluate("window.fetch=async()=>new Response(new Uint8Array([178,226,202,212]),"
                    "{headers:{'Content-Type':'text/html;charset=gbk'}})")
                self.assertEqual(session.read_html(browser.SITE + '/bbs/thread-123-1-1.html'), '测试')
            finally:
                try:
                    sb.loop.run_until_complete(asyncio.wait_for(
                        sb.driver.connection.send(mycdp.browser.close()), timeout=5))
                    process.wait(timeout=5)
                finally:
                    if process.poll() is None:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                    process.wait(timeout=5)
        self.assertFalse(root.exists())
