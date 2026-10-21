"""Synthetic graph evidence only; never writes to the user's archive/database."""
import http.client
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import app
import constellation


def seed(conn, length=1250):
    conn.execute("INSERT INTO datasets(id,name,input_json,created_at) VALUES(1,'Constellation QA','[]','test')")
    for sid in (1, 2):
        conn.execute("INSERT INTO sources(id,dataset_id,path,kind,size_bytes,status,row_count) VALUES(?,1,?,'json',0,'done',?)", (sid,f'/fixture/copy-{sid}.json',length))
        conn.execute("INSERT INTO conversations(source_id,conversation_id,title,current_node,update_time) VALUES(?,'long','A thousand steps / continuity test',?,1)",(sid,f'n{length-1}'))
    def node(nid,parent,role='assistant',md=None,children=None,sid=1,cid='long',present=True):
        raw={'id':nid,'message':{'author':{'role':role},'metadata':md or {}}}
        if present: raw['parent']=parent
        conn.execute('''INSERT INTO messages(source_id,conversation_id,node_id,parent_id,children_json,role,
            content_text,display_time,metadata_json,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (sid,cid,nid,parent,json.dumps(children or []),role,'Synthetic fixture: '+nid,1753768114,json.dumps(md or {}),json.dumps(raw)))
    for i in range(length):
        parent=f'n{i-1}' if i else None
        md={'parent_id':parent}
        if i%80==0:md['is_visually_hidden_from_conversation']=True
        for sid in (1,2):
            node(f'n{i}',parent,'system' if i%80==0 else 'user' if i%2==0 else 'assistant',md,[f'n{i+1}'] if i<length-1 else [],sid)
    for j in range(8):
        cid=f'branch-{j}'
        conn.execute("INSERT INTO conversations(source_id,conversation_id,title,current_node,update_time) VALUES(1,?,?,?,?)",(cid,['Tool calls / reciprocal links','Hidden metadata / literal records','Voice timeline / source copies','The branching conversation','A record without a parent','Alternate version / comparison','Origins and continuations','Boundary conditions'][j],f'b{j}-39',-j))
        for i in range(40):node(f'b{j}-{i}',f'b{j}-{(i-1)//2}' if i else None,md={},sid=1,cid=cid)
    return node


class ConstellationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn=sqlite3.connect(':memory:')
        cls.conn.row_factory=sqlite3.Row
        cls.conn.executescript(app.SCHEMA)
        cls.add_node=staticmethod(seed(cls.conn))
    @classmethod
    def tearDownClass(cls):cls.conn.close()

    def graph(self,**q):
        return constellation.graph(self.conn,{k:[str(v)] for k,v in dict(source_id=1,conversation_id='long',focus='n1249',**q).items()})

    def test_atlas_scope_and_literal_search(self):
        d=constellation.atlas(self.conn,{'dataset_id':['1']})
        self.assertEqual(d['total'],10)
        self.assertTrue(all(n['pk'] for n in d['items']))
        self.assertEqual(constellation.atlas(self.conn,{'dataset_id':['1'],'source_id':['2']})['total'],1)
        self.assertEqual(constellation.atlas(self.conn,{'dataset_id':['1'],'q':['%']})['total'],0)

    def test_expansion_beyond_200_and_unloaded_is_not_missing(self):
        d=self.graph()
        self.assertEqual(d['loaded_active'],600)
        self.assertEqual(d['ancestor_stop'],'window_limit')
        self.assertTrue(any(g['state']=='not_loaded' for g in d['gaps']))
        d=self.graph(limit=1800)
        self.assertEqual(d['loaded_active'],1250)
        self.assertEqual(d['ancestor_stop'],'root')
        self.assertFalse(d['gaps'])
        self.assertTrue(any(n['hidden'] for n in d['nodes']))
        small=self.graph()
        full_positions={n['id']:n['layout_index'] for n in d['nodes']}
        self.assertTrue(all(n['layout_index']==full_positions[n['id']] for n in small['nodes']))

    def test_extended_trace_batches_reach_literal_root(self):
        ids=[];cur='n1249'
        while True:
            walk=app.parent_trace_checks(self.conn,1,'long',cur,500)['outer']
            ids.extend(n['node_id'] for n in walk['steps'])
            if walk['stop']['reason']!='limit':break
            cur=walk['stop']['node_id']
        self.assertEqual(len(ids),1250)
        self.assertEqual(len(set(ids)),1250)
        self.assertEqual(walk['stop']['reason'],'null_parent')

    def test_absent_invalid_cycle_and_other_copy_provenance(self):
        conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
        self.addCleanup(conn.close);conn.executescript(app.SCHEMA);node=seed(conn,2)
        node('absent',None,present=False)
        node('cycle','cycle')
        node('gap','only-other',md={'parent_id':'n0'})
        node('only-other',None,sid=2)
        node('gap','only-other',sid=2)
        def graph(focus,**extra):return constellation.graph(conn,{k:[str(v)] for k,v in dict(source_id=1,conversation_id='long',focus=focus,**extra).items()})
        self.assertEqual(graph('absent')['ancestor_stop'],'missing_field')
        self.assertEqual(graph('cycle')['ancestor_stop'],'cycle')
        d=graph('gap',compare_source=2)
        self.assertTrue(any(n['supplemental'] and n['id']=='only-other' for n in d['nodes']))
        self.assertTrue(any(g['state']=='absent_in_source' and g['source_id']==1 for g in d['gaps']))
        self.assertTrue(any(e['kind']=='other' and e['source_id']==2 for e in d['edges']))
        self.assertTrue(any(e['kind']=='metadata' and e['source_id']==1 for e in d['edges']))
        self.assertFalse(any(n['supplemental'] for n in graph('gap')['nodes']))

    def test_limits_and_invalid_inputs(self):
        self.assertEqual(self.graph(limit=999999)['limit'],6000)
        with self.assertRaises(ValueError):self.graph(limit='bad')
        with self.assertRaises(LookupError):constellation.graph(self.conn,{'source_id':['55']})

    def test_large_window_is_bounded_and_read_only(self):
        conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
        self.addCleanup(conn.close);conn.executescript(app.SCHEMA);seed(conn,6501)
        before=conn.total_changes
        d=constellation.graph(conn,{'source_id':['1'],'conversation_id':['long'],'focus':['n6500'],'limit':['999999']})
        self.assertEqual(len(d['nodes']),6000)
        self.assertEqual(d['remaining'],501)
        self.assertEqual(d['ancestor_stop'],'window_limit')
        self.assertEqual(conn.total_changes,before)


def serve_fixture():
    with tempfile.TemporaryDirectory(prefix='traceglass-constellation-') as folder:
        app.DB_PATH=Path(folder)/'fixture.sqlite3'
        app.init_db()
        with app.db() as conn:seed(conn)
        server=app.ThreadingHTTPServer((app.HOST,8771),app.Handler)
        print('Synthetic constellation fixture at http://127.0.0.1:8771',flush=True)
        try:server.serve_forever()
        finally:server.server_close()


if __name__=='__main__':
    if '--serve-fixture' in sys.argv:serve_fixture()
    else:unittest.main()
