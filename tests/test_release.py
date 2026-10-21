"""Release boundaries, using only invented inputs and temporary storage."""
import http.client
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        for key in ('DATA', 'DB_PATH'):
            self.addCleanup(setattr, app, key, getattr(app, key))
        old_job = dict(app.JOB)
        self.addCleanup(app.JOB.update, old_job)
        app.DATA = self.folder / 'data'
        app.DB_PATH = app.DATA / 'test.sqlite3'
        app.init_db()
        with app.db() as conn:
            conn.execute("INSERT INTO datasets(id,name,input_json,created_at) VALUES(1,'test','[]','test')")

    def source(self, text, name='source.json'):
        path = self.folder / name
        path.write_text(text, encoding='utf-8')
        return path

    def demo(self):
        return json.loads((app.SAMPLE_DIR / 'demo.json').read_text(encoding='utf-8'))

    def test_duplicate_keys_and_invalid_utf8_fail_without_data_loss(self):
        for text in ('[{"id":"a","id":"b"}]', '[{"metadata":{"hidden":true,"hidden":false}}]'):
            with self.assertRaisesRegex(ValueError, 'Duplicate JSON key'):
                list(app.iter_json_values(self.source(text)))
        path = self.folder / 'invalid.json'
        path.write_bytes(b'{"text":"\xff"}')
        with self.assertRaises(UnicodeDecodeError):
            list(app.iter_json_values(path))

    def test_identity_collisions_rollback_instead_of_overwrite(self):
        demo = self.demo()
        for bad in (demo + demo,):
            with self.assertRaises(sqlite3.IntegrityError):
                app.import_file(self.source(json.dumps(bad)), 1)
        demo[0]['mapping']['demo-user']['id'] = 'different-embedded-id'
        with self.assertRaisesRegex(ValueError, 'differs from embedded'):
            app.import_file(self.source(json.dumps(demo)), 1)
        with app.db() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM messages').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT count(*) FROM sources').fetchone()[0], 0)

    def test_same_path_replacement_rejected_and_raw_window_is_not_a_snapshot(self):
        path = self.source(json.dumps(self.demo()))
        app.import_file(path, 1)
        path.write_text('[]')
        with self.assertRaisesRegex(ValueError, 'already indexed'):
            app.import_file(path, 1)
        with app.db() as conn:
            sid = conn.execute('SELECT id FROM sources').fetchone()[0]
            raw = app.api_source_raw(conn, sid, {})
            self.assertFalse(raw['source']['current_bytes_verified'])
            self.assertTrue(raw['source']['imported_sha256'])
            self.assertIn('NOT been reverified', raw['scope_note'])
            self.assertGreater(conn.execute('SELECT count(*) FROM messages').fetchone()[0], 0)

    def test_file_upload_is_bounded_unique_and_incomplete_copies_are_removed(self):
        class LimitedReader(io.BytesIO):
            def read(self, size=-1):
                if not 0 < size <= 1024**2:
                    raise AssertionError('unbounded read')
                return super().read(size)
        payload = b' ' * (2 * 1024**2) + b'[]'
        first = app.save_upload(LimitedReader(payload), 'export.json', len(payload))
        second = app.save_upload(io.BytesIO(b'[]'), 'export.json', 2)
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), payload)
        before = set((app.DATA / 'uploads').iterdir())
        with self.assertRaisesRegex(ValueError, 'ended early'):
            app.save_upload(io.BytesIO(b'['), 'export.json', 10)
        self.assertEqual(set((app.DATA / 'uploads').iterdir()), before)
        for name, size in (('../escape.json',2), ('folder\\escape.json',2), ('export.zip',2), ('a.json',0), ('a.json',app.MAX_UPLOAD_BYTES+1)):
            with self.assertRaises(ValueError):
                app.save_upload(io.BytesIO(b'[]'), name, size)

    def test_upload_http_boundaries_and_import(self):
        server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        def request(body, headers):
            client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            try:
                client.request('POST', '/api/upload?name=demo.json', body, headers)
                response = client.getresponse()
                return response.status, json.loads(response.read())
            finally:
                client.close()
        payload = json.dumps(self.demo()).encode()
        self.assertEqual(request(payload, {'Content-Type':'text/plain'})[0], 415)
        self.assertEqual(request(payload, {'Content-Type':'application/octet-stream','Origin':'https://attacker.test'})[0], 403)
        self.assertEqual(request(payload, {'Content-Type':'application/octet-stream','Host':'attacker.test'})[0], 403)
        status, result = request(payload, {'Content-Type':'application/octet-stream'})
        self.assertEqual(status, 201)
        self.assertTrue(result['stored_locally'])
        self.assertGreater(app.import_file(Path(result['path']), 1), 0)

    def test_import_discovery_failure_releases_busy_state(self):
        app.JOB['state'] = 'idle'
        with patch.object(app.threading, 'Thread') as worker, patch.object(app.traceback, 'print_exc'):
            self.assertTrue(app.start_import([str(self.folder / 'does-not-exist.json')]))
            worker.call_args.kwargs['target']()
        self.assertEqual(app.JOB['state'], 'error')
        self.assertIn('No supported', app.JOB['error'])


if __name__ == '__main__':
    unittest.main()
