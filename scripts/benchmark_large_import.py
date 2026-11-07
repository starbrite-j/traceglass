#!/usr/bin/env python3
"""Generate an invented corpus, exercise real ingestion/HTTP APIs, and keep evidence.

Nothing is mocked. A new workspace is required so personal indexes cannot be used.
The generated corpus is text-heavy, not an attachment or universal-schema benchmark.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import platform
import random
import resource
import sqlite3
import statistics
import sys
import threading
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def digest(value):
    return hashlib.sha256(compact(value).encode('utf-8')).hexdigest()


def peak_rss():
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == 'darwin' else value * 1024)


def emit(event, **fields):
    print(json.dumps({'event': event, **fields}, ensure_ascii=False), flush=True)


def generate(path, size_bytes, seed, nodes_per_conversation=16):
    rng = random.Random(seed)
    words = ('analysis source record context message timeline branch archive search '
             'evidence field origin ledger version reader sequence index parent child '
             'window graph question method detail sample timestamp unicode cafe café '
             'résumé 日本語 boundary observation inspect compare preserve verify').split()
    paragraphs = [' '.join(rng.choices(words, k=80)) + '. ' for _ in range(1024)]
    lengths = (96, 256, 1024, 2048, 4096, 8192, 16384, 65536)
    count = conversations = written = 0
    samples = []
    checksum = hashlib.sha256()
    started = time.perf_counter()
    with path.open('xb') as stream:
        def write(data):
            nonlocal written
            stream.write(data)
            checksum.update(data)
            written += len(data)
        write(b'[\n')
        while written < size_bytes:
            cid = f'bench-c{conversations:08d}'
            mapping = {}
            for offset in range(nodes_per_conversation):
                nid = f'{cid}-n{offset:02d}'
                marker = f'benchmarker{count:012d}'
                target = lengths[count % len(lengths)]
                text = f'{marker} Invented record {count}. '
                while len(text) < target:
                    text += paragraphs[rng.randrange(len(paragraphs))]
                text = text[:target]
                node = {
                    'id': nid, 'parent': f'{cid}-n{offset-1:02d}' if offset else None,
                    'children': [f'{cid}-n{offset+1:02d}'] if offset + 1 < nodes_per_conversation else [],
                    'message': {
                        'id': nid, 'author': {'role': 'user' if offset % 2 == 0 else 'assistant'},
                        'create_time': 1750000000 + count,
                        'content': {'content_type': 'text', 'parts': [text]},
                        'metadata': {'request_id': f'bench-request-{count:012d}',
                                     'topic': count % 17, 'synthetic': True},
                        'status': 'finished_successfully', 'recipient': 'all',
                    },
                }
                mapping[nid] = node
                sample = {'conversation_id': cid, 'node_id': nid, 'marker': marker,
                          'raw_sha256': digest(node), 'ordinal': count}
                if count % 10000 == 0:
                    samples.append(sample)
                last_sample = sample
                count += 1
            conversation = {
                'id': cid, 'title': f'Invented benchmark conversation {conversations}',
                'create_time': 1750000000 + count - nodes_per_conversation,
                'update_time': 1750000000 + count - 1,
                'current_node': f'{cid}-n{nodes_per_conversation-1:02d}', 'mapping': mapping,
            }
            if conversations:
                write(b',\n')
            write(compact(conversation).encode('utf-8'))
            conversations += 1
            if conversations % 1024 == 0:
                emit('generating', bytes=written, records=count)
        write(b'\n]\n')
    if last_sample not in samples:
        samples.append(last_sample)
    return {'bytes': written, 'sha256': checksum.hexdigest(), 'records': count,
            'conversations': conversations, 'edges': 2 * (count - conversations),
            'samples': samples, 'seed': seed, 'nodes_per_conversation': nodes_per_conversation,
            'generation_seconds': time.perf_counter() - started,
            'shape': f'{nodes_per_conversation}-node conversations; varied 96–65536 character text; multilingual vocabulary; unique IDs/markers'}


def request(port, path, timeout=120):
    client = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    try:
        start = time.perf_counter()
        client.request('GET', path)
        response = client.getresponse()
        raw = response.read()
        elapsed = time.perf_counter() - start
        if response.status != 200:
            raise RuntimeError(f'{path}: HTTP {response.status}: {raw[:300]!r}')
        def invalid(value):
            raise ValueError(f'Invalid JSON response: {value}')
        return json.loads(raw, parse_constant=invalid), elapsed, len(raw)
    finally:
        client.close()


def validate(port, manifest, dataset_id, max_http_seconds=None):
    checks, timings = [], {}
    def measure(name, path, predicate):
        observations, sizes = [], []
        for _ in range(3):
            data, seconds, size = request(port, path)
            if not predicate(data):
                raise AssertionError(f'{name} returned unexpected data')
            observations.append(seconds)
            sizes.append(size)
        timings[name] = {'seconds': observations, 'median_seconds': statistics.median(observations),
                         'max_seconds': max(observations), 'max_response_bytes': max(sizes)}
        checks.append(name)
        emit('check', name=name, **timings[name])
        if max_http_seconds is not None and max(observations) > max_http_seconds:
            raise AssertionError(f'{name} exceeded the {max_http_seconds}s HTTP response budget')
        return data
    def route(path, **query):
        return path + '?' + urllib.parse.urlencode(query)
    stats = measure('stats', route('/api/stats', dataset_id=dataset_id),
                    lambda d: d['totals']['messages'] == manifest['records'])
    sid = stats['sources'][0]['id']
    measure('ledger_first_page', route('/api/messages', dataset_id=dataset_id, page_size=100),
            lambda d: d['total'] == manifest['records'] and len(d['items']) == min(100, manifest['records']))
    measure('ledger_last_page', route('/api/messages', dataset_id=dataset_id, page=999999, page_size=100),
            lambda d: d['page'] == d['pages'] and 0 < len(d['items']) <= 100)
    for label, sample in (('first', manifest['samples'][0]), ('last', manifest['samples'][-1])):
        found = measure(f'text_search_{label}', route('/api/messages', dataset_id=dataset_id,
                        search_mode='text', q=sample['marker']),
                        lambda d, s=sample: d['total'] == 1 and d['items'][0]['node_id'] == s['node_id'])
        pk = found['items'][0]['pk']
        measure(f'record_detail_{label}', f'/api/messages/{pk}',
                lambda d, s=sample: digest(d['raw']) == s['raw_sha256'])
    sample = manifest['samples'][-1]
    measure('id_search', route('/api/messages', dataset_id=dataset_id, search_mode='ids', q=sample['node_id']),
            lambda d: d['total'] == 1 and d['items'][0]['node_id'] == sample['node_id'])
    measure('exact_metadata_filter', route('/api/messages', dataset_id=dataset_id, meta_path='topic', meta_value='3'),
            lambda d: d['total'] == (manifest['records'] + 13) // 17)
    measure('atlas', route('/api/atlas', dataset_id=dataset_id),
            lambda d: d['total'] == manifest['conversations'] and 0 < len(d['items']) <= 60)
    measure('basin_overview', route('/api/basins', dataset_id=dataset_id),
            lambda d: d['total'] == manifest['conversations'] and 0 < len(d['nodes']) <= 300)
    measure('connected_neighborhood', route('/api/neighborhood', pk=pk,limit=100,depth=4),
            lambda d: 0 < len(d['nodes']) <= 100 and all(n['source_id']==sid for n in d['nodes']))
    measure('constellation', route('/api/constellation', source_id=sid,
            conversation_id=sample['conversation_id'], focus=sample['node_id'], limit=50),
            lambda d: len(d['nodes']) == min(50, manifest['nodes_per_conversation']) and d['total_active'] == manifest['nodes_per_conversation'])
    measure('parent_trace', route('/api/parent-trace', source_id=sid,
            conversation_id=sample['conversation_id'], node_id=sample['node_id']),
            lambda d: d['outer']['stop']['reason'] == ('null_parent' if manifest['nodes_per_conversation'] <= 500 else 'limit'))
    measure('raw_window', route(f'/api/sources/{sid}/raw', offset=max(0, manifest['bytes'] - 8192), length=4096),
            lambda d: len(d['content'].encode('utf-8')) <= 4102)
    measure('raw_search_full_file', route(f'/api/sources/{sid}/raw', q=sample['marker']),
            lambda d: d['match_count'] == 1 and sample['marker'] in d['content'])
    return {'checks': checks, 'http_timings': timings, 'source_id': sid}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True, help='New, disposable directory; never an existing index')
    parser.add_argument('--size-mib', type=int, default=64)
    parser.add_argument('--seed', type=int, default=20261106)
    parser.add_argument('--nodes-per-conversation', type=int, default=16)
    parser.add_argument('--serve-port', type=int, default=0, help='Keep the validated server available for manual browser checks')
    parser.add_argument('--max-rss-mib', type=float)
    parser.add_argument('--max-import-seconds', type=float)
    parser.add_argument('--max-http-seconds', type=float)
    args = parser.parse_args()
    if not 1 <= args.size_mib <= 20480:
        parser.error('--size-mib must be 1–20480')
    if not 1 <= args.nodes_per_conversation <= 100000:
        parser.error('--nodes-per-conversation must be 1–100000')
    if any(value is not None and value <= 0 for value in (args.max_rss_mib, args.max_import_seconds, args.max_http_seconds)):
        parser.error('Acceptance budgets must be positive')
    workspace = args.workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=False)
    app.DATA = workspace / 'state'
    app.DB_PATH = app.DATA / 'traceglass.sqlite3'
    source = workspace / 'invented-conversations.json'
    report_path = workspace / 'report.json'
    report = {'started_utc': app.utc_now(), 'status': 'running', 'platform': platform.platform(),
              'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
              'cpu': platform.processor(), 'app_sha256': app.sha256_file(ROOT / 'app.py'),
              'relationships_sha256': app.sha256_file(ROOT / 'relationships.py'), 'implementation_sha256':{name:app.sha256_file(ROOT/name) for name in ('app.py','relationships.py','streaming.py','local_http.py','operations.py','constellation.py','investigations.py','static/app.js','static/constellation.js','static/constellation-worker.js')},
              'benchmark_sha256': app.sha256_file(Path(__file__)), 'target_mib': args.size_mib,
              'acceptance_limits': {'max_rss_mib': args.max_rss_mib, 'max_import_seconds': args.max_import_seconds,
                                    'max_http_seconds': args.max_http_seconds, 'live_probe_timeout_seconds': 3},
              'scope': 'Local synthetic text-heavy workload, not real private exports or all hardware/schema combinations.'}
    report_path.write_text(json.dumps(report, indent=2))
    server = None
    stop = threading.Event()
    try:
        report['manifest'] = manifest = generate(source, args.size_mib * 1024**2, args.seed, args.nodes_per_conversation)
        (workspace / 'manifest.json').write_text(json.dumps(manifest, indent=2))
        emit('generated', **{k: v for k, v in manifest.items() if k != 'samples'})
        app.init_db()
        trace_lock = threading.Lock()
        class EvidenceHandler(app.Handler):
            def log_message(self, fmt, *arguments):
                with trace_lock, (workspace / 'http.log').open('a', encoding='utf-8') as log:
                    log.write(app.utc_now() + ' ' + (fmt % arguments) + '\n')
                super().log_message(fmt, *arguments)
        server = app.ThreadingHTTPServer(('127.0.0.1', args.serve_port), EvidenceHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_port
        request(port, '/api/health')
        emit('ready', port=port)
        phases, live = [], []
        for name in ('import_file', 'analyze_dataset', 'analyze_cross_dataset_conflicts'):
            original = getattr(app, name)
            def measured(*arguments, _original=original, _name=name, **keywords):
                began = time.perf_counter()
                emit('phase_start', phase=_name)
                try:
                    return _original(*arguments, **keywords)
                finally:
                    phase = {'phase': _name, 'seconds': time.perf_counter() - began, 'peak_rss_bytes': peak_rss()}
                    phases.append(phase)
                    emit('phase_end', **phase)
            setattr(app, name, measured)
        def monitor():
            while not stop.wait(5):
                observation = {'job': dict(app.JOB), 'peak_rss_bytes': peak_rss(),
                               'database_bytes': app.DB_PATH.stat().st_size if app.DB_PATH.exists() else 0,
                               'wal_bytes': Path(str(app.DB_PATH) + '-wal').stat().st_size if Path(str(app.DB_PATH) + '-wal').exists() else 0}
                for name, endpoint in (('health', '/api/health'), ('status', '/api/import/status'), ('stats', '/api/stats')):
                    try:
                        _, duration, _ = request(port, endpoint, timeout=3)
                        observation[name + '_seconds'] = duration
                    except Exception as exc:
                        observation[name + '_error'] = f'{type(exc).__name__}: {exc}'
                live.append(observation)
                emit('progress', **observation)
        watcher = threading.Thread(target=monitor, daemon=True)
        watcher.start()
        began = time.perf_counter()
        imported = app.import_paths([source])
        report['import_seconds'] = time.perf_counter() - began
        report['import_peak_rss_bytes'] = peak_rss()
        stop.set()
        watcher.join(timeout=10)
        report.update(import_result=imported, phases=phases, live_import_probes=live)
        if args.max_rss_mib is not None and report['import_peak_rss_bytes'] > args.max_rss_mib * 1024**2:
            raise AssertionError('Process peak RSS exceeded the configured memory budget')
        if args.max_import_seconds is not None and report['import_seconds'] > args.max_import_seconds:
            raise AssertionError('Import exceeded the configured time budget')
        if imported['rows'] != manifest['records']:
            raise AssertionError('Import row count differs from generated manifest')
        with app.db() as conn:
            counts = {table: conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                      for table in ('sources', 'conversations', 'messages', 'messages_fts', 'edges')}
            expected = {'sources': 1, 'conversations': manifest['conversations'], 'messages': manifest['records'],
                        'messages_fts': manifest['records'], 'edges': manifest['edges']}
            if counts != expected:
                raise AssertionError(f'Index counts differ: {counts} != {expected}')
            for sample in manifest['samples']:
                raw = conn.execute('SELECT raw_json FROM messages WHERE conversation_id=? AND node_id=?',
                                   (sample['conversation_id'], sample['node_id'])).fetchone()[0]
                if digest(json.loads(raw)) != sample['raw_sha256']:
                    raise AssertionError('Indexed raw record differs from generated source record')
            if conn.execute('PRAGMA quick_check').fetchall()[0][0] != 'ok':
                raise AssertionError('SQLite quick_check failed')
            conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('integrity-check')")
            stored_sha = conn.execute('SELECT sha256 FROM sources').fetchone()[0]
        if stored_sha != manifest['sha256'] or app.sha256_file(source) != manifest['sha256']:
            raise AssertionError('Source checksum changed or import checksum differs')
        report.update(index_counts=counts, sampled_raw_records_verified=len(manifest['samples']),
                      source_unchanged=True, sqlite_quick_check='ok', fts_integrity_check='ok')
        report.update(validate(port, manifest, imported['dataset_id'], args.max_http_seconds))
        # Reopening the production schema initialization must retain counts and provenance.
        app.init_db()
        persisted, _, _ = request(port, '/api/stats')
        if persisted['totals']['messages'] != manifest['records']:
            raise AssertionError('Reopening the index changed the record count')
        report['reopen_check'] = 'ok'
        report['database_bytes'] = app.DB_PATH.stat().st_size
        report['peak_rss_bytes'] = peak_rss()
        report['status'] = 'passed'
        if any('health_error' in probe or 'status_error' in probe or 'stats_error' in probe for probe in live):
            report['status'] = 'failed'
            report['error'] = 'An HTTP responsiveness probe failed during import; inspect live_import_probes'
    except Exception as exc:
        report.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        stop.set()
        report['finished_utc'] = app.utc_now()
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
        emit('result', status=report['status'], report=str(report_path), error=report.get('error'))
        if server and not args.serve_port:
            server.shutdown()
            server.server_close()
    if args.serve_port:
        emit('browser_ready', url=f'http://127.0.0.1:{server.server_port}', report=str(report_path))
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            server.shutdown()
            server.server_close()
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
