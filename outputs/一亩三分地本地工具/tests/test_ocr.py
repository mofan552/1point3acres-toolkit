import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cli
import mcp_server
from library import Library, merge_pages, recognize_media, export_library
from presentation import render_reader
from settings import OCR_ENGINE_NAME, OCR_MAX_IMAGES, OCR_MIN_SCORE

TID = 5151
IMAGE_A = 'https://oss.1p3a.com/forum/a.png'
IMAGE_B = 'https://oss.1p3a.com/forum/b.png'
BLANK = 'https://oss.1p3a.com/forum/blank.png'


def fake_engine(readings):
    """An OCR engine as the layer sees it: path -> [(text, score)], counting calls."""
    calls = []

    def recognise(path):
        calls.append(Path(path).name)
        return readings.get(Path(path).name, [])
    recognise.calls = calls
    return recognise


class RecognitionTests(unittest.TestCase):
    """Machine-read text lives beside the image, keyed by content, and never replaces the author (#40)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'library.sqlite'
        media = self.root / 'media' / str(TID)
        media.mkdir(parents=True)
        self.files = {}
        for name, content in [('aaaa.png', b'A' * 10), ('bbbb.png', b'B' * 10), ('cccc.png', b'C' * 10)]:
            (media / name).write_bytes(content)
            self.files[name] = str(media / name)
        page = {'tid': TID, 'title': 'Synthetic OCR', 'url': f'https://www.1point3acres.com/bbs/thread-{TID}-1-1.html', 'expected_posts': 1,
                'posts': [{'pid': 1, 'text': '正文只有这一句', 'restricted': False,
                           'images': [{'url': IMAGE_A, 'alt': ''}, {'url': IMAGE_B, 'alt': ''}, {'url': BLANK, 'alt': ''}]}], 'next_url': None}
        db = Library(self.path)
        db.save(merge_pages([page], 'Stripe'))
        for url, name, sha in [(IMAGE_A, 'aaaa.png', 'a' * 64), (IMAGE_B, 'bbbb.png', 'a' * 64), (BLANK, 'cccc.png', 'c' * 64)]:
            db.save_media(TID, {'url': url, 'pid': 1, 'kind': 'image', 'name': None, 'sha256': sha, 'path': self.files[name],
                                'bytes': 10, 'mime': 'image/png', 'fetched_at': 'T', 'outcome': 'archived', 'error': None})
        db.close()
        self.readings = {'aaaa.png': [('滑动窗口的题目截图', 0.97), ('低分噪声', 0.2)], 'bbbb.png': [('不会被读到：与 A 同哈希', 0.99)], 'cccc.png': []}

    def recognize(self, engine, **kwargs):
        with patch('library.Library', side_effect=lambda: Library(self.path)), patch('library._ocr_engine', return_value=engine):
            return recognize_media(TID, **kwargs)

    def test_text_is_stored_by_content_hash_low_scores_dropped_and_blank_images_yield_nothing(self):
        engine = fake_engine(self.readings)
        result = self.recognize(engine)
        self.assertEqual(result['status'], 'complete')
        by_url = {item['url']: item for item in result['images']}
        self.assertEqual((by_url[IMAGE_A]['outcome'], by_url[IMAGE_A]['lines'], by_url[IMAGE_A]['mean_score']), ('recognized', 1, 0.97))
        self.assertEqual(by_url[IMAGE_B]['outcome'], 'reused', 'same content hash: read once, shared')
        self.assertEqual((by_url[BLANK]['outcome'], by_url[BLANK]['lines']), ('no_text', 0))
        self.assertEqual(engine.calls, ['aaaa.png', 'cccc.png'])
        db = Library(self.path)
        try:
            stored = db.media_text('a' * 64, OCR_ENGINE_NAME)
            self.assertEqual(stored['text'], '滑动窗口的题目截图')
            self.assertEqual(db.get(TID)['posts'][0]['text'], '正文只有这一句', 'the author text is untouched')
        finally:
            db.close()
        again = fake_engine(self.readings)
        second = self.recognize(again)
        self.assertEqual(again.calls, [], 'nothing is read twice')
        self.assertEqual(second['counts'], {'reused': 3})

    def test_search_finds_recognized_text_only_on_request_and_says_where_it_matched(self):
        self.recognize(fake_engine(self.readings))
        db = Library(self.path)
        try:
            self.assertEqual(db.find('滑动窗口', company='')['records'], [])
            found = db.find('滑动窗口', company='', include_ocr=True)
            self.assertEqual([(r['tid'], r['matched_in']) for r in found['records']], [(TID, 'recognized_text')])
            self.assertTrue(found['filters']['include_ocr'])
            original = db.find('这一句', company='', include_ocr=True)
            self.assertEqual([r['matched_in'] for r in original['records']], ['original'])
            self.assertEqual(db.find('滑动窗口', company='Airbnb', include_ocr=True)['records'], [], 'facets still apply')
            with self.assertRaisesRegex(ValueError, 'invalid_include_ocr'):
                db.find('x', include_ocr='yes')
        finally:
            db.close()

    def test_export_and_reader_label_the_recognized_text_as_machine_read(self):
        self.recognize(fake_engine(self.readings))
        out = self.root / 'out'
        with patch('library.Library', side_effect=lambda: Library(self.path)):
            export_library(out, company='Stripe')
        payload = json.loads((out / '面经.json').read_text(encoding='utf-8'))
        images = payload['records'][0]['posts'][0]['images']
        self.assertEqual(images[0]['recognized']['text'], '滑动窗口的题目截图')
        self.assertEqual(images[0]['recognized']['engine'], OCR_ENGINE_NAME)
        self.assertIsNone(images[2]['recognized'], 'a blank image carries no invented text')
        markdown = (out / '面经.md').read_text(encoding='utf-8')
        self.assertIn('机器识别，非原文', markdown)
        self.assertIn('> 滑动窗口的题目截图', markdown)
        html = render_reader(payload)
        self.assertIn('recognized_text', html)

    def test_missing_engine_missing_archive_and_bad_input_are_named(self):
        self.assertEqual(self.recognize(None)['error'], 'ocr_unavailable')
        with patch('library.Library', side_effect=lambda: Library(self.path)):
            self.assertEqual(recognize_media(TID, limit=0)['error'], 'invalid_ocr_budget')
            self.assertEqual(recognize_media(TID, limit=OCR_MAX_IMAGES + 1)['error'], 'invalid_ocr_budget')
            self.assertEqual(recognize_media(999999)['error'], 'thread_not_in_library')
        db = Library(self.path)
        db.db.execute('DELETE FROM media')
        db.db.commit()
        db.close()
        self.assertEqual(self.recognize(fake_engine({}))['error'], 'no_archived_images')

    def test_an_engine_failure_on_one_image_does_not_hide_the_others(self):
        def flaky(path):
            if Path(path).name == 'aaaa.png':
                raise RuntimeError('model_crashed')
            return self.readings.get(Path(path).name, [])
        result = self.recognize(flaky)
        self.assertEqual(result['status'], 'needs_attention')
        by_url = {item['url']: item for item in result['images']}
        self.assertEqual((by_url[IMAGE_A]['outcome'], by_url[IMAGE_A]['error']), ('failed', 'model_crashed'))
        self.assertEqual(by_url[BLANK]['outcome'], 'no_text')
        self.assertGreaterEqual(OCR_MIN_SCORE, 0.5)


class RecognitionEntryPointTests(unittest.TestCase):
    def test_cli_and_mcp_route_recognition_and_the_search_switch(self):
        payload = {'status': 'complete', 'tid': TID, 'engine': OCR_ENGINE_NAME, 'counts': {}, 'images': [], 'note': 'n', 'error': None}
        with patch('cli.recognize_media', return_value=payload) as called, patch('sys.argv', ['cli', 'recognize-media', str(TID), '--limit', '2']), \
                patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(cli.main(), 0)
        self.assertEqual((called.call_args.args, called.call_args.kwargs), ((str(TID),), {'limit': 2}))
        with patch('mcp_server.recognize_thread_media', return_value=payload) as tool_called:
            result = asyncio.run(mcp_server.server.call_tool('recognize_media', {'thread': TID}))
        self.assertFalse(result.is_error)
        self.assertEqual(tool_called.call_args.kwargs, {'limit': OCR_MAX_IMAGES})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'x.sqlite'
            with patch('cli.Library', side_effect=lambda: Library(path)), patch('sys.argv', ['cli', 'search', 'x', '--include-ocr']), \
                    patch('sys.stdout', new_callable=io.StringIO) as out:
                self.assertEqual(cli.main(), 0)
            self.assertTrue(json.loads(out.getvalue())['filters']['include_ocr'])
            with patch('mcp_server.Library', side_effect=lambda: Library(path)):
                tool = asyncio.run(mcp_server.server.call_tool('interviews_search', {'query': 'x', 'include_ocr': True}))
            self.assertTrue(json.loads(tool.content[0].text)['filters']['include_ocr'])
