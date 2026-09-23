import asyncio
import base64
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cli
import mcp_server
from browser import Browser
from library import Library, archive_media, export_library, merge_pages
from presentation import render_reader
from settings import MEDIA_MAX_PER_THREAD

TID = 4242
PNG_A = b'\x89PNG\r\n\x1a\nAAAA' + b'\x00' * 40
PNG_B = b'\x89PNG\r\n\x1a\nBBBB' + b'\x00' * 40
SITE_IMAGE = 'https://www.1point3acres.com/bbs/data/attachment/forum/a.png'
OSS_IMAGE = 'https://oss.1p3a.com/forum/b.png'
SAME_IMAGE = 'https://oss.1p3a.com/forum/b-copy.png'
EXTERNAL = 'https://cdn.example.org/c.png'
ATTACHMENT = 'https://www.1point3acres.com/bbs/forum.php?mod=attachment&aid=99'
RESTRICTED = 'https://www.1point3acres.com/bbs/forum.php?mod=attachment&aid=100'
EVIL = 'https://oss.1p3a.com/forum/..%2F..%2Fevil.html'


class MediaBrowser:
    """The site's media hosts as a table of bytes; a URL not in it is a download failure."""

    def __init__(self, files, too_large=()):
        self.files, self.too_large, self.reads = dict(files), set(too_large), []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read_bytes(self, url, max_bytes):
        self.reads.append(url)
        if url in self.too_large:
            raise RuntimeError('media_too_large')
        if url not in self.files:
            raise RuntimeError('media_http_404')
        mime, data = self.files[url]
        return {'mime': mime, 'data': data}


