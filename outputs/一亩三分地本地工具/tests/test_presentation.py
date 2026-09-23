import importlib.util
import json
import unittest
from bs4 import BeautifulSoup


class PresentationTests(unittest.TestCase):
    def render(self, text='main text', pagination=False, **fields):
        self.assertIsNotNone(importlib.util.find_spec('presentation'), 'Shared presentation is missing')
        from presentation import render_reader
        row = {'tid': 1, 'company': 'Stripe', 'title': 'A title', 'url': 'https://example.org/thread',
               'posts': [{'pid': 2, 'text': text}], 'complete': False, 'pagination_complete': pagination,
               'content_status': 'restricted', 'tags': [], 'role': '未标注', 'level': '未标注', **fields}
        payload = {'company': 'Stripe', 'records': [row], 'exported_at': '2026-09-10T00:00:00Z', 'notes': 'test'}
        return BeautifulSoup(render_reader(payload), 'html.parser'), row

    def facets(self, **fields):
        soup, _ = self.render(**fields)
        data = json.loads(soup.select_one('#dataset').string)
        return data['records'][0]['display']['facets'], data['ui']['facets']

    def test_facets_read_the_posting_date_and_the_shared_search_text(self):
        collected = {'views': 1, 'replies': 0, 'favorites': None, 'fetched_at': '2026-09-15T00:00:00+00:00'}
        record, ui = self.facets(title='Stripe MLE Onsite', role='MLE', level='Senior', listed_date='2026-9-9',
                                 stats={**collected, 'published_at': '2026-9-22 13:21'})
        self.assertEqual((record['role'], record['level'], record['post_date']), ('MLE', 'Senior', '2026-09-22'))
        self.assertIn('stripe mle onsite', record['search_text'])
        self.assertEqual(record['search_text'], record['search_text'].lower())
        self.assertEqual(ui, {'roles': ['MLE'], 'levels': ['Senior']})
        record, ui = self.facets(listed_date='2026-9-9', stats=collected)
        self.assertEqual(record['post_date'], '2026-09-09')
        record, ui = self.facets(stats=collected)
        self.assertIsNone(record['post_date'], 'the collection time is not a posting date')
        self.assertEqual(ui, {'roles': ['未标注'], 'levels': ['未标注']})

    def test_partial_restricted_record_has_both_filters_and_one_display_state(self):
        soup, original = self.render()
        data = json.loads(soup.select_one('#dataset').string)
        display = data['records'][0]['display']
        self.assertEqual(display['state'], 'partial_pages')
        self.assertEqual(set(display['filters']), {'all', 'restricted', 'partial_pages'})
        self.assertNotIn('display', original)
        self.assertEqual(data['ui']['files']['json'], '面经.json')

    def test_hostile_text_remains_data_not_markup(self):
        soup, _ = self.render('</script><script id="pwn">alert(1)</script>')
        self.assertIsNone(soup.select_one('#pwn'))
        data = json.loads(soup.select_one('#dataset').string)
        self.assertIn('</script>', data['records'][0]['posts'][0]['text'])
        self.assertIsNotNone(soup.select_one('style'))
        self.assertEqual(len(soup.select('script[src],link[rel="stylesheet"]')), 0)
