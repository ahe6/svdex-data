import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import urllib.request

from dataset import (GuestClient, PublicRedirect, TABLES, collect, entity_record, load_sqlite,
                     post_record, read_snapshot, topic_record, write_snapshot)


class GuestFixture:
    base = 'https://svdex.com'

    def __init__(self):
        self.requests = []

    def get(self, path):
        self.requests.append(path)
        if path.startswith('/categories.json'):
            return {'category_list': {'categories': [
                {'id': 5, 'name': 'Offerings', 'read_restricted': False,
                 'custom_fields': {'secret': 'NEVER_EXPORT'}, 'subcategory_list': [
                     {'id': 11, 'name': 'Public', 'read_restricted': False},
                     {'id': 8, 'name': 'Private', 'read_restricted': True}]},
                {'id': 6, 'name': 'Discussion', 'read_restricted': False}]}}
        if path == '/tags.json':
            return {'tags': [{'id': 99, 'name': 'private-tag', 'pm_only': True}],
                    'extras': {'tag_groups': [{'name': 'Sectors', 'tags': [{'id': 4, 'name': 'finance', 'count': 1234}]}]}}
        if path.startswith('/c/'):
            if '/6/' in path:
                return {'topic_list': {'topics': [self.topic(30, 6)]}}
            if 'page=0' in path:
                return {'topic_list': {'topics': [self.topic(17, 11)], 'more_topics_url': '/next'}}
            return {'topic_list': {'topics': []}}
        if path == '/t/17.json':
            return {**self.topic(17, 11), 'post_stream': {'posts': [self.post(101, 17, 1)], 'stream': [101, 102, 103, 104, 105]}}
        if path == '/t/30.json':
            return {**self.topic(30, 6), 'post_stream': {'posts': [self.post(300, 30, 1)], 'stream': [300]}}
        if path.startswith('/t/17/posts.json?'):
            return {'post_stream': {'posts': [self.post(102, 17, 2), self.post(103, 17, 3, post_type=4),
                                            self.post(104, 17, 4, hidden=True), self.post(105, 17, 5, deleted_at='yesterday')]}}
        if path.startswith('/people/'):
            user = {'id': 22, 'username': 'person', 'name': 'Person', 'email': 'NEVER_EXPORT',
                    'svdex_person': {'background': 'Public background', 'organizations': [{'id': 20, 'name': 'Company', 'private_contact': 'NEVER_EXPORT'}]}}
            return {'directory_items': [{'user': user}], 'meta': {'total_rows_directory_items': 1}}
        if path.startswith('/listings/company-directory.json?kind=companies'):
            return {'directory_items': [{'user': {'id': 20, 'username': 'company', 'name': 'Company', 'email': 'NEVER_EXPORT',
                    'svdex_company': {'kind': 'companies', 'total_funding_usd': 100, 'interest_totals': {'secret': 'NEVER_EXPORT'},
                                      'key_people': [{'name': 'Person', 'role': 'Founder', 'url': '/u/person', 'private_notes': 'NEVER_EXPORT'}]}}}],
                    'meta': {'total_rows_directory_items': 1}}
        if path.startswith('/listings/company-directory.json?kind=funds'):
            return {'directory_items': [], 'meta': {'total_rows_directory_items': 0}}
        raise AssertionError('Unexpected endpoint ' + path)

    @staticmethod
    def topic(id, category):
        return {'id': id, 'category_id': category, 'title': 'Public topic', 'visible': True, 'archetype': 'regular',
                'tags': [{'id': 4, 'name': 'finance'}], 'draft': 'NEVER_EXPORT',
                'svdex_listing': {'valuation_cap': '$10M', 'company_tag_ids': [4], 'private_bid': 'NEVER_EXPORT',
                                  'interest_totals': {'private': 'NEVER_EXPORT'}}}

    @staticmethod
    def post(id, topic, number, **changes):
        return {'id': id, 'topic_id': topic, 'user_id': 20, 'username': 'company', 'name': 'Company', 'post_number': number,
                'post_type': 1, 'hidden': False, 'deleted_at': None, 'cooked': '<p>Public reply</p>',
                'ip_address': 'NEVER_EXPORT', 'raw_email': 'NEVER_EXPORT', **changes}


