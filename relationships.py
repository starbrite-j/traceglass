"""Recorded relationships between distinct message occurrences. No semantic edges."""
import json
import re
import time
import operations
from collections import deque, defaultdict

SCHEMA = """
CREATE TABLE IF NOT EXISTS lineage_refs (
  owner_pk INTEGER NOT NULL REFERENCES messages(pk) ON DELETE CASCADE,
  field TEXT NOT NULL, ordinal INTEGER NOT NULL, target_id TEXT NOT NULL,
  PRIMARY KEY(owner_pk,field,ordinal)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_lineage_target ON lineage_refs(target_id,field,owner_pk);
CREATE INDEX IF NOT EXISTS ix_messages_global_node ON messages(node_id,source_id,conversation_id,pk);
CREATE INDEX IF NOT EXISTS ix_messages_lineage_catalog ON messages(source_id,conversation_id,node_id,message_id,pk);
CREATE TABLE IF NOT EXISTS lineage_events (
  owner_pk INTEGER PRIMARY KEY REFERENCES messages(pk) ON DELETE CASCADE,
  flags_json TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS lineage_event_invalidate AFTER UPDATE OF raw_json,content_text,status,role,recipient,channel,content_type,metadata_json,parent_id,children_json ON messages BEGIN
  DELETE FROM lineage_events WHERE owner_pk=new.pk;
END;
CREATE TABLE IF NOT EXISTS lineage_index_state(version INTEGER PRIMARY KEY);
CREATE TRIGGER IF NOT EXISTS lineage_insert AFTER INSERT ON messages BEGIN
  INSERT INTO lineage_refs SELECT new.pk,'node.parent',-1,new.parent_id
    WHERE typeof(new.parent_id)='text' AND new.parent_id<>'';
  INSERT INTO lineage_refs SELECT new.pk,'message.metadata.parent_id',-1,json_extract(new.metadata_json,'$.parent_id')
    WHERE json_type(new.metadata_json,'$.parent_id')='text' AND json_extract(new.metadata_json,'$.parent_id')<>'';
  INSERT INTO lineage_refs SELECT new.pk,'node.children',CAST(key AS INTEGER),value
    FROM json_each(new.children_json) WHERE type='text' AND value<>'';
END;
CREATE TRIGGER IF NOT EXISTS lineage_update AFTER UPDATE OF parent_id,metadata_json,children_json ON messages BEGIN
  DELETE FROM lineage_refs WHERE owner_pk=new.pk;
  INSERT INTO lineage_refs SELECT new.pk,'node.parent',-1,new.parent_id
    WHERE typeof(new.parent_id)='text' AND new.parent_id<>'';
  INSERT INTO lineage_refs SELECT new.pk,'message.metadata.parent_id',-1,json_extract(new.metadata_json,'$.parent_id')
    WHERE json_type(new.metadata_json,'$.parent_id')='text' AND json_extract(new.metadata_json,'$.parent_id')<>'';
  INSERT INTO lineage_refs SELECT new.pk,'node.children',CAST(key AS INTEGER),value
    FROM json_each(new.children_json) WHERE type='text' AND value<>'';
END;
CREATE TRIGGER IF NOT EXISTS lineage_delete AFTER DELETE ON messages BEGIN
  DELETE FROM lineage_refs WHERE owner_pk=old.pk;
  DELETE FROM lineage_events WHERE owner_pk=old.pk;
END;
"""


FLAG_COLUMNS = 'raw_json,content_text,status,content_type,role,channel,recipient'


def cache_flags(conn, pk):
    row = conn.execute('SELECT '+FLAG_COLUMNS+' FROM messages WHERE pk=?',(pk,)).fetchone()
    conn.execute('INSERT OR REPLACE INTO lineage_events VALUES(?,?)',
                 (pk,json.dumps(flags(row),ensure_ascii=True,separators=(',',':'))))


