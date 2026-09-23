import unittest
from contracts import unread_summary


class ContractTests(unittest.TestCase):
    def contract(self):
        import importlib.util
        self.assertIsNotNone(importlib.util.find_spec('contracts'), 'Shared business contracts are missing')
        import contracts
        return contracts

    def test_unknown_run_state_fails_closed(self):
        c = self.contract()
        with self.assertRaises(ValueError):
            c.is_failure({'status': 'successful-ish'})
        self.assertTrue(c.is_failure({'status': c.RunStatus.FAILED}))
        self.assertTrue(c.is_failure({'status': c.RunStatus.NEEDS_ATTENTION}))
        self.assertFalse(c.is_failure({'status': c.RunStatus.COMPLETE}))

    def test_missing_pages_take_precedence_over_permission_badge(self):
        c = self.contract()
        row = {'complete': False, 'pagination_complete': False, 'content_status': 'restricted'}
        self.assertEqual(c.record_state(row), c.CollectionStatus.PARTIAL_PAGES)
        row['pagination_complete'] = True
        self.assertEqual(c.record_state(row), c.CollectionStatus.RESTRICTED)
        row.update(complete=True, content_status='visible')
        self.assertEqual(c.record_state(row), c.CollectionStatus.COMPLETE)

    def test_collection_aggregation_preserves_partial_failure(self):
        c = self.contract()
        self.assertEqual(c.collection_status([]), c.RunStatus.NEEDS_ATTENTION)
        self.assertEqual(c.collection_status([{'status': 'failed'}]), c.RunStatus.FAILED)
        self.assertEqual(c.collection_status([{'status': 'cached'}, {'status': 'failed'}]), c.RunStatus.NEEDS_ATTENTION)
        self.assertEqual(c.collection_status([{'status': 'restricted'}]), c.RunStatus.COMPLETE)

    def test_action_reward_metadata_drives_verification(self):
        c = self.contract()
        from datetime import datetime, timezone
        from rules import verify_reward
        now = datetime(2026, 9, 10, 2, tzinfo=timezone.utc)
        for action in c.ACTIONS:
            row = {'uid': 77, 'extcredits1': 1, 'dateline': int(now.timestamp()), 'details': {'title': action.reward_title}}
            self.assertTrue(verify_reward(77, action.key, [row], now))
            self.assertFalse(verify_reward(78, action.key, [row], now))

    def test_record_validator_rejects_false_complete_and_duplicate_posts(self):
        c = self.contract()
        self.assertTrue(hasattr(c, 'validate_record'), 'Records have no shared validation')
        row = {'tid': 1, 'complete': False, 'pagination_complete': True, 'content_status': 'restricted',
               'expected_posts': 1, 'posts': [{'pid': 2, 'text': 'visible part', 'restricted': True}]}
        c.validate_record(row)
        for bad in [dict(row, complete=True), dict(row, posts=row['posts'] * 2),
                    dict(row, pagination_complete='true'), dict(row, expected_posts=3)]:
            with self.subTest(record=bad), self.assertRaises(ValueError):
                c.validate_record(bad)


class UnreadSummaryTests(unittest.TestCase):
    """A counter the site did not send is unknown, never zero (#33)."""

    def test_real_zeros_are_zeros_not_missing(self):
        result = unread_summary({'newprompt': 0, 'newpm': 0, 'chat_unread_total': 0}, 'T')
        self.assertEqual(result['counts'], {'prompt': 0, 'pm': 0, 'chat': 0})
        self.assertEqual(result['missing'], [])
        self.assertEqual(result['source'], 'user.me')

    def test_absent_or_non_integer_counters_are_unknown(self):
        for user in [{'newprompt': 3}, {'newprompt': 3, 'newpm': '0', 'chat_unread_total': True},
                     {'newprompt': 3, 'newpm': -1, 'chat_unread_total': None}]:
            with self.subTest(user=user):
                result = unread_summary(user, 'T')
                self.assertEqual(result['counts'], {'prompt': 3, 'pm': None, 'chat': None})
                self.assertEqual(result['missing'], ['pm', 'chat'])

    def test_no_identity_at_all_means_every_counter_is_unknown(self):
        result = unread_summary(None, 'T')
        self.assertEqual(result['counts'], {'prompt': None, 'pm': None, 'chat': None})
        self.assertEqual(result['missing'], ['prompt', 'pm', 'chat'])