class DatasetTests(unittest.TestCase):
    def test_guest_export_covers_categories_pagination_and_full_reply_stream(self):
        client = GuestFixture()
        rows = collect(client)
        self.assertEqual([r['id'] for r in rows['topics']], [17, 30])
        self.assertEqual([r['id'] for r in rows['posts']], [101, 102, 300])
        self.assertNotIn('NEVER_EXPORT', json.dumps(rows))
        self.assertFalse(any('/c/8/' in p for p in client.requests))
        self.assertTrue(any('/posts.json?' in p for p in client.requests))
        self.assertEqual(rows['tags'][0]['group'], 'Sectors')
        self.assertNotIn('count', rows['tags'][0])

    def test_private_unlisted_deleted_and_pm_topics_are_excluded(self):
        base = GuestFixture.topic(17, 11)
        for changes in ({'category_id': 8}, {'visible': False}, {'archetype': 'private_message'}, {'deleted_at': 'now'}, {'visible': None}):
            self.assertIsNone(topic_record({**base, **changes}, {11}, 'https://svdex.com'))

    def test_whisper_deleted_and_cross_topic_posts_are_excluded(self):
        post = GuestFixture.post(1, 17, 1)
        for changes in ({'post_type': 4}, {'post_type': 3}, {'hidden': True}, {'deleted_at': 'now'}, {'user_deleted': True}, {'topic_id': 99}):
            self.assertIsNone(post_record({**post, **changes}, 17, 'https://svdex.com'))

    def test_hidden_profile_facts_are_not_reconstructed_from_identity(self):
        self.assertIsNone(entity_record({'id': 1, 'username': 'hidden'}, 'companies', 'https://svdex.com'))

    def test_missing_reply_aborts_export(self):
        client = GuestFixture()
        original = client.get
        client.get = lambda path: {'post_stream': {'posts': []}} if '/posts.json?' in path else original(path)
        with self.assertRaisesRegex(ValueError, 'Incomplete post stream'):
            collect(client)

    def test_caps_abort_instead_of_publishing_partial_data(self):
        with self.assertRaisesRegex(ValueError, 'pagination exceeded'):
            collect(GuestFixture(), max_pages=1)

    def test_credentials_and_remote_http_are_not_supported(self):
        for origin in ('https://secret:password@svdex.com', 'https://svdex.com?token=x', 'http://svdex.com', 'https://svdex.com/private'):
            with self.assertRaises(ValueError):
                GuestClient(origin)
        client = GuestClient()
        for path in ('https://example.com', '//example.com'):
            with self.assertRaises(ValueError):
                client.get(path)

    def test_only_same_origin_canonical_api_redirects_are_allowed(self):
        redirect = PublicRedirect()
        request = urllib.request.Request('https://svdex.com/c/5/l/latest.json')
        self.assertEqual(redirect.redirect_request(request, None, 302, 'Found', {}, 'https://svdex.com/c/offerings/5/l/latest.json').full_url,
                         'https://svdex.com/c/offerings/5/l/latest.json')
        for target in ('https://evil.example/c/5', 'http://svdex.com/c/5', 'https://svdex.com/login', 'https://svdex.com/admin'):
            with self.assertRaises(ValueError):
                redirect.redirect_request(request, None, 302, 'Found', {}, target)

    def test_snapshot_roundtrip_sqlite_and_removals(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            rows = collect(GuestFixture())
            first = write_snapshot(rows, root / 'first', 'https://svdex.com', 'start')
            load_sqlite(root / 'first', root / 'public.sqlite')
            with sqlite3.connect(root / 'public.sqlite') as db:
                self.assertEqual(db.execute('SELECT count(*) FROM replies').fetchone()[0], 1)
                self.assertEqual(db.execute('SELECT count(*) FROM companies').fetchone()[0], 1)
            second_rows = copy.deepcopy(rows)
            second_rows['posts'] = [p for p in rows['posts'] if p['id'] != 102]
            second = write_snapshot(second_rows, root / 'second', 'https://svdex.com', 'start', root / 'first')
            removed = json.loads((root / 'second' / second['removed']['name']).read_text())
            self.assertEqual(removed['removed_ids']['posts'], [102])
            load_sqlite(root / 'second', root / 'public.sqlite')
            with sqlite3.connect(root / 'public.sqlite') as db:
                self.assertEqual(db.execute('SELECT count(*) FROM replies').fetchone()[0], 0)
            path = root / 'first' / first['files']['posts']['name']
            path.write_bytes(b'tampered')
            before = (root / 'public.sqlite').read_bytes()
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                load_sqlite(root / 'first', root / 'public.sqlite')
            self.assertEqual(before, (root / 'public.sqlite').read_bytes())

    def test_untrusted_manifest_paths_are_refused(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            write_snapshot(collect(GuestFixture()), root / 'snapshot', 'https://svdex.com', 'start')
            path = root / 'snapshot' / 'manifest.json'
            manifest = json.loads(path.read_text())
            manifest['files']['posts']['name'] = '../secret.json'
            path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, 'Unsafe dataset filename'):
                read_snapshot(path.parent)


if __name__ == '__main__':
    unittest.main()