class MediaArchiveTests(unittest.TestCase):
    """Archived media sits beside the record: only what the site shows, only from its hosts, never inside the text (#39)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'library.sqlite'
        page = {'tid': TID, 'title': 'Synthetic media', 'url': f'https://www.1point3acres.com/bbs/thread-{TID}-1-1.html', 'expected_posts': 2,
                'posts': [{'pid': 1, 'text': '主楼', 'restricted': False, 'images': [{'url': SITE_IMAGE, 'alt': '图一'}, {'url': EXTERNAL, 'alt': '外链'}],
                           'attachments': [{'name': 'notes.pdf', 'url': ATTACHMENT, 'restricted': False},
                                           {'name': 'secret.zip', 'url': RESTRICTED, 'restricted': True}]},
                          {'pid': 2, 'text': '二楼', 'restricted': False, 'images': [{'url': OSS_IMAGE, 'alt': ''}, {'url': SAME_IMAGE, 'alt': ''}, {'url': EVIL, 'alt': ''}]}],
                'next_url': None}
        db = Library(self.path)
        db.save(merge_pages([page], 'Stripe'))
        db.close()
        self.files = {SITE_IMAGE: ('image/png', PNG_A), OSS_IMAGE: ('image/png', PNG_B), SAME_IMAGE: ('image/png', PNG_B),
                      ATTACHMENT: ('application/pdf', b'%PDF-1.4 synthetic'), EVIL: ('text/html', b'<html>')}

    def archive(self, browser, **kwargs):
        with patch('library.Library', side_effect=lambda: Library(self.path)), patch('library.Browser', return_value=browser), \
                patch('library.MEDIA_DIRECTORY', self.root / 'media'):
            return archive_media(TID, **kwargs)

    def record(self):
        db = Library(self.path)
        try:
            return db.get(TID), db.media_for(TID)
        finally:
            db.close()

    def test_site_media_is_archived_by_hash_while_external_and_restricted_stay_missing_by_reason(self):
        before, _ = self.record()
        browser = MediaBrowser(self.files)
        result = self.archive(browser)
        self.assertEqual(result['status'], 'complete')
        by_url = {item['url']: item for item in result['media']}
        self.assertEqual(by_url[SITE_IMAGE]['outcome'], 'archived')
        self.assertEqual(by_url[OSS_IMAGE]['outcome'], 'archived')
        self.assertEqual((by_url[SAME_IMAGE]['outcome'], by_url[SAME_IMAGE]['path']), ('reused', by_url[OSS_IMAGE]['path']))
        self.assertEqual((by_url[EXTERNAL]['outcome'], by_url[EXTERNAL]['error']), ('skipped_external', 'external_host_not_downloaded'))
        self.assertEqual((by_url[RESTRICTED]['outcome'], by_url[RESTRICTED]['error']), ('skipped_restricted', 'attachment_restricted'))
        self.assertEqual(by_url[ATTACHMENT]['mime'], 'application/pdf')
        self.assertNotIn(EXTERNAL, browser.reads)
        self.assertNotIn(RESTRICTED, browser.reads)
        media_root = (self.root / 'media').resolve()
        for url in (SITE_IMAGE, OSS_IMAGE, ATTACHMENT, EVIL):
            path = Path(by_url[url]['path'])
            self.assertTrue(path.is_file(), url)
            self.assertTrue(path.resolve().is_relative_to(media_root), 'nothing may leave the archive directory')
            self.assertEqual(path.name.split('.')[0], hashlib.sha256(self.files[url][1]).hexdigest()[:24])
        self.assertTrue(Path(by_url[EVIL]['path']).name.endswith('.bin'), 'an unknown type gets a neutral extension, never one from the page')
        self.assertEqual(Path(by_url[ATTACHMENT]['path']).suffix, '.pdf')
        after, index = self.record()
        self.assertEqual(before, after, 'the record text and references are untouched')
        self.assertEqual(len(index), 7)
        self.assertEqual(result['counts'], {'archived': 4, 'reused': 1, 'skipped_external': 1, 'skipped_restricted': 1})

    def test_a_too_large_or_failed_download_is_a_row_with_its_reason_and_only_it_is_retried(self):
        first = self.archive(MediaBrowser(self.files, too_large=[OSS_IMAGE]))
        self.assertEqual(first['status'], 'needs_attention')
        failed = next(item for item in first['media'] if item['url'] == OSS_IMAGE)
        self.assertEqual((failed['outcome'], failed['error'], failed['path']), ('failed', 'media_too_large', None))
        again = MediaBrowser(self.files)
        second = self.archive(again)
        self.assertEqual(second['status'], 'complete')
        self.assertEqual(again.reads, [OSS_IMAGE], 'archived files are not downloaded twice')
        retried = next(item for item in second['media'] if item['url'] == OSS_IMAGE)
        # Its bytes equal the copy archived under another URL in the first run, so it is reused, not stored twice.
        self.assertEqual(retried['outcome'], 'reused')
        self.assertTrue(Path(retried['path']).is_file())

    def test_the_per_call_budget_and_bad_input_are_refused_or_named(self):
        browser = MediaBrowser(self.files)
        result = self.archive(browser, limit=1)
        self.assertEqual(result['counts'].get('skipped_limit'), 4, 'five downloadable references, one budget')
        self.assertEqual(len(browser.reads), 1)
        with patch('library.Browser', side_effect=AssertionError('must not open a browser')), \
                patch('library.Library', side_effect=lambda: Library(self.path)):
            self.assertEqual(archive_media(TID, limit=0)['error'], 'invalid_media_budget')
            self.assertEqual(archive_media(TID, limit=MEDIA_MAX_PER_THREAD + 1)['error'], 'invalid_media_budget')
            self.assertEqual(archive_media(999999)['error'], 'thread_not_in_library')
            self.assertEqual(archive_media('abc')['error'], 'unsupported_thread_target')

    def test_export_carries_the_archived_files_and_marks_the_rest(self):
        self.archive(MediaBrowser(self.files))
        out = self.root / 'out'
        with patch('library.Library', side_effect=lambda: Library(self.path)):
            export_library(out, company='Stripe')
        payload = json.loads((out / '面经.json').read_text(encoding='utf-8'))
        post_one, post_two = payload['records'][0]['posts']
        self.assertTrue(post_one['images'][0]['local'].startswith('媒体/'))
        self.assertIsNone(post_one['images'][1]['local'])
        self.assertTrue((out / post_one['images'][0]['local']).is_file())
        self.assertEqual(post_two['images'][0]['local'], post_two['images'][1]['local'], 'one copy for identical content')
        self.assertTrue(post_one['attachments'][0]['local'].endswith('.pdf'))
        self.assertIsNone(post_one['attachments'][1]['local'])
        self.assertEqual(payload['archived_files'], 4)
        markdown = (out / '面经.md').read_text(encoding='utf-8')
        self.assertIn('![图一](媒体/', markdown)
        self.assertIn('（未归档）', markdown)
        self.assertIn('（受限）', markdown)
        html = render_reader(payload)
        self.assertIn('attachment_missing', html)
        self.assertIn(post_one['images'][0]['local'], html)

    def test_a_record_without_archives_exports_as_before(self):
        out = self.root / 'plain'
        with patch('library.Library', side_effect=lambda: Library(self.path)):
            export_library(out, company='Stripe')
        payload = json.loads((out / '面经.json').read_text(encoding='utf-8'))
        self.assertEqual(payload['archived_files'], 0)
        self.assertFalse((out / '媒体').exists())
        self.assertTrue(all(image['local'] is None for post in payload['records'][0]['posts'] for image in post['images']))


class MediaReadBoundaryTests(unittest.TestCase):
    def test_only_site_media_hosts_are_fetched_and_size_is_enforced(self):
        browser = Browser.__new__(Browser)
        browser.evaluate = lambda expression: self.fail('no request may be built for a refused host')
        for url in ['https://cdn.example.org/a.png', 'http://oss.1p3a.com/a.png', 'https://evil.1p3a.com.example/a.png']:
            with self.assertRaisesRegex(ValueError, 'unsupported_media_host', msg=url):
                browser.read_bytes(url, 1000)
        # Same-origin files go through fetch; the site's own host answers with CORS-readable bodies.
        browser.evaluate = lambda expression: {'status': 200, 'too_large': True, 'declared': 5000}
        with self.assertRaisesRegex(RuntimeError, 'media_too_large'):
            browser.read_bytes(ATTACHMENT, 1000)
        browser.evaluate = lambda expression: {'status': 200, 'challenge': False, 'mime': 'image/png', 'data': base64.b64encode(PNG_A).decode()}
        self.assertEqual(browser.read_bytes(ATTACHMENT, 1000), {'mime': 'image/png', 'data': PNG_A})
        browser.evaluate = lambda expression: {'status': 403, 'challenge': False, 'mime': '', 'data': ''}
        with self.assertRaisesRegex(RuntimeError, 'media_http_403'):
            browser.read_bytes(ATTACHMENT, 1000)

    def test_asset_hosts_are_read_through_the_browsers_own_network_record(self):
        """The asset hosts have no CORS header (real page: fetch failed, image load worked), so the bytes
        come from CDP's response body after an image load, with the declared size checked first."""
        from types import SimpleNamespace
        browser = Browser.__new__(Browser)
        browser.media_responses = {}
        browser.sb = SimpleNamespace(sleep=lambda seconds: None)
        loads = []

        import mycdp

        def load_image(expression):
            loads.append(expression)
            browser.media_responses[OSS_IMAGE] = SimpleNamespace(request_id=mycdp.network.RequestId('req-1'), response=SimpleNamespace(
                status=200, headers={'Content-Type': 'image/png', 'Content-Length': str(len(PNG_A))}))
            return 'load'
        browser.evaluate = load_image
        bodies = []
        browser._cdp = lambda command: bodies.append(next(command)['params']) or (base64.b64encode(PNG_A).decode(), True)
        self.assertEqual(browser.read_bytes(OSS_IMAGE, 1000), {'mime': 'image/png', 'data': PNG_A})
        self.assertIn('new Image()', loads[0])
        self.assertEqual(bodies, [{'requestId': 'req-1'}])
        with self.assertRaisesRegex(RuntimeError, 'media_too_large'):
            browser.read_bytes(OSS_IMAGE, len(PNG_A) - 1)
        browser.media_responses.clear()
        browser.evaluate = lambda expression: 'error'
        with self.assertRaisesRegex(RuntimeError, 'media_read_failed'):
            browser.read_bytes(OSS_IMAGE, 1000)


class MediaEntryPointTests(unittest.TestCase):
    def test_cli_and_mcp_route_the_archive_call(self):
        payload = {'status': 'complete', 'tid': TID, 'directory': 'd', 'counts': {}, 'media': [], 'scope': 'currently_visible_content', 'error': None}
        with patch('cli.archive_media', return_value=payload) as called, patch('sys.argv', ['cli', 'archive-media', str(TID), '--limit', '3']), \
                patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(cli.main(), 0)
        self.assertEqual((called.call_args.args, called.call_args.kwargs), ((str(TID),), {'limit': 3}))
        self.assertEqual(json.loads(out.getvalue())['tid'], TID)
        with patch('mcp_server.archive_thread_media', return_value=payload) as tool_called:
            result = asyncio.run(mcp_server.server.call_tool('archive_media', {'thread': TID}))
        self.assertFalse(result.is_error)
        self.assertEqual(tool_called.call_args.kwargs, {'limit': MEDIA_MAX_PER_THREAD})