def backfill(conn):
    if not conn.execute('SELECT 1 FROM lineage_index_state WHERE version=1').fetchone():
        conn.execute("INSERT OR IGNORE INTO lineage_refs SELECT pk,'node.parent',-1,parent_id FROM messages WHERE typeof(parent_id)='text' AND parent_id<>''")
        conn.execute("""INSERT OR IGNORE INTO lineage_refs SELECT pk,'message.metadata.parent_id',-1,
            json_extract(metadata_json,'$.parent_id') FROM messages
            WHERE json_type(metadata_json,'$.parent_id')='text' AND json_extract(metadata_json,'$.parent_id')<>''""")
        conn.execute("""INSERT OR IGNORE INTO lineage_refs SELECT m.pk,'node.children',CAST(j.key AS INTEGER),j.value
            FROM messages m,json_each(m.children_json) j WHERE j.type='text' AND j.value<>''""")
        conn.execute('INSERT INTO lineage_index_state VALUES(1)')
    last=0
    while True:
        pending=[r[0] for r in conn.execute('SELECT m.pk FROM messages m LEFT JOIN lineage_events e ON e.owner_pk=m.pk WHERE m.pk>? AND e.owner_pk IS NULL ORDER BY m.pk LIMIT 100',(last,))]
        if not pending:break
        for pk in pending:cache_flags(conn,pk)
        last=pending[-1]


def integer(q, key, default, low, high):
    try:
        return max(low, min(high, int(q.get(key, [default])[0])))
    except (ValueError, TypeError):
        raise ValueError('Invalid ' + key)


def key(pk):
    return 'record:' + str(pk)


def flags(row):
    raw = json.loads(row['raw_json'])
    msg = raw.get('message') or {}
    md = msg.get('metadata') or {}
    evidence = []
    if row['status'] in ('failed', 'error', 'timeout', 'timed_out'):
        evidence.append({'field': 'message.status', 'value': row['status']})
    if row['content_type'] == 'system_error':
        evidence.append({'field': 'message.content.content_type', 'value': 'system_error'})
    for label, obj in [('message', msg), ('message.metadata', md), ('message.content', msg.get('content') or {})]:
        if isinstance(obj, dict) and obj.get('error') not in (None, False, ''):
            evidence.append({'field': label + '.error', 'value_preview': str(obj['error'])[:160]})
        if isinstance(obj, dict) and obj.get('is_error') is True:
            evidence.append({'field':label+'.is_error','value':True})
        if row['role']=='tool' and isinstance(obj,dict) and type(obj.get('exit_code')) is int and obj['exit_code']!=0:
            evidence.append({'field':label+'.exit_code','value':obj['exit_code']})
    error_hint = None
    if row['role']=='tool':
        text=row['content_text'] or ''
        match=re.search(r'(?im)^(?:Traceback \(most recent call last\):|(?:[A-Za-z_][\w.]*Error|Exception):|Error:|Process exited with code [1-9]\d*)[^\n]*',text[:4096]+'\n'+text[-4096:])
        if match:
            error_hint={'field':'message content (first/last 4096 characters)','excerpt':match.group(0)[:200],
                        'meaning':'Error-like text: review source; not a verified execution status.'}
    return {'error_reported': bool(evidence), 'error_evidence': evidence,
            'error_text_hint':error_hint,'interrupted':row['status'] in ('cancelled','canceled','interrupted'),
            'thinking': row['content_type'] in ('thoughts', 'reasoning_recap') or row['channel'] == 'analysis',
            'thinking_basis': 'exported content_type/channel',
            'hidden': md.get('is_visually_hidden_from_conversation') is True,
            'tool_attempt': row['role'] == 'assistant' and row['recipient'] not in (None, '', 'all'),
            'tool_result': row['role'] == 'tool',
            'outer_parent_state': 'missing' if 'parent' not in raw else 'null' if raw['parent'] is None else 'text' if isinstance(raw['parent'],str) else 'invalid',
            'outer_parent': raw.get('parent'),
            'metadata_parent_state': 'missing' if 'parent_id' not in md else 'null' if md['parent_id'] is None else 'text' if isinstance(md['parent_id'],str) else 'invalid',
            'metadata_parent': md.get('parent_id') if isinstance(md.get('parent_id'),str) else None}


