import asyncio
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cli
import mcp_server
from browser import Browser
from contracts import publish_result
from interact import create_thread
from settings import VIDEO_UPLOAD_MAX_BYTES
from tests.test_images import ImageBrowser

MP4 = b'\x00\x00\x00\x18ftypmp42' + b'\x00' * 64


class VideoBrowser(ImageBrowser):
    """The site's native video flow: a one-off upload URL, the file sent there, then registration."""

    def __init__(self, *, grant=True, register=True, list_video=True, **kwargs):
        super().__init__(**kwargs)
        self.grant, self.register, self.list_video = grant, register, list_video
        self.video_calls = []

    def api_request(self, method, path, body):
        if path == '/api/videos/upload-url':
            self.video_calls.append((method, path, dict(body)))
            if not self.grant:
                return {'status': 200, 'body': {'errno': -1, 'msg': '视频功能未开放'}}
            return {'status': 200, 'body': {'errno': 0, 'msg': 'OK', 'uploadURL': 'https://upload.example.net/one-off', 'uid': 'vid-uid-1'}}
        if path == '/api/videos/upload':
            self.video_calls.append((method, path, dict(body)))
            if not self.register:
                return {'status': 200, 'body': {'errno': -1, 'msg': '注册失败'}}
            return {'status': 200, 'body': {'errno': 0, 'msg': 'OK', 'video_id': 'video-99'}}
        return super().api_request(method, path, body)

    def upload_file(self, token, name, mime, data, url=None):
        if url is None:
            return super().upload_file(token, name, mime, data)  # an image, through the site's image uploader
        self.video_calls.append(('UPLOAD', url, {'name': name, 'mime': mime, 'size': len(data), 'token': token}))
        return {}

    def _apply(self, path, body):
        answer = super()._apply(path, body)
        if path == '/api/threads' and self.created is not None:
            self.created['videos'] = [{'video_id': body['video_id'], 'status': 'ready'}] if body.get('video_id') and self.list_video else []
        return answer


