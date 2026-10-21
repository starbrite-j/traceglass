"""Isolated regression tests for bounded reading and explicit navigation scopes."""
import gzip
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import constellation


class LoadingNavigationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.addCleanup(setattr, app, 'DB_PATH', app.DB_PATH)
        app.DB_PATH = self.folder / 'test.sqlite3'
        app.init_db()
        with app.db() as conn:
            conn.execute("INSERT INTO datasets(id,name,input_json,created_at) VALUES(1,'test','[]','test')")

    def source(self, text, name='export.json'):
        path = self.folder / name
        path.write_text(text, encoding='utf-8')
        return path

    def conversation(self, count=1, text='camel DEADBEEF'):
        return {'id':'conversation-camel', 'title':'Test', 'mapping': {
            f'n{i}': {'id':f'n{i}', 'parent':f'n{i-1}' if i else None,
                'children':[f'n{i+1}'] if i+1<count else [], 'message': {
                    'id':f'm{i}', 'author':{'role':'user'}, 'create_time':1753768114+i,
                    'content':{'content_type':'text','parts':[text]},
                    'metadata':{'request_id':f'req_literal_%_{i}', 'flag':'100%_literal'}}}
            for i in range(count)}}

    def test_array_and_ndjson_and_gzip(self):
        values = [{'camel':'🐫'}, {'nested':[False, None, 12.5]}]
        for text in (json.dumps(values), '\n'.join(json.dumps(v, indent=2) for v in values)):
            self.assertEqual(list(app.iter_json_values(self.source(text))), values)
        self.assertEqual(list(app.iter_json_values(self.source('[]'))), [])
        path = self.folder/'compressed.json.gz'
        with gzip.open(path,'wt',encoding='utf-8') as f:
            json.dump(values,f)
        self.assertEqual(list(app.iter_json_values(path)),values)

    def test_malformed_separators_are_not_silently_accepted(self):
        for text in ('[{} {}]', '[{},]', '[{}', '[', '[]junk', '[{},,{}]', '{}{}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                list(app.iter_json_values(self.source(text)))

    def test_scalar_and_unicode_read_boundaries(self):
        class TinyReader(io.StringIO):
            def read(self, size=-1):
                return super().read(min(size,2))
        text='[1.25, 1e+12, -3.4e-2, true, null, {"x":"🐫"}]'
        with patch.object(app,'open_text',return_value=TinyReader(text)):
            self.assertEqual(list(app.iter_json_values(Path('unused'))),json.loads(text))

    def test_failed_refresh_preserves_old_index_even_after_1000_records(self):
        path = self.source(json.dumps([self.conversation()]))
        app.import_file(path,1)
        with app.db() as conn:
            before = tuple(conn.execute('SELECT sha256,row_count,status FROM sources').fetchone())
        # One thousand complete conversations followed by malformed input.
        replacements=[]
        for i in range(1001):
            conv=self.conversation(text='replacement');conv['id']=f'new-{i}'
            replacements.append(json.dumps(conv))
        path.write_text('['+','.join(replacements)+',BROKEN]')
        with self.assertRaises(ValueError):app.import_file(path,1)
        with app.db() as conn:
            self.assertEqual(tuple(conn.execute('SELECT sha256,row_count,status FROM sources').fetchone()),before)
            self.assertEqual(conn.execute('SELECT count(*) FROM messages').fetchone()[0],1)
            self.assertEqual(app.api_messages(conn,{'q':['camel']})['total'],1)
            self.assertEqual(app.api_messages(conn,{'q':['replacement']})['total'],0)

    def test_search_modes_and_clamped_pages(self):
        app.import_file(self.source(json.dumps([self.conversation(65)])),1)
        with app.db() as conn:
            def query(**kwargs):return app.api_messages(conn,{k:[str(v)] for k,v in kwargs.items()})
            d=query(q='conversation-camel',search_mode='ids')
            self.assertEqual(d['total'],65)
            self.assertIn('conversation.id',d['items'][0]['matching_paths'])
            self.assertEqual(query(q='n64',search_mode='ids')['total'],1)
            self.assertEqual(query(q='%_',search_mode='ids')['total'],65)
            self.assertEqual(query(q='NO%_',search_mode='ids')['total'],0)
            self.assertEqual(query(q='DEADBEEF',search_mode='text')['total'],65)
            self.assertEqual(query(q='DEADBEEF')['search_scope']['effective_mode'],'ids')
            self.assertEqual(query(q='100%_',search_mode='metadata')['total'],65)
            self.assertEqual(query(q='100X_',search_mode='metadata')['total'],0)
            self.assertEqual(query(page=999,page_size=20)['page'],4)
            self.assertEqual(len(query(page=999,page_size=20)['items']),5)
            self.assertEqual(query(q='absent',page=999)['page'],1)
            self.assertEqual(query(q='camel',source_id=999)['total'],0)
            with self.assertRaises(ValueError):query(search_mode='guessed')
            self.assertEqual(constellation.atlas(conn,{'dataset_id':['1'],'page':['999']})['page'],1)

    def test_import_slot_reserved_before_thread_runs(self):
        old=dict(app.JOB)
        self.addCleanup(app.JOB.update,old)
        app.JOB['state']='idle'
        with patch.object(app.threading,'Thread') as thread:
            self.assertTrue(app.start_import(['synthetic']))
            self.assertFalse(app.start_import(['synthetic']))
            thread.assert_called_once()
        app.JOB['state']='idle'
        with patch.object(app.threading,'Thread') as thread:
            thread.return_value.start.side_effect=RuntimeError('cannot start')
            with self.assertRaises(RuntimeError):app.start_import(['synthetic'])
            self.assertEqual(app.JOB['state'],'error')


if __name__=='__main__':unittest.main()
