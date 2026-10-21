"""Read-only, source-scoped graph windows. Layout never creates evidence links."""
import json
from collections import deque


def integer(q, key, default, low, high):
    try:
        return max(low, min(high, int(q.get(key, [default])[0])))
    except (ValueError, TypeError):
        raise ValueError(f"Invalid {key}")


def atlas(conn, q):
    dataset = integer(q, 'dataset_id', 0, 0, 2**31)
    source = integer(q, 'source_id', 0, 0, 2**31)
    page = integer(q, 'page', 1, 1, 100000)
    query = str(q.get('q', [''])[0])[:200]
    where, args = ['s.dataset_id=?'], [dataset]
    if source:
        where.append('c.source_id=?'); args.append(source)
    if query:
        where.append("(instr(lower(c.title),lower(?))>0 OR instr(c.conversation_id,?)>0)")
        args.extend([query, query])
    clause = ' AND '.join(where)
    total = conn.execute(f'SELECT count(*) FROM conversations c JOIN sources s ON s.id=c.source_id WHERE {clause}', args).fetchone()[0]
    page = min(page, max(1, (total+59)//60))
    rows = conn.execute(f"""SELECT c.conversation_id,c.source_id,c.title,c.current_node,
        (SELECT count(*) FROM messages m WHERE m.source_id=c.source_id AND m.conversation_id=c.conversation_id) nodes,
        (SELECT pk FROM messages m WHERE m.source_id=c.source_id AND m.conversation_id=c.conversation_id
          ORDER BY CASE WHEN m.node_id=c.current_node THEN 0 ELSE 1 END,m.pk DESC LIMIT 1) pk
        FROM conversations c JOIN sources s ON s.id=c.source_id WHERE {clause}
        ORDER BY c.update_time DESC,c.pk DESC LIMIT 60 OFFSET ?""", [*args, (page-1)*60]).fetchall()
    return {'items': [dict(r) for r in rows], 'page': page, 'total': total,
            'pages': max(1, (total+59)//60), 'scope': 'Conversation copies in selected dataset. No inferred inter-conversation edges.'}


def graph(conn, q):
    source = integer(q, 'source_id', 0, 1, 2**31)
    compare = integer(q, 'compare_source', 0, 0, 2**31)
    limit = integer(q, 'limit', 600, 50, 6000)
    cid = str(q.get('conversation_id', [''])[0])
    focus = str(q.get('focus', [''])[0])
    conv = conn.execute('SELECT title,current_node FROM conversations WHERE source_id=? AND conversation_id=?', (source, cid)).fetchone()
    if not conv:
        raise LookupError('Conversation not found in this source')

    def read(sid):
        # Only lightweight columns: never ship a full tool payload to the canvas.
        rows = conn.execute('''SELECT pk,node_id,parent_id,children_json,role,substr(content_text,1,180) preview,
            display_time,metadata_json,json_type(raw_json,'$.parent') parent_type
            FROM messages WHERE source_id=? AND conversation_id=? ORDER BY pk''', (sid, cid)).fetchall()
        result = {}
        for r in rows:
            d = dict(r); md = json.loads(d.pop('metadata_json') or '{}')
            if not isinstance(md, dict): md = {}
            d['metadata_parent_present'] = 'parent_id' in md
            d['metadata_parent'] = md.get('parent_id')
            d['hidden'] = md.get('is_visually_hidden_from_conversation') is True
            d['children'] = json.loads(d.pop('children_json') or '[]')
            result[d['node_id']] = d
        return result

    active = read(source)
    if focus not in active:
        focus = conv['current_node'] if conv['current_node'] in active else next(iter(active), '')
    other = read(compare) if compare and compare != source else {}
    children = {}
    for nid, n in active.items():
        if isinstance(n['parent_id'], str):
            children.setdefault(n['parent_id'], []).append(nid)
    # Source-wide stable layout indices prevent newly loaded ancestors occupying
    # the coordinates of an earlier window. These indices are layout, not evidence.
    layout_seen = set()
    starts = [nid for nid,n in active.items() if n['parent_id'] not in active]
    for start in starts + list(active):
        stack = [start]
        while stack:
            nid = stack.pop()
            if nid in layout_seen: continue
            active[nid]['layout_index'] = len(layout_seen)
            layout_seen.add(nid)
            stack.extend(reversed(children.get(nid, [])))
    chosen, seen = [], set()
    cur = focus
    # Ancestors have priority, so a high-degree fork cannot displace the focus.
    last_type = None
    while isinstance(cur, str) and cur in active and cur not in seen and len(chosen) < limit:
        last_type = active[cur]['parent_type']
        chosen.append(cur); seen.add(cur); cur = active[cur]['parent_id']
        if last_type != 'text': break
    if last_type is None: ancestor_stop = 'missing_field'
    elif last_type == 'null': ancestor_stop = 'root'
    elif last_type != 'text' or not cur: ancestor_stop = 'invalid_parent'
    elif isinstance(cur, str) and cur in active:
        ancestor_stop = 'cycle' if cur in seen else 'window_limit'
    else: ancestor_stop = 'missing_target'
    pending = deque(chosen)
    while pending and len(chosen) < limit:
        for child in children.get(pending.popleft(), []):
            if child not in seen:
                seen.add(child); chosen.append(child); pending.append(child)
                if len(chosen) >= limit: break
    # Include disconnected components only when there is room; never invent links.
    for nid in active:
        if len(chosen) >= limit: break
        if nid not in seen: seen.add(nid); chosen.append(nid)
    supplemental = []
    if other:
        pending = deque(chosen)
        while pending and len(chosen)+len(supplemental) < limit:
            nid = pending.popleft()
            for parent in [other.get(nid, {}).get('parent_id'), active.get(nid, {}).get('metadata_parent')]:
                if isinstance(parent, str) and parent in other and parent not in seen:
                    seen.add(parent)
                    (chosen if parent in active else supplemental).append(parent)
                    pending.append(parent)
                    if len(chosen)+len(supplemental) >= limit: break
    edges, gaps, nodes = [], [], []
    for nid in chosen+supplemental:
        n = active[nid] if nid in active else other[nid]
        node = {**n, 'id': nid, 'source_id': source if nid in active else compare,
                'supplemental': nid not in active, 'is_focus': nid == focus,
                'unloaded_children': sum(ch not in seen for ch in children.get(nid, []))}
        node['mismatch'] = n['metadata_parent_present'] and n['metadata_parent'] != n['parent_id']
        own_mapping = active if nid in active else other
        parent_record = own_mapping.get(n['parent_id']) if isinstance(n['parent_id'], str) else None
        node['parent_lists_child'] = nid in parent_record['children'] if parent_record else None
        nodes.append(node)
        relationships = []
        if nid in active:
            relationships.append(('outer', active[nid]['parent_id'], source, active))
            if active[nid]['metadata_parent_present'] and active[nid]['metadata_parent'] != active[nid]['parent_id']:
                relationships.append(('metadata', active[nid]['metadata_parent'], source, active))
        if nid in other:
            relationships.append(('other', other[nid]['parent_id'], compare, other))
        for kind, parent, sid, mapping in relationships:
            if kind in {'outer', 'other'} and mapping[nid]['parent_type'] not in {'text','null',None}:
                gaps.append({'child':nid,'target':str(parent),'kind':kind,'state':'invalid','source_id':sid}); continue
            if parent is None: continue
            if not isinstance(parent, str) or not parent:
                gaps.append({'child': nid, 'target': str(parent), 'kind': kind, 'state': 'invalid', 'source_id': sid}); continue
            if parent in seen:
                edges.append({'source': parent, 'target': nid, 'kind': kind, 'source_id': sid})
                if parent not in mapping:
                    gaps.append({'child': nid, 'target': parent, 'kind': kind, 'state': 'absent_in_source', 'source_id': sid})
            else:
                gaps.append({'child': nid, 'target': parent, 'kind': kind,
                             'state': 'not_loaded' if parent in mapping else 'absent_in_source', 'source_id': sid})
    versions = [dict(r) for r in conn.execute('''SELECT c.source_id,s.dataset_id,s.path FROM conversations c
        JOIN sources s ON s.id=c.source_id WHERE c.conversation_id=? ORDER BY c.source_id DESC LIMIT 50''', (cid,))]
    return {'nodes': nodes, 'edges': edges, 'gaps': gaps, 'focus': focus, 'title': conv['title'],
            'source_id': source, 'conversation_id': cid, 'versions': versions, 'limit': limit,
            'total_active': len(active), 'loaded_active': len(chosen), 'remaining': len(active)-len(chosen),
            'ancestor_stop': ancestor_stop, 'ancestor_next': cur,
            'scope': 'Exact selected source. Overlay edges retain their own source IDs; proximity is layout only.'}