class Neighborhood:
    # Each database occurrence remains distinct, including conflicting copies.
    CANDIDATES = 32
    REFERENCES = 128
    EDGES = 15000
    REFERENCE_WORK = 20000

    def __init__(self, conn, dataset, compare=0):
        self.conn, self.dataset, self.compare = conn, dataset, compare or dataset
        self.rows, self.links, self.adjacency = {}, {}, {}
        self.gaps, self.bounds = {}, {}
        self.neighbor_map = defaultdict(set)
        self.processed_refs = set()

    def add_edge(self, edge):
        if len(self.links)>=self.EDGES and edge['id'] not in self.links:
            self.bounds['edge_limit']={'reason':'edge_limit','returned':self.EDGES}
            return
        self.links[edge['id']] = edge
        a,b = (int(edge[k].split(':')[1]) for k in ('source','target'))
        self.neighbor_map[a].add(b);self.neighbor_map[b].add(a)

    def light(self, pk):
        if pk not in self.rows:
            row = self.conn.execute('''SELECT m.pk,m.source_id,m.conversation_id,m.node_id,m.message_id
                FROM messages m JOIN sources s ON s.id=m.source_id WHERE m.pk=? AND s.dataset_id IN (?,?)''', (pk, self.dataset,self.compare)).fetchone()
            if row:
                self.rows[pk] = dict(row)
        return self.rows.get(pk)

    def targets(self, owner, field, target):
        namespace = "(m.node_id=? OR m.message_id=?)" if field == 'message.metadata.parent_id' else 'm.node_id=?'
        args = [target, target] if field == 'message.metadata.parent_id' else [target]
        where = namespace + ' AND s.dataset_id IN (?,?)'
        args.extend((self.dataset,self.compare))
        total = self.conn.execute('SELECT count(*) FROM messages m JOIN sources s ON s.id=m.source_id WHERE '+where, args).fetchone()[0]
        rows = self.conn.execute('''SELECT m.pk,m.source_id,m.conversation_id,m.node_id,m.message_id
            FROM messages m JOIN sources s ON s.id=m.source_id WHERE '''+where+'''
            ORDER BY (m.source_id=? AND m.conversation_id=?) DESC,(m.source_id=?) DESC,m.pk LIMIT ?''',
            [*args, owner['source_id'], owner['conversation_id'], owner['source_id'], self.CANDIDATES]).fetchall()
        for row in rows:
            self.rows[row['pk']] = dict(row)
        return rows, total

    def reference(self, ref):
        ref_key = (ref['owner_pk'],ref['field'],ref['ordinal'])
        if ref_key in self.processed_refs:
            return
        if len(self.processed_refs)>=self.REFERENCE_WORK or len(self.links)>=self.EDGES:
            self.bounds['work_limit']={'reason':'reference_or_edge_work_limit','reference_limit':self.REFERENCE_WORK,'edge_limit':self.EDGES}
            return
        self.processed_refs.add(ref_key)
        owner = self.light(ref['owner_pk'])
        if not owner:
            return
        rows, total = self.targets(owner, ref['field'], ref['target_id'])
        identity = (owner['pk'], ref['field'], ref['ordinal'])
        if total == 0:
            self.gaps[identity] = {'owner': key(owner['pk']), 'field': ref['field'], 'ordinal': ref['ordinal'],
                                   'target_id': ref['target_id'], 'state': 'target_absent_in_dataset'}
        if total > len(rows):
            self.bounds['reference:'+str(identity)] = {'owner': key(owner['pk']), 'reason': 'candidate_limit',
                                                      'total': total, 'returned': len(rows)}
        for target in rows:
            parent, child = (owner, target) if ref['field'] == 'node.children' else (target, owner)
            edge_id = 'ref:%s:%s:%s:%s' % (*identity, target['pk'])
            self.add_edge({
                'id': edge_id, 'source': key(parent['pk']), 'target': key(child['pk']),
                'kind': {'node.parent': 'outer', 'node.children': 'children', 'message.metadata.parent_id': 'metadata'}[ref['field']],
                'directed': True, 'field': ref['field'], 'ordinal': ref['ordinal'], 'owner': key(owner['pk']),
                'literal_target_id': ref['target_id'], 'candidate_count': total, 'ambiguous': total > 1,
                'target_namespace': ('both' if target['node_id'] == target['message_id'] == ref['target_id'] else
                                     'node_id' if target['node_id'] == ref['target_id'] else 'message_id'),
                'cross_conversation': owner['conversation_id'] != target['conversation_id'],
                'cross_source': owner['source_id'] != target['source_id'],
                'origin_source_id': owner['source_id'], 'origin_conversation_id': owner['conversation_id'],
            })

    def neighbors(self, pk):
        if pk in self.adjacency:
            return sorted(self.neighbor_map[pk])
        node = self.light(pk)
        own = self.conn.execute('SELECT * FROM lineage_refs WHERE owner_pk=? ORDER BY field,ordinal LIMIT ?',
                                (pk, self.REFERENCES + 1)).fetchall()
        incoming = self.conn.execute('''SELECT r.* FROM lineage_refs r INDEXED BY ix_lineage_target JOIN messages m ON m.pk=r.owner_pk
            JOIN sources s ON s.id=m.source_id WHERE s.dataset_id IN (?,?) AND r.target_id IN (?,?) AND
            (r.target_id=? OR r.field='message.metadata.parent_id')
            ORDER BY r.owner_pk,r.field,r.ordinal LIMIT ?''',
            (self.dataset,self.compare, node['node_id'], node['message_id'], node['node_id'], self.REFERENCES + 1)).fetchall()
        for name, refs in [('outgoing', own), ('incoming', incoming)]:
            if len(refs) > self.REFERENCES:
                self.bounds[(pk, name)] = {'owner': key(pk), 'reason': name+'_reference_limit', 'returned': self.REFERENCES,
                                          'at_least': self.REFERENCES + 1}
            for ref in refs[:self.REFERENCES]:
                self.reference(ref)
        if node['message_id']:
            copies = self.conn.execute('''SELECT m.pk,m.source_id,m.conversation_id,m.node_id,m.message_id
                FROM messages m JOIN sources s ON s.id=m.source_id
                WHERE s.dataset_id IN (?,?) AND m.message_id=? AND m.pk<>? ORDER BY m.pk LIMIT ?''',
                (self.dataset,self.compare, node['message_id'], pk, self.CANDIDATES + 1)).fetchall()
            if len(copies) > self.CANDIDATES:
                self.bounds[(pk, 'identity')] = {'owner': key(pk), 'reason': 'shared_identity_limit', 'returned': self.CANDIDATES,
                                               'at_least': self.CANDIDATES+1}
            for other in copies[:self.CANDIDATES]:
                self.rows[other['pk']] = dict(other)
                a, b = sorted((pk, other['pk']))
                edge_id = f'identity:{a}:{b}'
                self.add_edge({'id': edge_id, 'source': key(a), 'target': key(b), 'kind': 'identity',
                    'directed': False, 'field': 'message.id', 'literal_target_id': node['message_id'],
                    'ambiguous': False, 'cross_conversation': node['conversation_id'] != other['conversation_id'],
                    'cross_source': node['source_id'] != other['source_id'],
                    'meaning': 'Same exported message ID; distinct occurrences, not a causal parent edge.'})
        self.adjacency[pk] = True
        return sorted(self.neighbor_map[pk])


