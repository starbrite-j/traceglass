#!/usr/bin/env python3
"""List JSON items the current importer does not index, without changing sources.

Reports are private local artifacts. Recognition is not proof of successful import;
use --database to compare recognized identities against an existing index.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app

WRAPPERS = ('conversations', 'items', 'data')
SUFFIXES = ('.json', '.jsonl', '.ndjson', '.json.gz', '.jsonl.gz', '.ndjson.gz')
TEXT_KEYS = {'text', 'content', 'transcript', 'transcription', 'summary', 'result',
             'title', 'description', 'output', 'message', 'error'}


def json_path(parent, key):
    return parent + '[' + json.dumps(key, ensure_ascii=False) + ']'


def safe_csv(row):
    return {key: ("'" + value if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')) else value)
            for key, value in row.items()}


def unused_content_strings(content):
    """Yield nonempty string leaves not consumed by flatten_content.

    Exclude the content_type discriminator. Include other format metadata and
    references, explicitly distinguished from fields with text-like key names.
    This does not claim that every returned string is human message text.
    """
    used = set()
    if content.get('content_type') == 'user_editable_context':
        used.update((key,) for key in ('user_profile', 'user_instructions') if isinstance(content.get(key), str))
    elif content.get('content_type') == 'thoughts' and isinstance(content.get('thoughts'), list):
        for index, thought in enumerate(content['thoughts']):
            if isinstance(thought, dict):
                used.update(('thoughts', index, field) for field in ('summary','content') if isinstance(thought.get(field), str))
    elif content.get('content_type') in ('reasoning_recap','tether_browsing_display'):
        used.update((field,) for field in (('content',) if content['content_type']=='reasoning_recap' else ('result','summary'))
                    if isinstance(content.get(field), str))
    elif isinstance(content.get('parts'), list):
        for index, part in enumerate(content['parts']):
            prefix = ('parts', index)
            if isinstance(part, dict) and isinstance(part.get('text'), str):
                used.add(prefix + ('text',))
            else:
                # Other parts are serialized/converted as a whole by flatten_content.
                used.add(prefix)
    elif isinstance(content.get('text'), str):
        used.add(('text',))

    def walk(value, path):
        if path in used or path == ('content_type',):
            return
        if isinstance(value, str) and value:
            yield path, value
        elif isinstance(value, dict):
            for key, child in value.items():
                yield from walk(child, path + (key,))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                yield from walk(child, path + (index,))
    yield from walk(content, ())


def describe(value):
    """Bound previews before serialization, even for multi-gigabyte values."""
    result = {'type': type(value).__name__}
    if isinstance(value, dict):
        result['keys'] = list(value)[:30]
        result['key_count'] = len(value)
        for key in ('id', 'conversation_id', 'role', 'content_type'):
            child = value.get(key)
            if type(child) in (str, int, float, bool) or child is None and key in value:
                result[key] = str(child)[:160]
    elif isinstance(value, list):
        result['length'] = len(value)
    elif isinstance(value, str):
        result.update(length=len(value), preview=value[:240])
    else:
        result['preview'] = str(value)[:240]
    return result


def walk_records(value, path, emit):
    """Mirror normalized_records; report boundaries of unvisited subtrees.

    A reported container may contain many items. Counts are audit entries, not
    an invented count of messages inside an unsupported schema.
    """
    if not isinstance(value, dict):
        emit('excluded', path, 'unsupported_record_type',
             'Expected a conversation or chronological-record object; this entire value is not visited.', value)
        return
    if isinstance(value.get('mapping'), dict):
        yield {'kind': 'conversation', 'value': value, 'path': path}
        for key in WRAPPERS:
            if isinstance(value.get(key), list):
                emit('excluded', json_path(path, key), 'shadowed_container',
                     'The conversation mapping takes precedence; this entire sibling list is not visited.', value[key])
        return
    if value.get('reader_schema') and isinstance(value.get('mapping_node'), dict):
        yield {'kind': 'chronological', 'value': value, 'path': path}
        for key in WRAPPERS:
            if isinstance(value.get(key), list):
                emit('excluded', json_path(path, key), 'shadowed_container',
                     'The chronological record takes precedence; this entire sibling list is not visited.', value[key])
        return
    chosen = next((key for key in WRAPPERS if isinstance(value.get(key), list)), None)
    if chosen is None:
        emit('excluded', path, 'unrecognized_record_shape',
             'No object-valued mapping, supported chronological record, or conversations/items/data list was recognized. The entire object is not visited.', value)
        return
    for key in WRAPPERS:
        if key != chosen and isinstance(value.get(key), list):
            emit('excluded', json_path(path, key), 'shadowed_container',
                 f'The importer follows {chosen!r} first; this entire sibling list is not visited.', value[key])
    for index, child in enumerate(value[chosen]):
        yield from walk_records(child, json_path(path, chosen) + f'[{index}]', emit)


def inspect_node(node, path, emit):
    """Report selected extraction limits; do not label metadata as deleted."""
    msg = app.object_field(node, 'message', 'node')
    app.object_field(msg, 'author', 'node.message')
    metadata = app.object_field(msg, 'metadata', 'node.message')
    app.object_field(metadata, 'finish_details', 'node.message.metadata')
    content = app.object_field(msg, 'content', 'node.message')
    msg_path = json_path(path, 'message')
    if content and not app.flatten_content(content):
        emit('warning', json_path(msg_path, 'content'), 'no_searchable_message_text',
             'This content is retained in raw JSON but produces no normal message-text search entry.', content)
    text_fields = {'content_type', 'parts', 'text'}
    if content.get('content_type') == 'user_editable_context':
        text_fields = {'content_type', 'user_profile', 'user_instructions'}
    elif content.get('content_type') == 'thoughts':
        text_fields = {'content_type','thoughts'}
    elif content.get('content_type') == 'reasoning_recap':
        text_fields = {'content_type','content'}
    elif content.get('content_type') == 'tether_browsing_display':
        text_fields = {'content_type','result','summary'}
    for key, child in content.items():
        if key not in text_fields and isinstance(child, (str, list, dict)) and child:
            emit('warning', json_path(json_path(msg_path, 'content'), key), 'content_field_not_used_for_text',
                 'This field is retained in raw JSON but is not used by the normal message-text extractor. Asset references are not decoded.', child)
    if 'create_time' in msg and msg['create_time'] is not None:
        _, display, classification = app.normalized_time(msg['create_time'])
        if display is None:
            emit('warning', json_path(msg_path, 'create_time'), 'timestamp_not_used_for_date_filters',
                 f'The record remains eligible for indexing, but this timestamp has no sortable display date ({classification}).', msg['create_time'])
    return msg


def audit(inputs, output, database=None, source_id=None):
    if source_id is not None and (database is None or len(inputs) != 1 or not Path(inputs[0]).is_file()):
        raise ValueError('--source-id requires --database and exactly one input file')
    # Do not traverse our own newly created reports if auditing a parent folder.
    output = Path(output).expanduser().resolve()
    paths, ignored = [], []
    seen = set()
    for raw in inputs:
        path = Path(raw).expanduser().resolve()
        if output == path or path in output.parents:
            raise ValueError('Report directory must be outside every input directory/file')
        candidates = sorted(path.rglob('*')) if path.is_dir() else [path]
        for candidate in candidates:
            if candidate in seen or candidate.is_dir():
                continue
            seen.add(candidate)
            if not candidate.exists():
                ignored.append((candidate, 'missing_input', 'This input does not exist.'))
            elif path.is_dir() and not candidate.name.lower().endswith(SUFFIXES):
                ignored.append((candidate, 'unsupported_file_extension', 'Directory discovery does not select this file extension; extract ZIP archives separately.'))
            else:
                paths.append(candidate)
    connection = None
    if database is not None:
        connection = sqlite3.connect(Path(database).expanduser().resolve().as_uri() + '?mode=ro', uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')  # One consistent read snapshot throughout the audit.
    output.mkdir(parents=True, exist_ok=False)
    summary = {
        'audit_version': 1, 'started_utc': app.utc_now(), 'status': 'running',
        'app_sha256': app.sha256_file(ROOT / 'app.py'),
        'audit_sha256': app.sha256_file(Path(__file__)),
        'database': str(Path(database).expanduser().resolve()) if database else None,
        'counts': {}, 'sources': [],
        'scope': 'Read-only source audit of current importer recognition, selected text/date extraction limits, and optional indexed identities. Not an exhaustive field-by-field diff or media decoder. Excluded containers are listed as subtrees, not counted as individual messages.',
        'notes': [
            'Recognized nodes are candidates, not proof that a file imported successfully.',
            'With --database, identity membership is checked only when the full source checksum matches the indexed source.',
            'Raw windows read the current source file; source files are never modified by this audit.',
            'Previews are bounded. JSON paths and top-level value indexes locate full content in the source.',
            'Reports may contain private paths, IDs, keys, and short previews; do not publish them without review.',
        ],
    }
    counts = Counter()
    current_source = None
    current_top = None
    try:
        with (output / 'excluded-items.csv').open('w', encoding='utf-8', newline='') as excluded, \
             (output / 'extraction-warnings.csv').open('w', encoding='utf-8', newline='') as warnings, \
             (output / 'unindexed-content-fields.csv').open('w', encoding='utf-8', newline='') as content_fields, \
             (output / 'items.jsonl').open('w', encoding='utf-8') as details:
            fields = ['source', 'top_level_value', 'json_path', 'conversation_id', 'conversation_title',
                      'node_id', 'role', 'content_type', 'reason', 'explanation', 'description']
            writers = {kind: csv.DictWriter(stream, fieldnames=fields) for kind, stream in [('excluded', excluded), ('warning', warnings)]}
            for writer in writers.values():
                writer.writeheader()
            content_writer = csv.DictWriter(content_fields, fieldnames=[
                'source', 'top_level_value', 'json_path', 'conversation_id', 'conversation_title',
                'node_id', 'role', 'content_type', 'classification', 'characters',
                'normal_message_text_empty', 'field_sha256', 'preview'])
            content_writer.writeheader()

            def emit(kind, path, reason, explanation, value=None, context=None):
                counts[kind + '_entries'] += 1
                counts[reason] += 1
                row = {'source': str(current_source), 'top_level_value': current_top,
                       'json_path': path, 'reason': reason, 'explanation': explanation,
                       'description': describe(value), **(context or {})}
                details.write(json.dumps(dict(row, category=kind), ensure_ascii=False) + '\n')
                row['description'] = json.dumps(row['description'], ensure_ascii=False)
                # CSV formulas must not execute if a source supplies a hostile key/ID.
                writers[kind].writerow(safe_csv(row))

            for path, reason, explanation in ignored:
                current_source, current_top = path, None
                emit('excluded', '$', reason, explanation)
            for path in paths:
                current_source, current_top = path, None
                info = {'path': str(path), 'recognized_conversations': 0, 'recognized_nodes': 0,
                        'indexed_nodes_verified': 0, 'recognized_nodes_absent_from_index': 0,
                        'status': 'scanning'}
                summary['sources'].append(info)
                before = None
                try:
                    before = app.sha256_file(path)
                    info['sha256_before'] = before
                    indexed_source = None
                    if connection is not None:
                        indexed_source = connection.execute('SELECT id,path,sha256,row_count,status FROM sources WHERE ' + ('id=?' if source_id is not None else 'path=?'),
                                                            (source_id if source_id is not None else str(path),)).fetchone()
                        if indexed_source is None and source_id is None:
                            # Direct import_file callers may store /var versus /private/var
                            # aliases. Match the same canonical path, never a same-name file.
                            aliases = [row for row in connection.execute('SELECT id,path,sha256,row_count,status FROM sources')
                                       if Path(row['path']).expanduser().resolve() == path]
                            if len(aliases) == 1:
                                indexed_source = aliases[0]
                        if indexed_source is None:
                            emit('warning', '$', 'source_not_in_selected_index', 'No source entry matches this path. Recognition is audited, but actual indexed membership cannot be established; uploads may have a different path. Use --source-id to select a known copy.')
                        else:
                            info['indexed_source'] = dict(indexed_source)
                            info['source_matches_index'] = before == indexed_source['sha256']
                            if not info['source_matches_index']:
                                emit('warning', '$', 'source_checksum_mismatch', 'Current source bytes do not match the indexed checksum. Indexed membership comparisons are disabled.')
                                indexed_source = None
                    for current_top, obj in enumerate(app.iter_json_values(path)):
                        # $ is relative to this decoded top-level value, including array entries.
                        for record in walk_records(obj, '$', emit):
                            value, location = record['value'], record['path']
                            if record['kind'] == 'conversation':
                                cid = str(value.get('id') or value.get('conversation_id') or f"conversation-{info['recognized_nodes']}")
                                info['recognized_conversations'] += 1
                                if not value['mapping']:
                                    emit('warning', json_path(location, 'mapping'), 'empty_mapping', 'This conversation has no mapping nodes and contributes no ledger rows.', value['mapping'])
                                nodes = ((nid, node, json_path(json_path(location, 'mapping'), nid)) for nid, node in value['mapping'].items())
                            else:
                                conv = app.object_field(value, 'conversation', 'record')
                                cid = str(conv.get('conversation_id') or 'unknown')
                                node = dict(value['mapping_node'])
                                node['message'] = value.get('message')
                                nodes = [(None, node, json_path(location, 'mapping_node'))]
                            for nid, node, node_path in nodes:
                                if not isinstance(node, dict):
                                    raise ValueError(f'{node_path} must be a JSON object')
                                if nid is not None and node.get('id') is not None and node['id'] != nid:
                                    raise ValueError(f'{node_path} mapping key differs from embedded node.id')
                                # Chronological message fields live beside mapping_node in the source.
                                msg_path = location if record['kind'] == 'chronological' else node_path
                                title = value.get('title') if record['kind'] == 'conversation' else conv.get('title')
                                context = {'conversation_id': cid, 'conversation_title': str(title or 'Untitled conversation')[:500],
                                           'node_id': nid if nid is not None else node.get('id')}
                                raw_msg = node.get('message')
                                if isinstance(raw_msg, dict):
                                    author = raw_msg.get('author')
                                    content = raw_msg.get('content')
                                    context['role'] = author.get('role') if isinstance(author, dict) else None
                                    context['content_type'] = content.get('content_type') if isinstance(content, dict) else None
                                def emit_node(*args):
                                    emit(*args, context=context)
                                msg = inspect_node(node, msg_path, emit_node)
                                content = msg.get('content') or {}
                                unused, text_candidates = False, False
                                normal_text_empty = not bool(app.flatten_content(content))
                                for relative, text in unused_content_strings(content):
                                    unused = True
                                    text_like = relative[-1] in TEXT_KEYS
                                    text_candidates = text_candidates or text_like
                                    field_path = json_path(json_path(msg_path, 'message'), 'content')
                                    for segment in relative:
                                        field_path = json_path(field_path, segment) if isinstance(segment, str) else field_path + f'[{segment}]'
                                    classification = 'text_like_field_name' if text_like else 'metadata_reference_or_other_string'
                                    content_writer.writerow(safe_csv({
                                        'source': str(path), 'top_level_value': current_top,
                                        'json_path': field_path, **context,
                                        'classification': classification, 'characters': len(text),
                                        'normal_message_text_empty': normal_text_empty,
                                        'field_sha256': hashlib.sha256(text.encode('utf-8', 'surrogatepass')).hexdigest(),
                                        'preview': text[:240],
                                    }))
                                    counts['unindexed_content_string_fields'] += 1
                                    if text_like:
                                        counts['unindexed_text_like_fields'] += 1
                                if unused:
                                    info['nodes_with_unindexed_content_strings'] = info.get('nodes_with_unindexed_content_strings', 0) + 1
                                if text_candidates:
                                    info['nodes_with_unindexed_text_like_fields'] = info.get('nodes_with_unindexed_text_like_fields', 0) + 1
                                    if normal_text_empty:
                                        info['nodes_with_unindexed_text_like_fields_and_empty_search_text'] = info.get('nodes_with_unindexed_text_like_fields_and_empty_search_text', 0) + 1
                                info['recognized_nodes'] += 1
                                if indexed_source is not None:
                                    identity = nid if nid is not None else node.get('id') or msg.get('id')
                                    if nid is None and not identity:
                                        emit_node('warning', node_path, 'generated_identity_unverifiable', 'The importer generates a time-dependent index identity here; this audit cannot reconstruct it.')
                                    else:
                                        row = connection.execute('SELECT pk FROM messages WHERE source_id=? AND conversation_id=? AND node_id=?',
                                                                 (indexed_source['id'], cid, str(identity))).fetchone()
                                        if row is None:
                                            info['recognized_nodes_absent_from_index'] += 1
                                            emit_node('excluded', node_path, 'recognized_record_absent_from_index', 'This recognized identity is absent from the selected source in the index snapshot.', {'conversation_id': cid, 'id': identity})
                                        else:
                                            info['indexed_nodes_verified'] += 1
                    info['status'] = 'scanned'
                except Exception as exc:
                    info.update(status='failed', error=f'{type(exc).__name__}: {exc}')
                    emit('warning', '$', 'file_audit_failed', 'The audit could not finish this file. A parse/type error also prevents a successful file import; earlier recognized candidates are not proof of committed rows.', info['error'])
                finally:
                    try:
                        info['sha256_after'] = app.sha256_file(path)
                        info['source_unchanged'] = before is not None and before == info['sha256_after']
                        if not info['source_unchanged']:
                            info['status'] = 'changed_during_audit'
                            emit('warning', '$', 'source_changed_during_audit', 'Source checksum changed during the scan; do not treat this report as a stable-source audit.')
                    except OSError as exc:
                        info.update(status='failed', checksum_error=str(exc), source_unchanged=False)
    finally:
        if connection is not None:
            connection.close()
        summary['counts'] = dict(counts)
        summary['status'] = 'incomplete' if any(s['status'] != 'scanned' for s in summary['sources']) or counts['missing_input'] else 'complete'
        if not paths and not ignored:
            summary['status'] = 'no_inputs'
        summary['finished_utc'] = app.utc_now()
        (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('inputs', nargs='+', type=Path)
    parser.add_argument('--out', required=True, type=Path, help='New report directory outside input directories')
    parser.add_argument('--database', type=Path, help='Optional existing index, opened read-only; never initialized or repaired')
    parser.add_argument('--source-id', type=int, help='Optional known source ID when input path differs from indexed copy')
    args = parser.parse_args(argv)
    report = audit(args.inputs, args.out, args.database, args.source_id)
    print(json.dumps({'status': report['status'], 'report_directory': str(args.out.resolve()), 'counts': report['counts']}))
    return 0 if report['status'] == 'complete' else 1


if __name__ == '__main__':
    raise SystemExit(main())
