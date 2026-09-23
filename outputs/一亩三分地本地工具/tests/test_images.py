import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cli
import mcp_server
from browser import Browser
from contracts import publish_result
from interact import create_thread
from settings import IMAGE_UPLOAD_MAX_BYTES, IMAGE_UPLOAD_MAX_COUNT
from tests.test_compose import ComposeBrowser

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 64


class ImageBrowser(ComposeBrowser):
    """The three-step upload as the site runs it; `fail_at` makes the N-th image's upload fail."""

    def __init__(self, *, fail_at=None, list_attachments=True, **kwargs):
        super().__init__(**kwargs)
        self.fail_at, self.list_attachments = fail_at, list_attachments
        self.uploads, self.deleted, self.next_aid = [], [], 5000

    def api_request(self, method, path, body):
        if path == '/api/v2/attachment/upload-init':
            self.api_calls.append((method, path, dict(body)))
            if self.fail_at is not None and len(self.uploads) + 1 == self.fail_at:
                return {'status': 200, 'body': {'errno': -1, 'msg': '图片过大'}}
            aid = self.next_aid
            self.next_aid += 1
            return {'status': 200, 'body': {'errno': 0, 'aid': aid, 'attach_url': f'https://oss.example/{aid}.png', 'upload_token': f'tok{aid}'}}
        if path == '/api/v2/attachment/upload-complete':
            self.api_calls.append((method, path, dict(body)))
            self.uploads.append(body['aid'])
            return {'status': 200, 'body': {'errno': 0, 'msg': 'OK'}}
        if path.startswith('/api/user/unused-attachments/'):
            self.api_calls.append((method, path, dict(body)))
            self.deleted.append(int(path.rsplit('/', 1)[1]))
            return {'status': 200, 'body': {'errno': 0, 'msg': 'OK'}}
        return super().api_request(method, path, body)

    def upload_file(self, token, name, mime, data):
        self.api_calls.append(('UPLOAD', token, {'name': name, 'mime': mime, 'size': len(data)}))
        return {'receipt_token': 'receipt-' + token}

    def _apply(self, path, body):
        answer = super()._apply(path, body)
        if path == '/api/threads' and self.created is not None:
            self.created['attachment_list'] = [{'aid': item['aid']} for item in body['attachments']] if self.list_attachments else []
        return answer