def neighborhood(conn, q):
    pk = integer(q, 'pk', 0, 0, 2**63-1)
    if pk==0:
        raise ValueError('A positive occurrence pk is required')
    limit = integer(q, 'limit', 300, 10, 3000)
    depth = integer(q, 'depth', 4, 1, 12)
    row = conn.execute('SELECT s.dataset_id FROM messages m JOIN sources s ON s.id=m.source_id WHERE m.pk=?', (pk,)).fetchone()
    if row is None:
        raise LookupError('Record not found')
    compare = integer(q,'compare_dataset_id',0,0,2**31)
    graph = Neighborhood(conn, row['dataset_id'],compare)
    chosen, seen, pending, distance = [], {pk}, deque([(pk, 0)]), {pk: 0}
    boundary = False
    while pending:
        current, level = pending.popleft()
        chosen.append(current)
        neighbors = graph.neighbors(current)
        for other in neighbors:
            if other in seen:
                continue
            if level == depth or len(seen) >= limit:
                boundary = True
                continue
            seen.add(other); distance[other] = level + 1; pending.append((other, level + 1))
    # Query each loaded occurrence once; do not collapse by node/message ID.
    nodes = []
    for offset in range(0, len(chosen), 400):
        batch = chosen[offset:offset+400]
        marks = ','.join('?' for _ in batch)
        rows = conn.execute(f'''SELECT m.pk,m.node_id,m.message_id,m.source_id,m.conversation_id,m.role,m.content_type,m.status,m.recipient,m.display_time,m.timestamp_status,substr(m.content_text,1,200) content_text,m.canonical_content_hash,e.flags_json,c.title,s.dataset_id FROM messages m LEFT JOIN lineage_events e ON e.owner_pk=m.pk JOIN sources s ON s.id=m.source_id LEFT JOIN conversations c
            ON c.source_id=m.source_id AND c.conversation_id=m.conversation_id WHERE m.pk IN ({marks})''', batch)
        for node in rows:
            if node['flags_json'] is None:raise BlockingIOError('Derived event index needs repair; restart Traceglass before tracing this record')
            node_flags = json.loads(node['flags_json'])
            nodes.append({'id': key(node['pk']), 'pk': node['pk'], 'node_id': node['node_id'], 'message_id': node['message_id'],
                'source_id': node['source_id'], 'dataset_id':node['dataset_id'], 'conversation_id': node['conversation_id'], 'title': node['title'],
                'role': node['role'], 'content_type': node['content_type'], 'status': node['status'], 'recipient': node['recipient'],
                'display_time': node['display_time'], 'timestamp_status': node['timestamp_status'], 'preview': (node['content_text'] or '')[:200],
                'content_hash': node['canonical_content_hash'], 'distance': distance[node['pk']], **node_flags})
    nodes.sort(key=lambda n: (n['distance'], n['pk']))
    selected = {n['id'] for n in nodes}
    edges, unloaded = [], []
    for edge in graph.links.values():
        if edge['source'] in selected and edge['target'] in selected:
            edges.append(edge)
        elif edge['source'] in selected or edge['target'] in selected:
            unloaded.append(edge)
    # A leaf means no continuation recorded in this dataset, not an inferred failure.
    by_id = {n['id']: n for n in nodes}
    for node in nodes:
        continuation = conn.execute('''SELECT EXISTS(SELECT 1 FROM lineage_refs WHERE owner_pk=? AND field='node.children')
            OR EXISTS(SELECT 1 FROM lineage_refs r INDEXED BY ix_lineage_target JOIN messages m ON m.pk=r.owner_pk
                JOIN sources s ON s.id=m.source_id WHERE r.target_id=? AND r.field='node.parent' AND s.dataset_id IN (?,?))
            OR EXISTS(SELECT 1 FROM lineage_refs r INDEXED BY ix_lineage_target JOIN messages m ON m.pk=r.owner_pk
                JOIN sources s ON s.id=m.source_id WHERE r.target_id IN (?,?) AND r.field='message.metadata.parent_id' AND s.dataset_id IN (?,?))''',
            (node['pk'],node['node_id'],row['dataset_id'],graph.compare,node['node_id'],node['message_id'],row['dataset_id'],graph.compare)).fetchone()[0]
        node['terminal_in_dataset'] = not bool(continuation)
    for edge in edges:
        if edge['kind'] == 'identity':
            edge['content_differs'] = by_id[edge['source']]['content_hash'] != by_id[edge['target']]['content_hash']
    source_ids = sorted({n['source_id'] for n in nodes})
    sources = [dict(r) for r in conn.execute('SELECT id,dataset_id,path,sha256,size_bytes FROM sources WHERE id IN ('+','.join('?' for _ in source_ids)+')',source_ids)]
    return {'schema_version':2,'index_id':operations.identity(conn),'nodes': nodes, 'edges': edges, 'gaps': list(graph.gaps.values()), 'unloaded_edges': unloaded[:200], 'sources':sources,
            'unloaded_edge_count': len(unloaded), 'bounds': list(graph.bounds.values()),
            'truncated': boundary or bool(graph.bounds), 'limit': limit, 'depth': depth, 'focus': key(pk),
            'dataset_id': row['dataset_id'], 'compare_dataset_id':compare,
            'available_datasets':[dict(r) for r in conn.execute('SELECT id,name FROM datasets ORDER BY id DESC LIMIT 100')],
            'conversation_count': len({(n['source_id'], n['conversation_id']) for n in nodes}),
            'scope': 'Focus dataset plus explicitly selected comparison dataset, if any. Breadth-first in both directions across recorded references and shared message IDs. Identity does not merge occurrences; proximity and timestamps do not create edges.'}


