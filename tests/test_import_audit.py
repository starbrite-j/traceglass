"""Audit reports use invented sources and isolated databases only."""
import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app

spec = importlib.util.spec_from_file_location('import_audit', Path(__file__).resolve().parents[1] / 'scripts' / 'audit_import.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def conversation():
    return {'id': 'invented-conversation', 'title': 'Invented title', 'mapping': {
        'invented-node': {'id': 'invented-node', 'parent': None, 'message': {
            'id': 'invented-message', 'author': {'role': 'system'},
            'metadata': {'is_visually_hidden_from_conversation': True},
            'create_time': 1750000000,
            'content': {'content_type': 'text', 'parts': ['invented text']},
        }}}}


class ImportAuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.source = self.folder / 'invented.json'

    def scan(self, value, **kwargs):
        self.source.write_text(json.dumps(value), encoding='utf-8')
        original = self.source.read_bytes()
        result = audit.audit([self.source], self.folder / 'report', **kwargs)
        self.assertEqual(original, self.source.read_bytes())
        return result

    def entries(self):
        return [json.loads(line) for line in (self.folder / 'report' / 'items.jsonl').read_text().splitlines()]

    def test_recognition_matches_importer_and_reports_shadowed_containers(self):
        chronological = {'reader_schema': 'invented', 'mapping_node': {'id': 'node'}, 'message': None}
        cases = [conversation(), chronological, None, ['nested-array'], {'role': 'user'},
                 {'conversations': [], 'items': [conversation()]},
                 {'items': [{'data': [conversation(), chronological, 9]}], 'data': [conversation()]},
                 dict(conversation(), data=[conversation()]), dict(chronological, items=[conversation()])]
        events = []
        for value in cases:
            with self.subTest(value=value):
                expected = list(app.normalized_records(value))
                actual = list(audit.walk_records(value, '$', lambda *event: events.append(event)))
                self.assertEqual([{k: record[k] for k in ('kind', 'value')} for record in actual], expected)
        self.assertEqual(sum(event[2] == 'shadowed_container' for event in events), 4)

    def test_mixed_items_have_exact_paths_and_hidden_system_nodes_remain_recognized(self):
        result = self.scan([conversation(), {'role': 'user', 'content': 'unrecognized'}, 4])
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['sources'][0]['recognized_nodes'], 1)
        entries = self.entries()
        self.assertEqual([(e['top_level_value'], e['json_path'], e['reason']) for e in entries],
                         [(1, '$', 'unrecognized_record_shape'), (2, '$', 'unsupported_record_type')])

    def test_extraction_limits_are_warnings_with_source_ids(self):
        value = conversation()
        msg = value['mapping']['invented-node']['message']
        msg['content'] = {'content_type': 'audio', 'transcript': 'invented transcript', 'asset_pointer': 'invented-file'}
        msg['create_time'] = 'invalid date'
        result = self.scan([value])
        self.assertEqual(result['counts'].get('excluded_entries', 0), 0)
        entries = self.entries()
        self.assertEqual({e['reason'] for e in entries}, {'no_searchable_message_text', 'content_field_not_used_for_text', 'timestamp_not_used_for_date_filters'})
        self.assertTrue(all(e['node_id'] == 'invented-node' and e['conversation_id'] == 'invented-conversation' for e in entries))

    def test_chronological_message_paths_point_to_original_sibling(self):
        result = self.scan([{'reader_schema': 'invented', 'mapping_node': {'id': 'invented-node'},
                             'conversation': {'conversation_id': 'invented-conversation'},
                             'message': {'content': {'transcript': 'invented'}}}])
        self.assertEqual(result['sources'][0]['recognized_nodes'], 1)
        self.assertTrue(all(e['json_path'].startswith('$["message"]') for e in self.entries()))

    def test_failed_file_has_provisional_counts_and_preserves_source(self):
        self.source.write_text(json.dumps(conversation()) + '\nBROKEN', encoding='utf-8')
        before = app.sha256_file(self.source)
        result = audit.audit([self.source], self.folder / 'report')
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['sources'][0]['recognized_nodes'], 1)
        self.assertEqual(app.sha256_file(self.source), before)
        self.assertTrue(any(e['reason'] == 'file_audit_failed' for e in self.entries()))

    def test_folder_lists_unselected_files_and_empty_json(self):
        folder = self.folder / 'inputs'
        folder.mkdir()
        (folder / 'empty.JSON').write_text('[]')
        (folder / 'image.png').write_bytes(b'invented')
        result = audit.audit([folder], self.folder / 'report')
        self.assertEqual(result['counts']['unsupported_file_extension'], 1)
        self.assertEqual(result['sources'][0]['recognized_nodes'], 0)
        with self.assertRaisesRegex(ValueError, 'outside'):
            audit.audit([folder], folder / 'nested-report')

    def test_index_comparison_is_readonly_and_detects_absent_identity(self):
        data = self.folder / 'state'
        with patch.multiple(app, DATA=data, DB_PATH=data / 'index.sqlite3'):
            app.init_db()
            with app.db() as conn:
                conn.execute("INSERT INTO datasets(id,name,input_json,created_at) VALUES(1,'invented','[]','test')")
            self.source.write_text(json.dumps([conversation()]))
            app.import_file(self.source, 1)
            with app.db() as conn:
                conn.execute('DELETE FROM messages')
            before = app.sha256_file(app.DB_PATH)
            result = audit.audit([self.source], self.folder / 'report', app.DB_PATH)
            self.assertEqual(result['sources'][0]['recognized_nodes_absent_from_index'], 1)
            self.assertEqual(before, app.sha256_file(app.DB_PATH))
            self.assertEqual(self.entries()[0]['reason'], 'recognized_record_absent_from_index')

    def test_mismatched_source_never_claims_index_membership(self):
        data = self.folder / 'state'
        with patch.multiple(app, DATA=data, DB_PATH=data / 'index.sqlite3'):
            app.init_db()
            with app.db() as conn:
                conn.execute("INSERT INTO datasets(id,name,input_json,created_at) VALUES(1,'invented','[]','test')")
            self.source.write_text(json.dumps([conversation()]))
            app.import_file(self.source, 1)
            self.source.write_text(json.dumps([conversation(), {'extra': True}]))
            result = audit.audit([self.source], self.folder / 'report', app.DB_PATH, 1)
            self.assertFalse(result['sources'][0]['source_matches_index'])
            self.assertEqual(result['sources'][0]['indexed_nodes_verified'], 0)
            self.assertTrue(any(e['reason'] == 'source_checksum_mismatch' for e in self.entries()))

    def test_previews_remain_bounded_and_csv_cells_do_not_execute_formulas(self):
        self.assertEqual(len(audit.describe('x' * 1000000)['preview']), 240)
        self.scan({'id': '=invented_formula', 'arbitrary': 'x' * 1000000})
        with (self.folder / 'report' / 'excluded-items.csv').open(newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertLess(len(rows[0]['description']), 1000)

    def test_unindexed_strings_follow_the_actual_text_extraction_paths(self):
        content = {'content_type': 'text', 'text': 'shadowed', 'parts': [
            'used', {'text': 'used', 'transcript': 'unused'},
            {'transcript': 'serialized and used'}, ['converted and used']], 'language': 'metadata'}
        self.assertEqual(list(audit.unused_content_strings(content)), [
            (('text',), 'shadowed'), (('parts', 1, 'transcript'), 'unused'), (('language',), 'metadata')])
        content = {'content_type': 'thoughts', 'thoughts': [{'summary': 'invented summary', 'content': 'invented text'}]}
        self.assertEqual(len(list(audit.unused_content_strings(content))), 0)
        value = conversation()
        value['mapping']['invented-node']['message']['content'] = content
        result = self.scan([value])
        self.assertEqual(result['sources'][0].get('nodes_with_unindexed_text_like_fields_and_empty_search_text',0), 0)
        with (self.folder / 'report' / 'unindexed-content-fields.csv').open(newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 0)


if __name__ == '__main__':
    unittest.main()
