"""JSON import regressions using invented records and temporary storage only."""
import copy
import gzip
import hashlib
import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class JsonIngestionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        for key, value in (
            ('DATA', self.folder / 'data'),
            ('DB_PATH', self.folder / 'data' / 'test.sqlite3'),
        ):
            self.addCleanup(setattr, app, key, getattr(app, key))
            setattr(app, key, value)
        previous_job = dict(app.JOB)
        self.addCleanup(app.JOB.update, previous_job)
        app.init_db()
        with app.db() as conn:
            conn.execute("INSERT INTO datasets(id,name,input_json,created_at) VALUES(1,'synthetic','[]','test')")

    def conversation(self):
        return {
            'id': 'synthetic-conversation', 'title': 'Invented import',
            'mapping': {
                'synthetic-node': {
                    'id': 'synthetic-node', 'parent': None, 'children': [],
                    'message': {
                        'id': 'synthetic-message', 'author': {'role': 'user'},
                        'create_time': 1750000000,
                        'content': {'content_type': 'text', 'parts': ['invented needle']},
                        'metadata': {},
                    },
                },
            },
        }

    def source(self, text, name='source.json'):
        path = self.folder / name
        if name.lower().endswith('.gz'):
            with gzip.open(path, 'wt', encoding='utf-8') as stream:
                stream.write(text)
        else:
            path.write_text(text, encoding='utf-8')
        return path

    def assert_empty_index(self):
        with app.db() as conn:
            for table in ('sources', 'conversations', 'messages', 'messages_fts', 'edges'):
                self.assertEqual(conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0, table)

    def test_directory_discovers_jsonl_gzip_and_mixed_case_extensions(self):
        folder = self.folder / 'exports'
        folder.mkdir()
        names = ('a.json', 'b.JSONL', 'c.ndjson', 'd.json.gz', 'e.JSONL.GZ', 'f.ndjson.gz')
        for name in (*names, 'ignore.txt', 'archive.zip'):
            (folder / name).write_bytes(b'[]')
        self.assertEqual({p.name for p in app.candidate_files([folder])}, set(names))

    def test_directory_jsonl_import_indexes_both_plain_and_compressed_files(self):
        folder = self.folder / 'exports'
        folder.mkdir()
        for name in ('one.JSONL', 'two.JSONL.GZ'):
            self.source(json.dumps(self.conversation()) + '\n', str(Path('exports') / name))
        result = app.import_paths([folder])
        self.assertEqual((result['files'], result['rows']), (2, 2))
        with app.db() as conn:
            kinds = {Path(row['path']).name: row['kind'] for row in conn.execute('SELECT path,kind FROM sources')}
        self.assertEqual(kinds, {'one.JSONL': 'json', 'two.JSONL.GZ': 'gzip'})

    def test_utf8_bom_import_preserves_original_bytes_and_checksum(self):
        for name in ('bom.json', 'bom.JSON.GZ'):
            with self.subTest(name=name):
                path = self.source('\ufeff' + json.dumps([self.conversation()]), name)
                original = path.read_bytes()
                self.assertEqual(app.import_file(path, 1), 1)
                self.assertEqual(path.read_bytes(), original)
                with app.db() as conn:
                    source = conn.execute('SELECT * FROM sources WHERE path=?', (str(path),)).fetchone()
                    self.assertEqual(source['sha256'], hashlib.sha256(original).hexdigest())
                    raw = app.api_source_raw(conn, source['id'], {})
                    self.assertTrue(raw['content'].startswith('\ufeff'))
                    searched = app.api_source_raw(conn, source['id'], {'q': ['invented needle']})
                    self.assertEqual(searched['match_count'], 1)
                    self.assertIn('invented needle', searched['content'])

    def test_nonfinite_numbers_reject_and_rollback_preceding_records(self):
        for token in ('NaN', 'Infinity', '-Infinity', '1e400', '-1e400'):
            with self.subTest(token=token):
                bad = copy.deepcopy(self.conversation())
                bad['id'] = 'second-conversation'
                bad['mapping']['synthetic-node']['message']['metadata']['score'] = 'SCORE_PLACEHOLDER'
                bad_text = json.dumps(bad).replace('"SCORE_PLACEHOLDER"', token)
                path = self.source('[' + json.dumps(self.conversation()) + ',' + bad_text + ']')
                original = path.read_bytes()
                with self.assertRaisesRegex(ValueError, '[Nn]on-finite|finite range'):
                    app.import_file(path, 1)
                self.assert_empty_index()
                self.assertEqual(path.read_bytes(), original)

    def test_strings_resembling_nonfinite_numbers_remain_strings(self):
        values = [{'value': text} for text in ('NaN', 'Infinity', '-Infinity', '1e400')]
        self.assertEqual(list(app.iter_json_values(self.source(json.dumps(values)))), values)

    def test_large_nested_value_preserves_escaped_unicode_and_following_record(self):
        text = ('escaped quote " and slash \\ with {braces} [arrays] café 🐫\n' * 100000)
        values = [{'nested': {'parts': [text], 'metadata': {'flag': False}}}, {'tail': 'retained'}]
        path = self.source(json.dumps(values, ensure_ascii=False))
        self.assertEqual(list(app.iter_json_values(path)), values)

    def test_unrepresentable_timestamps_keep_raw_values_without_breaking_api_json(self):
        for index, value in enumerate(('NaN', 'Infinity', '-Infinity', 10**400, {'epoch': 'Infinity'})):
            with self.subTest(value=value):
                conversation = self.conversation()
                message = conversation['mapping']['synthetic-node']['message']
                message.update(create_time=value, update_time=value)
                path = self.source(json.dumps([conversation]), f'time-{index}.json')
                self.assertEqual(app.import_file(path, 1), 1)
                with app.db() as conn:
                    row = conn.execute('SELECT pk,create_time,display_time,update_time FROM messages WHERE source_id=(SELECT id FROM sources WHERE path=?)', (str(path),)).fetchone()
                    self.assertEqual(tuple(row)[1:], (None, None, None))
                    detail = app.api_message(conn, row['pk'])
                    self.assertEqual(detail['raw']['message']['create_time'], value)
                    json.dumps(detail, allow_nan=False)

    def test_late_parse_failure_rolls_back_new_file_and_preserves_existing_index(self):
        original = self.source(json.dumps([self.conversation()]), 'original.json')
        app.import_paths([original])
        records = []
        for index in range(1001):
            conversation = self.conversation()
            conversation['id'] = f'synthetic-new-{index}'
            records.append(json.dumps(conversation))
        malformed = self.source('[' + ','.join(records) + ',BROKEN]', 'malformed.json')
        with self.assertRaises(ValueError):
            app.import_paths([malformed])
        self.assertEqual(app.JOB['state'], 'error')
        with app.db() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM sources').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT count(*) FROM conversations').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT count(*) FROM messages').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT count(*) FROM messages_fts').fetchone()[0], 1)
            source = conn.execute('SELECT path,sha256 FROM sources').fetchone()
            self.assertEqual(source['path'], str(original.resolve()))
            self.assertEqual(source['sha256'], app.sha256_file(original))

    def test_nested_wrappers_keep_supported_record_order(self):
        first = self.conversation()
        second = copy.deepcopy(first)
        second['id'] = 'second-conversation'
        wrapped = {'items': [{'data': [first, second]}]}
        records = list(app.normalized_records(wrapped))
        self.assertEqual([record['value'] for record in records], [first, second])

    def test_null_optional_objects_are_preserved_in_raw_records(self):
        conversation = self.conversation()
        node = conversation['mapping']['synthetic-node']
        node['message'].update(author=None, metadata=None, content=None)
        self.assertEqual(app.import_file(self.source(json.dumps([conversation])), 1), 1)
        with app.db() as conn:
            row = conn.execute('SELECT raw_json FROM messages').fetchone()
            self.assertEqual(json.loads(row['raw_json']), node)

    def test_wrapped_chronological_records_keep_locators_and_sequence(self):
        conversation = self.conversation()
        node = conversation['mapping']['synthetic-node']
        record = {
            'reader_schema': 'synthetic',
            'conversation': {'conversation_id': 'synthetic-conversation'},
            'mapping_node': {key: value for key, value in node.items() if key != 'message'},
            'message': node['message'],
            'source_locator': {'synthetic_line': 7}, 'chronological_sequence': 3,
        }
        for key in ('conversations', 'items', 'data'):
            with self.subTest(wrapper=key):
                path = self.source(json.dumps({key: [record]}), key + '.json')
                self.assertEqual(app.import_file(path, 1), 1)
                with app.db() as conn:
                    row = conn.execute('SELECT * FROM messages WHERE source_id=(SELECT id FROM sources WHERE path=?)', (str(path),)).fetchone()
                    self.assertEqual(json.loads(row['source_locator_json']), record['source_locator'])
                    self.assertEqual(row['chronological_sequence'], 3)
                    self.assertEqual(json.loads(row['raw_json']), node)

    def test_unexpected_object_fields_fail_with_field_name_and_rollback(self):
        fields = (
            ('message',), ('message', 'author'), ('message', 'metadata'),
            ('message', 'content'), ('message', 'metadata', 'finish_details'),
        )
        for fields_path in fields:
            for unexpected in ([], 'unexpected', 42, False):
                with self.subTest(field=fields_path, unexpected=unexpected):
                    conversation = self.conversation()
                    target = conversation['mapping']['synthetic-node']
                    for field in fields_path[:-1]:
                        target = target[field]
                    target[fields_path[-1]] = unexpected
                    path = self.source(json.dumps([conversation]))
                    with self.assertRaisesRegex(ValueError, fields_path[-1]):
                        app.import_file(path, 1)
                    self.assert_empty_index()

    def test_nonobject_mapping_nodes_fail_instead_of_disappearing(self):
        for unexpected in (None, [], 'unexpected', 42):
            with self.subTest(unexpected=unexpected):
                conversation = self.conversation()
                conversation['mapping']['bad-node'] = unexpected
                with self.assertRaisesRegex(ValueError, 'bad-node'):
                    app.import_file(self.source(json.dumps([conversation])), 1)
                self.assert_empty_index()

    def test_mapping_key_indexes_node_without_rewriting_missing_or_null_id(self):
        for missing in (True, False):
            with self.subTest(missing=missing):
                conversation = self.conversation()
                node = conversation['mapping']['synthetic-node']
                if missing:
                    del node['id']
                else:
                    node['id'] = None
                path = self.source(json.dumps([conversation]), f'id-{missing}.json')
                self.assertEqual(app.import_file(path, 1), 1)
                with app.db() as conn:
                    row = conn.execute('SELECT node_id,raw_json FROM messages WHERE source_id=(SELECT id FROM sources WHERE path=?)', (str(path),)).fetchone()
                    self.assertEqual(row['node_id'], 'synthetic-node')
                    self.assertEqual(json.loads(row['raw_json']), node)

    def test_empty_embedded_node_id_is_a_conflict_rather_than_a_missing_field(self):
        conversation = self.conversation()
        conversation['mapping']['synthetic-node']['id'] = ''
        with self.assertRaisesRegex(ValueError, 'differs from embedded'):
            app.import_file(self.source(json.dumps([conversation])), 1)
        self.assert_empty_index()

    def test_nonfinite_http_json_returns_400_before_starting_import(self):
        server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        with patch.object(app, 'start_import') as start_import:
            for token in ('NaN', 'Infinity', '-Infinity', '1e400'):
                with self.subTest(token=token):
                    client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                    try:
                        client.request('POST', '/api/import', '{"paths":[],"score":' + token + '}', {'Content-Type': 'application/json'})
                        response = client.getresponse()
                        self.assertEqual(response.status, 400)
                        self.assertIn('error', json.loads(response.read()))
                    finally:
                        client.close()
            start_import.assert_not_called()

    def test_import_status_remains_available_without_a_database_connection(self):
        server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        app.JOB.update(state='running', current=0, total=1, detail='Indexing synthetic large file')
        with patch.object(app, 'db', side_effect=AssertionError('Status must not open SQLite')):
            client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            try:
                client.request('GET', '/api/import/status')
                response = client.getresponse()
                self.assertEqual(response.status, 200)
                result = json.loads(response.read())
                self.assertEqual(result['job']['state'], 'running')
                self.assertEqual(result['job']['detail'], 'Indexing synthetic large file')
            finally:
                client.close()

    def test_stats_without_cached_summary_work_on_a_readonly_connection(self):
        app.import_file(self.source(json.dumps([self.conversation()])), 1)
        with app.db(readonly=True) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM dataset_summaries').fetchone()[0], 0)
            stats = app.api_stats(conn)
            self.assertEqual(stats['totals']['messages'], 1)
            self.assertEqual(stats['totals']['conversations'], 1)
            self.assertEqual(conn.execute('SELECT count(*) FROM dataset_summaries').fetchone()[0], 0)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM messages")

    def test_stats_during_import_serve_labeled_snapshot_without_opening_database(self):
        previous_cache = dict(app.STATS_CACHE)
        self.addCleanup(app.STATS_CACHE.update, previous_cache)
        self.addCleanup(app.STATS_CACHE.clear)
        app.import_paths([self.source(json.dumps([self.conversation()]))])
        with app.db(readonly=True) as conn:
            snapshot = app.api_stats(conn)
        app.STATS_CACHE[0] = snapshot
        app.JOB.update(state='running', detail='Synthetic import in progress')
        server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        with patch.object(app, 'db', side_effect=AssertionError('Stats must not open SQLite during import')):
            client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            try:
                client.request('GET', '/api/stats')
                response = client.getresponse()
                self.assertEqual(response.status, 200)
                result = json.loads(response.read())
                self.assertEqual(result['totals']['messages'], 1)
                self.assertEqual(result['summary_status'], 'before_current_import')
                self.assertEqual(result['job']['state'], 'running')
            finally:
                client.close()


if __name__ == '__main__':
    unittest.main()