def read_snapshot(conn, function, q):
    """One consistent read-only HTTP snapshot, with a SQLite execution deadline."""
    own_transaction = not conn.in_transaction
    if own_transaction:
        conn.execute('BEGIN')
    deadline=time.monotonic()+10
    conn.set_progress_handler(lambda: int(time.monotonic()>deadline),10000)
    try:
        return function(conn,q)
    finally:
        conn.set_progress_handler(None,0)
        if own_transaction:
            conn.rollback()


def basins(conn, q):
    dataset = integer(q, 'dataset_id', 0, 0, 2**31)
    page = integer(q, 'page', 1, 1, 100000)
    size = 300
    query = str(q.get('q', [''])[0])[:200]
    source = integer(q, 'source_id', 0, 0, 2**31)
    clause = 's.dataset_id=? AND (?=0 OR c.source_id=?) AND (instr(lower(c.title),lower(?))>0 OR instr(c.conversation_id,?)>0)'
    args = [dataset, source, source, query, query]
    total = conn.execute('SELECT count(*) FROM conversations c JOIN sources s ON s.id=c.source_id WHERE '+clause,args).fetchone()[0]
    page = min(page,max(1,(total+size-1)//size))
    rows = conn.execute('''SELECT c.pk,c.source_id,c.conversation_id,c.title,
        (SELECT count(*) FROM messages m WHERE m.source_id=c.source_id AND m.conversation_id=c.conversation_id) nodes,
        COALESCE((SELECT pk FROM messages m WHERE m.source_id=c.source_id AND m.conversation_id=c.conversation_id AND m.node_id=c.current_node LIMIT 1),
                 (SELECT pk FROM messages m WHERE m.source_id=c.source_id AND m.conversation_id=c.conversation_id ORDER BY pk DESC LIMIT 1)) record_pk
        FROM conversations c JOIN sources s ON s.id=c.source_id WHERE '''+clause+' ORDER BY c.pk LIMIT ? OFFSET ?',[*args,size,(page-1)*size]).fetchall()
    nodes = [dict(r, id='basin:'+str(r['pk'])) for r in rows]
    if not nodes:
        return {'nodes': [], 'edges': [], 'total': total, 'page': page, 'pages': max(1,(total+size-1)//size), 'truncated': False}
    ids = [n['pk'] for n in nodes]
    marks = ','.join('?' for _ in ids)
    # Covering-index scans avoid a large correlated join over raw message rows.
    # The overview has explicit work budgets; the record resolver uses indexed
    # lookups against the full dataset, independently of this coarse window.
    from collections import Counter
    catalog_limit, reference_limit, pair_limit = 250000, 500000, 200000
    by_node, by_message, owners = defaultdict(list), defaultdict(list), {}
    visible = {n['pk'] for n in nodes}
    catalog_truncated = False
    catalog_rows = 0
    for r in conn.execute("""SELECT m.pk,m.node_id,m.message_id,c.pk basin FROM sources s
        JOIN messages m INDEXED BY ix_messages_lineage_catalog ON m.source_id=s.id
        JOIN conversations c ON c.source_id=m.source_id AND c.conversation_id=m.conversation_id
        WHERE s.dataset_id=? ORDER BY m.source_id,m.conversation_id,m.node_id LIMIT ?""", (dataset,catalog_limit+1)):
        if catalog_rows == catalog_limit:
            catalog_truncated = True;break
        catalog_rows += 1
        occurrence = (r['pk'],r['basin'])
        by_node[r['node_id']].append(occurrence)
        if r['message_id']:
            by_message[r['message_id']].append(occurrence)
        if r['basin'] in visible:
            owners[r['pk']] = r['basin']
    bridges, samples = Counter(), {}
    high_targets, offscreen, reference_truncated = set(), 0, False
    scanned = 0
    for r in conn.execute("""SELECT r.* FROM conversations c JOIN messages m INDEXED BY ix_messages_lineage_catalog
        ON m.source_id=c.source_id AND m.conversation_id=c.conversation_id
        JOIN lineage_refs r ON r.owner_pk=m.pk WHERE c.pk IN ("""+marks+") ORDER BY r.owner_pk,r.field,r.ordinal LIMIT ?",[*ids,reference_limit+1]):
        if scanned == reference_limit:
            reference_truncated=True;break
        scanned += 1
        a = owners.get(r['owner_pk'])
        if a is None:
            continue
        node_targets=by_node.get(r['target_id'],())
        message_targets=by_message.get(r['target_id'],()) if r['field']=='message.metadata.parent_id' else ()
        if len(node_targets)>32 or len(message_targets)>32:
            high_targets.add((r['field'],r['target_id']));continue
        targets = dict(node_targets)
        if r['field']=='message.metadata.parent_id':
            targets.update(message_targets)
        if len(targets)>32:
            high_targets.add((r['field'],r['target_id']));continue
        if any(b==a for b in targets.values()):
            continue
        for b in targets.values():
            if b not in visible:
                offscreen+=1;continue
            if a==b:continue
            bridge=(a,b,{'node.parent':'outer','node.children':'children','message.metadata.parent_id':'metadata'}[r['field']],r['field'])
            bridges[bridge]+=1;samples.setdefault(bridge,r['owner_pk'])
    identity_work, identity_truncated, hubs = 0, False, 0
    for mid,appearances in by_message.items():
        if len(appearances)>32:
            hubs+=1;continue
        eligible = sorted({basin:pk for pk,basin in appearances if basin in visible}.items())
        for i,(a,pk) in enumerate(eligible):
            for b,_ in eligible[i+1:]:
                identity_work+=1
                if identity_work>pair_limit:
                    identity_truncated=True;break
                bridge=(a,b,'identity','message.id')
                bridges[bridge]+=1;samples.setdefault(bridge,pk)
            if identity_truncated:break
        if identity_truncated:break
    ranked=sorted(bridges,key=lambda k:(-bridges[k],k))[:2000]
    edges=[{'source':'basin:'+str(a),'target':'basin:'+str(b),'kind':kind,'field':field,
            'weight':bridges[(a,b,kind,field)],'record_pk':samples[(a,b,kind,field)]} for a,b,kind,field in ranked]
    return {'nodes':nodes,'edges':edges,'total':total,'page':page,'pages':max(1,(total+size-1)//size),
        'truncated':total>len(nodes) or len(bridges)>2000 or catalog_truncated or reference_truncated or identity_truncated or hubs>0 or bool(high_targets),
        'catalog_rows':catalog_rows,'catalog_limit':catalog_limit,'catalog_truncated':catalog_truncated,
        'references_scanned':scanned,'reference_scan_limit':reference_limit,'reference_scan_truncated':reference_truncated,
        'identity_pair_work_limit':pair_limit,'identity_pair_work_truncated':identity_truncated,
        'high_fanout_identity_groups':hubs,'high_fanout_target_groups':len(high_targets),'offscreen_reference_matches':offscreen,
        'scope':'Each basin is one conversation occurrence. Bridges aggregate literal cross-only references or shared message IDs in the displayed window. Work limits are explicit; overview omissions are not missing records. Layout is not causal evidence.'}