class ImageThreadTests(unittest.TestCase):
    """Images go up in order, all or none, and count as published only when the site lists them (#28)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.files = []
        for name in ('one.png', 'two.jpg', 'three.webp'):
            path = self.root / name
            path.write_bytes(PNG)
            self.files.append(str(path))

    def post(self, browser, message='开头 [image:2] 中间 [image:1] 结尾', images=None, **kwargs):
        with patch('interact.Browser', return_value=browser):
            return create_thread(472, '图文标题', message, images=self.files if images is None else images, **kwargs)

    def test_preview_checks_the_files_and_fixes_the_order_without_uploading(self):
        browser = ImageBrowser()
        result = self.post(browser)
        self.assertEqual((result['status'], result['submitted']), ('complete', False))
        self.assertEqual([(i['index'], i['name'], i['mime'], i['placement']) for i in result['preview']['images']],
                         [(1, 'one.png', 'image/png', 'inline'), (2, 'two.jpg', 'image/jpeg', 'inline'), (3, 'three.webp', 'image/webp', 'appended')])
        self.assertEqual(result['preview']['image_order'], [2, 1, 3])
        self.assertEqual(browser.api_calls, [])

    def test_submit_uploads_in_order_then_posts_with_placeholders_resolved_and_attachments_listed(self):
        browser = ImageBrowser()
        result = self.post(browser, submit=True)
        self.assertEqual((result['status'], result['confirmed'], result['tid']), ('complete', True, 1190999))
        steps = [(call[0], call[1]) for call in browser.api_calls]
        self.assertEqual(steps[:3], [('POST', '/api/v2/attachment/upload-init'), ('UPLOAD', 'tok5000'), ('POST', '/api/v2/attachment/upload-complete')])
        self.assertEqual(browser.uploads, [5000, 5001, 5002])
        posted = next(call[2] for call in browser.api_calls if call[1] == '/api/threads')
        self.assertEqual(posted['message'], '开头 [img]https://oss.example/5001.png[/img] 中间 [img]https://oss.example/5000.png[/img] 结尾'
                                            '\n[img]https://oss.example/5002.png[/img]')
        self.assertEqual([a['aid'] for a in posted['attachments']], [5000, 5001, 5002])
        self.assertEqual(result['preview']['image_order_verified'], True)
        self.assertEqual(browser.deleted, [])

    def test_a_failed_upload_stops_before_the_thread_and_removes_only_this_calls_uploads(self):
        browser = ImageBrowser(fail_at=2)
        result = self.post(browser, submit=True)
        self.assertEqual((result['status'], result['error'], result['tid']), ('failed', 'image_upload_rejected', None))
        self.assertFalse(any(call[1] == '/api/threads' for call in browser.api_calls))
        self.assertEqual(browser.deleted, [5000])
        self.assertFalse(result['submitted'], 'nothing was posted, so nothing is left to look up or retry')

    def test_images_the_site_does_not_list_are_not_reported_as_visible(self):
        browser = ImageBrowser(list_attachments=False)
        result = self.post(browser, submit=True)
        self.assertEqual((result['status'], result['error'], result['confirmed']), ('needs_attention', 'images_not_visible', False))

    def test_unsupported_oversized_missing_or_misplaced_images_are_refused_before_the_browser_opens(self):
        big = self.root / 'big.png'
        big.write_bytes(b'\x00' * (IMAGE_UPLOAD_MAX_BYTES + 1))
        (self.root / 'note.txt').write_text('x', encoding='utf-8')
        cases = [([str(self.root / 'note.txt')], 'x', 'unsupported_image_type'),
                 ([str(big)], 'x', 'image_too_large'),
                 ([str(self.root / 'absent.png')], 'x', 'image_not_found'),
                 ([self.files[0]] * (IMAGE_UPLOAD_MAX_COUNT + 1), 'x', 'too_many_images'),
                 (self.files[:1], '[image:2]', 'image_placeholder_out_of_range'),
                 (self.files[:1], '[image:1] [image:1]', 'image_placeholder_repeated'),
                 ('one.png', 'x', 'invalid_image_list')]
        with patch('interact.Browser', side_effect=AssertionError('must not open a browser')):
            for images, message, expected in cases:
                result = create_thread(472, '标题', message, images=images, submit=True)
                self.assertEqual((result['status'], result['error'], result['submitted']), ('failed', expected, False), expected)

    def test_the_attach_tags_the_site_appends_do_not_count_as_a_content_change(self):
        # Observed on a real publish: Discuz stores the text we sent plus "[attach]<aid>[/attach]" at the end.
        class AppendsAttachTags(ImageBrowser):
            def _apply(self, path, body):
                answer = super()._apply(path, body)
                if path == '/api/threads':
                    self.created['message_bbcode'] = body['message'] + '\n' + ''.join(f"[attach]{a['aid']}[/attach]" for a in body['attachments'])
                return answer
        browser = AppendsAttachTags()
        result = self.post(browser, submit=True)
        self.assertEqual((result['status'], result['confirmed'], result['error']), ('complete', True, None))
        self.assertTrue(result['preview']['image_order_verified'])

    def test_a_thread_without_images_is_unchanged(self):
        browser = ImageBrowser()
        result = self.post(browser, message='纯文字', images=[], submit=True)
        self.assertEqual((result['status'], result['confirmed']), ('complete', True))
        posted = next(call[2] for call in browser.api_calls if call[1] == '/api/threads')
        self.assertEqual((posted['message'], posted['attachments']), ('纯文字', []))
        self.assertNotIn('image_order_verified', result['preview'])


class UploadBoundaryTests(unittest.TestCase):
    def test_the_file_goes_to_the_uploader_once_with_the_token_and_no_cookies(self):
        browser = Browser.__new__(Browser)
        calls = []
        browser.evaluate = lambda expression: calls.append(expression) or {'transport_error': True}
        with self.assertRaisesRegex(RuntimeError, 'upload_unconfirmed'):
            browser.upload_file('tok', 'a.png', 'image/png', PNG)
        self.assertEqual(len(calls), 1)
        self.assertIn('"https://uploader.1p3a.com/"', calls[0])
        self.assertIn("form.append('upload_token',\"tok\")", calls[0])
        self.assertNotIn("credentials:'include'", calls[0])
        for token, data in [('', PNG), ('tok', b''), (None, PNG)]:
            with self.assertRaisesRegex(ValueError, 'upload_arguments_invalid'):
                browser.upload_file(token, 'a.png', 'image/png', data)
        browser.evaluate = lambda expression: self.fail('no request may be built for a refused target')
        with self.assertRaisesRegex(ValueError, 'unsupported_api_target'):
            browser.api_request('DELETE', '/api/user/unused-attachments/abc', {})
        with self.assertRaisesRegex(ValueError, 'unsupported_api_target'):
            browser.api_request('POST', '/api/v2/attachment/upload', {})


class ImageEntryPointTests(unittest.TestCase):
    def test_cli_and_mcp_pass_the_chosen_files_through(self):
        payload = publish_result('thread', submitted=False, preview={'images': []})
        with patch('cli.create_thread', return_value=payload) as called, \
                patch('sys.argv', ['cli', 'post', '472', '--subject', '标题', '--message', '正文 [image:1]', '--image', 'a.png', '--image', 'b.jpg']), \
                patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(cli.main(), 0)
        self.assertEqual(called.call_args.kwargs['images'], ['a.png', 'b.jpg'])
        with patch('mcp_server.publish_thread', return_value=payload) as tool_called:
            tool = asyncio.run(mcp_server.server.call_tool('create_thread', {'fid': 472, 'subject': '标题', 'message': '正文', 'images': ['a.png']}))
        self.assertFalse(tool.is_error)
        self.assertEqual(tool_called.call_args.kwargs['images'], ['a.png'])
