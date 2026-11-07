#!/usr/bin/env python3
"""Verify the completed browser upload of a benchmark-generated source."""
import argparse
import json
import sqlite3
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path

from benchmark_large_import import ROOT, app, digest, request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--upload-started-utc', required=True, help='Observed browser LOAD click timestamp')
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    manifest = json.loads((workspace / 'manifest.json').read_text())
    automated = json.loads((workspace / 'report.json').read_text())
    if automated['status'] != 'passed':
        raise AssertionError('Run the automated acceptance checks successfully first')
    job = request(args.port, '/api/import/status')[0]['job']
    if job['state'] != 'complete':
        raise AssertionError('Browser import has not completed successfully')
    stats = request(args.port, '/api/stats')[0]
    if stats['totals']['messages'] != manifest['records'] or len(stats['sources']) != 1:
        raise AssertionError('Browser-loaded dataset has unexpected counts')
    selected_source = stats['sources'][0]
    copied = Path(selected_source['path'])
    copied.relative_to(workspace / 'state' / 'uploads')
    if copied.stat().st_size != manifest['bytes'] or app.sha256_file(copied) != manifest['sha256']:
        raise AssertionError('Uploaded copy differs from the generated source')
    if selected_source['sha256'] != manifest['sha256']:
        raise AssertionError('Indexed copy checksum differs from the source')
    with sqlite3.connect((workspace / 'state' / 'traceglass.sqlite3').as_uri() + '?mode=ro', uri=True) as conn:
        counts = {name: conn.execute(f'SELECT count(*) FROM {name}').fetchone()[0]
                  for name in ('sources', 'conversations', 'messages', 'messages_fts', 'edges')}
        expected = {'sources': 2, 'conversations': 2 * manifest['conversations'],
                    'messages': 2 * manifest['records'], 'messages_fts': 2 * manifest['records'],
                    'edges': 2 * manifest['edges']}
        if counts != expected:
            raise AssertionError(f'Copy retention differs: {counts} != {expected}')
        for sample in manifest['samples']:
            row = conn.execute('SELECT raw_json FROM messages WHERE source_id=? AND conversation_id=? AND node_id=?',
                               (selected_source['id'], sample['conversation_id'], sample['node_id'])).fetchone()
            if not row or digest(json.loads(row[0])) != sample['raw_sha256']:
                raise AssertionError('Uploaded raw sample changed')
    sample = manifest['samples'][-1]
    found = request(args.port, f"/api/messages?dataset_id={stats['active_dataset_id']}&search_mode=text&q={sample['marker']}")[0]
    if found['total'] != 1 or found['items'][0]['node_id'] != sample['node_id']:
        raise AssertionError('Final-record lookup failed in the browser-loaded dataset')
    started = datetime.fromisoformat(job['started_at'].replace('Z', '+00:00'))
    finished = datetime.fromisoformat(job['finished_at'].replace('Z', '+00:00'))
    clicked = datetime.fromisoformat(args.upload_started_utc.replace('Z', '+00:00'))
    polls, uploads = [], []
    for line in (workspace / 'http.log').read_text().splitlines():
        timestamp, detail = line.split(' ', 1)
        instant = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        if instant >= clicked and 'POST /api/upload?' in detail and ' 201 ' in detail:
            uploads.append(timestamp)
        if started <= instant <= finished and 'GET /api/import/status ' in detail:
            polls.append(instant)
    if len(uploads) != 1 or not polls:
        raise AssertionError('Expected browser upload and progress requests were not recorded')
    per_second = Counter(int(instant.timestamp()) for instant in polls)
    if max(per_second.values()) > 3:
        raise AssertionError('Progress requests multiplied instead of remaining bounded')
    report = {
        'status': 'passed', 'app_sha256': app.sha256_file(ROOT / 'app.py'),
        'upload_started_utc': args.upload_started_utc, 'upload_response_utc': uploads[0],
        'index_started_utc': job['started_at'], 'index_finished_utc': job['finished_at'],
        'index_seconds': (finished - started).total_seconds(),
        'click_to_finished_seconds': (finished - clicked).total_seconds(),
        'copied_bytes': copied.stat().st_size, 'copied_sha256': manifest['sha256'],
        'indexed_records_in_selected_dataset': stats['totals']['messages'],
        'database_counts_after_two_copies': counts,
        'sampled_uploaded_records_verified': len(manifest['samples']),
        'prior_copy_retained': True,
        'progress_poll_requests': len(polls), 'max_progress_requests_in_one_second': max(per_second.values()),
        'median_progress_poll_interval_seconds': statistics.median((b-a).total_seconds() for a,b in zip(polls,polls[1:])),
        'scope': 'Actual native browser file picker, file upload, asynchronous import, source-copy hashes, retained copies, and tail-record search.'
    }
    if report['app_sha256'] != automated['app_sha256']:
        raise AssertionError('Application code changed since the automated acceptance run')
    (workspace / 'browser-upload-report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