class VideoThreadTests(unittest.TestCase):
    """One native video per thread through the site's own flow, counted as published only when listed (#41)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video = self.root / 'clip.mp4'
        self.video.write_bytes(MP4)
        self.image = self.root / 'one.png'
        self.image.write_bytes(b'\x89PNG\r\n\x1a\n' + b'\x00' * 8)

    def post(self, browser, **kwargs):
        with patch('interact.Browser', return_value=browser):
            return create_thread(472, '带视频的帖子', '正文 [image:1]', images=[str(self.image)], video=str(self.video), **kwargs)

    def test_preview_lists_the_video_without_uploading(self):
        browser = VideoBrowser()
        result = self.post(browser)
        self.assertEqual((result['status'], result['submitted']), ('complete', False))
        self.assertEqual(result['preview']['video'], {'path': str(self.video), 'name': 'clip.mp4', 'mime': 'video/mp4', 'size': len(MP4)})
        self.assertEqual(browser.video_calls, [])

    def test_submit_uploads_through_the_granted_url_registers_and_posts_the_video_id(self):
        browser = VideoBrowser()
        result = self.post(browser, submit=True)
        self.assertEqual((result['status'], result['confirmed'], result['tid']), ('complete', True, 1190999))
        self.assertEqual([(c[0], c[1]) for c in browser.video_calls],
                         [('POST', '/api/videos/upload-url'), ('UPLOAD', 'https://upload.example.net/one-off'), ('POST', '/api/videos/upload')])
        self.assertEqual(browser.video_calls[1][2]['token'], None, 'the one-off URL takes the file only, no uploader token')
        self.assertEqual(browser.video_calls[2][2], {'vid': 'vid-uid-1'})
        posted = next(call[2] for call in browser.api_calls if call[1] == '/api/threads')
        self.assertEqual(posted['video_id'], 'video-99')
        self.assertTrue(result['preview']['video_verified'])

    def test_a_refused_grant_or_registration_stops_before_the_thread_and_cleans_up_images(self):
        for browser in (VideoBrowser(grant=False), VideoBrowser(register=False)):
            result = self.post(browser, submit=True)
            self.assertEqual((result['status'], result['error'], result['tid'], result['submitted']), ('failed', 'video_upload_rejected', None, False))
            self.assertFalse(any(call[1] == '/api/threads' for call in browser.api_calls))
            self.assertEqual(browser.deleted, [5000], 'the image uploaded for this thread is removed again')

    def test_a_video_the_thread_does_not_list_is_not_reported_as_visible(self):
        result = self.post(VideoBrowser(list_video=False), submit=True)
        self.assertEqual((result['status'], result['error'], result['confirmed']), ('needs_attention', 'video_not_visible', False))

    def test_bad_video_input_is_refused_before_the_browser_opens(self):
        big = self.root / 'big.mp4'
        big.write_bytes(b'\x00' * (VIDEO_UPLOAD_MAX_BYTES + 1))
        (self.root / 'clip.avi').write_bytes(MP4)
        with patch('interact.Browser', side_effect=AssertionError('must not open a browser')):
            for video, expected in [(str(self.root / 'clip.avi'), 'unsupported_video_type'), (str(big), 'video_too_large'),
                                    (str(self.root / 'absent.mp4'), 'video_not_found'), ('', 'invalid_video_reference'), (7, 'invalid_video_reference')]:
                result = create_thread(472, '标题', '正文', video=video, submit=True)
                self.assertEqual((result['status'], result['error'], result['submitted']), ('failed', expected, False), expected)

    def test_a_thread_without_a_video_is_unchanged(self):
        browser = VideoBrowser()
        with patch('interact.Browser', return_value=browser):
            result = create_thread(472, '标题', '正文', submit=True)
        posted = next(call[2] for call in browser.api_calls if call[1] == '/api/threads')
        self.assertNotIn('video_id', posted)
        self.assertNotIn('video_verified', result['preview'])
        self.assertIsNone(result['preview']['video'])


class VideoUploadBoundaryTests(unittest.TestCase):
    def test_a_granted_url_must_be_https_and_carries_no_token_field(self):
        browser = Browser.__new__(Browser)
        browser.evaluate = lambda expression: self.fail('no request may be built for a refused target')
        with self.assertRaisesRegex(ValueError, 'upload_arguments_invalid'):
            browser.upload_file(None, 'a.mp4', 'video/mp4', MP4, url='http://upload.example.net/x')
        calls = []
        # Observed live: the one-off address acknowledges with a non-JSON body; the site's own client ignores it.
        browser.evaluate = lambda expression: calls.append(expression) or {'status': 200, 'text': 'OK'}
        self.assertEqual(browser.upload_file(None, 'a.mp4', 'video/mp4', MP4, url='https://upload.example.net/x'), {})
        browser.evaluate = lambda expression: {'status': 200, 'text': 'not json'}
        with self.assertRaisesRegex(RuntimeError, 'upload_returned_non_json'):
            browser.upload_file('tok', 'a.png', 'image/png', MP4)  # the image uploader must answer with its receipt
        browser.evaluate = lambda expression: {'status': 500, 'text': ''}
        with self.assertRaisesRegex(RuntimeError, 'upload_http_500'):
            browser.upload_file(None, 'a.mp4', 'video/mp4', MP4, url='https://upload.example.net/x')
        self.assertIn('"https://upload.example.net/x"', calls[0])
        self.assertNotIn("form.append('upload_token'", calls[0])
        self.assertNotIn("credentials:'include'", calls[0])
        browser.evaluate = lambda expression: self.fail('no request may be built for a refused target')
        with self.assertRaisesRegex(ValueError, 'unsupported_api_target'):
            browser.api_request('POST', '/api/videos/1/events', {})


class VideoEntryPointTests(unittest.TestCase):
    def test_cli_and_mcp_pass_the_video_path_through(self):
        payload = publish_result('thread', submitted=False, preview={'video': None})
        with patch('cli.create_thread', return_value=payload) as called, \
                patch('sys.argv', ['cli', 'post', '472', '--subject', '标题', '--message', '正文', '--video', 'clip.mp4']), \
                patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(cli.main(), 0)
        self.assertEqual(called.call_args.kwargs['video'], 'clip.mp4')
        with patch('mcp_server.publish_thread', return_value=payload) as tool_called:
            tool = asyncio.run(mcp_server.server.call_tool('create_thread', {'fid': 472, 'subject': '标题', 'message': '正文', 'video': 'clip.mp4'}))
        self.assertFalse(tool.is_error)
        self.assertEqual(tool_called.call_args.kwargs['video'], 'clip.mp4')
