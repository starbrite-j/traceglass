"""Connected-history regressions: only invented records in temporary indexes."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import relationships


class RelationshipTests(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:');self.conn.row_factory=sqlite3.Row
        self.addCleanup(self.conn.close);self.conn.executescript(app.SCHEMA)
        self.conn.execute("INSERT INTO datasets VALUES(1,'invented','now','[]')")
        self.conn.execute("INSERT INTO datasets VALUES(2,'outside scope','now','[]')")
        for sid,dataset in [(1,1),(2,1),(3,2)]:
            self.conn.execute("INSERT INTO sources(id,dataset_id,path,kind,size_bytes) VALUES(?,?,?,'json',0)",(sid,dataset,f'/invented/{sid}.json'))

    def node(self,nid,parent=None,cid='alpha',sid=1,mid=None,md=None,children=None,role='assistant',status='finished_successfully',content=None):
        self.conn.execute("INSERT OR IGNORE INTO conversations(source_id,conversation_id,title,raw_json) VALUES(?,?,?,'{}')",(sid,cid,'Invented '+cid))
        node={'id':nid,'parent':parent,'children':children or [],'message':{
            'id':mid or nid,'author':{'role':role},'metadata':md or {},'status':status,
            'create_time':1750000000,'content':content or {'content_type':'text','parts':['invented '+nid]}}}
        app.insert_message(self.conn,sid,cid,'Invented '+cid,node,mapping_key=nid)
        return self.conn.execute('SELECT pk FROM messages WHERE source_id=? AND conversation_id=? AND node_id=?',(sid,cid,nid)).fetchone()[0]

    def graph(self,pk,**q):
        return relationships.neighborhood(self.conn,{k:[str(v)] for k,v in dict(pk=pk,**q).items()})

    def test_sibling_without_descendants_and_error_evidence_are_preserved(self):
        p=self.node('p',children=['a','b']);a=self.node('a','p',children=['c'])
        b=self.node('b','p',role='tool',status='failed');c=self.node('c','a')
        d=self.graph(c,depth=3)
        self.assertEqual({n['pk'] for n in d['nodes']},{p,a,b,c})
        sibling=next(n for n in d['nodes'] if n['pk']==b)
        self.assertTrue(sibling['terminal_in_dataset']);self.assertTrue(sibling['error_reported'])
        self.assertEqual(sibling['error_evidence'],[{'field':'message.status','value':'failed'}])
        self.assertFalse(next(n for n in d['nodes'] if n['pk']==c)['error_reported'])
        self.assertEqual({e['kind'] for e in d['edges']},{'outer','children'})

    def test_cross_chat_metadata_reference_and_shared_identity_keep_occurrences(self):
        origin=self.node('origin',cid='old')
        focus=self.node('reply',cid='new',md={'parent_id':'origin'})
        other=self.node('reply',cid='third',mid='reply',content={'content_type':'text','parts':['different source text']})
        outside=self.node('origin',cid='private',sid=3)
        d=self.graph(focus)
        self.assertEqual({n['pk'] for n in d['nodes']},{origin,focus,other})
        self.assertEqual(len({n['id'] for n in d['nodes']}),3)
        metadata=next(e for e in d['edges'] if e['kind']=='metadata')
        self.assertEqual((metadata['source'],metadata['target']),(relationships.key(origin),relationships.key(focus)))
        self.assertTrue(metadata['cross_conversation']);self.assertFalse(metadata['ambiguous'])
        identity=next(e for e in d['edges'] if e['kind']=='identity')
        self.assertFalse(identity['directed']);self.assertTrue(identity['content_differs'])
        compared=self.graph(focus,compare_dataset_id=2)
        self.assertIn(outside,{n['pk'] for n in compared['nodes']})

    def test_missing_pk_does_not_silently_select_first_record(self):
        self.node('first')
        with self.assertRaises(ValueError):relationships.neighborhood(self.conn,{})

    def test_message_namespace_does_not_replace_mapping_identity(self):
        target=self.node('mapping-key',mid='different-message-id',cid='origin')
        focus=self.node('child',md={'parent_id':'different-message-id'})
        d=self.graph(focus)
        edge=next(e for e in d['edges'] if e['kind']=='metadata')
        self.assertEqual(edge['target_namespace'],'message_id')
        self.assertEqual(edge['source'],relationships.key(target))
        self.assertEqual(next(n for n in d['nodes'] if n['pk']==target)['node_id'],'mapping-key')
        overview=relationships.basins(self.conn,{'dataset_id':['1']})
        self.assertTrue(any(e['kind']=='metadata' and e['record_pk']==focus for e in overview['edges']))

    def test_error_text_hint_and_interruption_are_not_verified_failures(self):
        pk=self.node('hint',role='tool',content={'content_type':'text','parts':['Traceback (most recent call last):\nInventedError: sample']})
        node=self.graph(pk)['nodes'][0]
        self.assertIsNotNone(node['error_text_hint']);self.assertFalse(node['error_reported'])
        cancelled=self.node('cancelled',status='cancelled')
        node=self.graph(cancelled)['nodes'][0]
        self.assertTrue(node['interrupted']);self.assertFalse(node['error_reported'])

    def test_ambiguous_ids_do_not_merge_roots_or_choose_a_unique_parent(self):
        one=self.node('reused-root',cid='one',mid='one-message')
        two=self.node('reused-root',cid='two',mid='two-message')
        focus=self.node('child',md={'parent_id':'reused-root'})
        d=self.graph(focus)
        edges=[e for e in d['edges'] if e['kind']=='metadata']
        self.assertEqual(len(edges),2)
        self.assertTrue(all(e['ambiguous'] and e['candidate_count']==2 for e in edges))
        self.assertEqual({e['source'] for e in edges},{relationships.key(one),relationships.key(two)})

    def test_terminal_flag_checks_dataset_not_only_loaded_window(self):
        root=self.node('root');child=self.node('child','root');self.node('grandchild','child')
        d=self.graph(root,depth=1)
        self.assertFalse(next(n for n in d['nodes'] if n['pk']==child)['terminal_in_dataset'])
        self.assertTrue(d['truncated']);self.assertGreater(d['unloaded_edge_count'],0)
        self.assertEqual(d['gaps'],[])

    def test_cycles_missing_targets_and_readonly_queries(self):
        focus=self.node('cycle','cycle',md={'parent_id':'absent'})
        before=self.conn.total_changes;d=self.graph(focus)
        self.assertEqual(len(d['nodes']),1);self.assertFalse(d['nodes'][0]['terminal_in_dataset'])
        self.assertTrue(any(e['source']==e['target'] for e in d['edges']))
        self.assertEqual(d['gaps'][0]['state'],'target_absent_in_dataset')
        self.assertEqual(self.conn.total_changes,before)

    def test_large_fanout_has_explicit_bounds(self):
        focus=self.node('parent')
        for i in range(160):self.node(f'child-{i}','parent')
        d=self.graph(focus,limit=10,depth=12)
        self.assertEqual(len(d['nodes']),10);self.assertTrue(d['truncated'])
        self.assertTrue(any(b['reason']=='incoming_reference_limit' for b in d['bounds']))
        self.assertTrue(all(g['state']!='target_absent_in_dataset' for g in d['gaps']))

    def test_identity_and_reference_indexes_track_updates_delete_and_backfill(self):
        owner=self.node('child','one',md={'parent_id':'two'},children=['three'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM lineage_refs').fetchone()[0],3)
        self.conn.execute("UPDATE messages SET parent_id='new' WHERE pk=?",(owner,))
        self.assertEqual(self.conn.execute("SELECT target_id FROM lineage_refs WHERE field='node.parent'").fetchone()[0],'new')
        self.conn.execute('DELETE FROM lineage_refs');relationships.backfill(self.conn)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM lineage_refs').fetchone()[0],3)
        self.conn.execute('DELETE FROM messages WHERE pk=?',(owner,))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM lineage_refs').fetchone()[0],0)

    def test_basin_bridges_match_literal_links_and_identity_not_shared_text(self):
        self.node('p',cid='origin');self.node('q',cid='new',md={'parent_id':'p'})
        self.node('shared',cid='origin');self.node('shared',cid='copy')
        self.node('unrelated',cid='separate',content={'content_type':'text','parts':['invented p']})
        d=relationships.basins(self.conn,{'dataset_id':['1']})
        self.assertEqual(len(d['nodes']),4)
        self.assertEqual({e['kind'] for e in d['edges']},{'metadata','identity'})
        separate=next(n['id'] for n in d['nodes'] if n['conversation_id']=='separate')
        self.assertFalse(any(separate in (e['source'],e['target']) for e in d['edges']))

    def test_snapshot_is_consistent_during_another_connection_commit(self):
        pk=self.node('before');self.conn.commit()
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'snapshot.sqlite3'
            reader=sqlite3.connect(path);reader.row_factory=sqlite3.Row
            self.conn.backup(reader);reader.execute('PRAGMA journal_mode=WAL')
            writer=sqlite3.connect(path)
            try:
                def query(conn,q):
                    before=conn.execute('SELECT status FROM messages WHERE pk=?',(pk,)).fetchone()[0]
                    writer.execute("UPDATE messages SET status='failed' WHERE pk=?",(pk,));writer.commit()
                    after=conn.execute('SELECT status FROM messages WHERE pk=?',(pk,)).fetchone()[0]
                    return before,after
                self.assertEqual(relationships.read_snapshot(reader,query,{}),('finished_successfully','finished_successfully'))
                self.assertFalse(reader.in_transaction)
                self.assertEqual(reader.execute('SELECT status FROM messages WHERE pk=?',(pk,)).fetchone()[0],'failed')
            finally:
                writer.close();reader.close()

    def test_snapshot_sql_deadline_releases_transaction_and_handler(self):
        self.conn.commit()
        def query(conn,q):
            return conn.execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<1000000) SELECT sum(x) FROM n').fetchone()
        with patch('relationships.time.monotonic',side_effect=[0,11]):
            with self.assertRaisesRegex(sqlite3.OperationalError,'interrupted'):
                relationships.read_snapshot(self.conn,query,{})
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.conn.execute('SELECT 1').fetchone()[0],1)

    def test_old_index_rebuild_and_search_repair_preserve_raw_hash_and_occurrence(self):
        pk=self.node('old-thought',parent='prior',content={'content_type':'thoughts','thoughts':[{'content':'inventedmigrationneedle'}]})
        before=tuple(self.conn.execute('SELECT pk,raw_json,canonical_content_hash FROM messages WHERE pk=?',(pk,)).fetchone())
        self.conn.execute("UPDATE messages SET content_text='' WHERE pk=?",(pk,))
        self.conn.execute('DELETE FROM messages_fts')
        for name in ('lineage_insert','lineage_update','lineage_delete'):
            self.conn.execute('DROP TRIGGER '+name)
        self.conn.execute('DROP TABLE lineage_refs');self.conn.execute('DROP TABLE lineage_index_state');self.conn.commit()
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'old.sqlite3';target=sqlite3.connect(path);self.conn.backup(target);target.close()
            with patch.object(app,'DATA',Path(folder)),patch.object(app,'DB_PATH',path):
                app.init_db();app.init_db()
                with app.db(readonly=True) as conn:
                    self.assertEqual(tuple(conn.execute('SELECT pk,raw_json,canonical_content_hash FROM messages WHERE pk=?',(pk,)).fetchone()),before)
                    self.assertEqual(conn.execute('SELECT count(*) FROM lineage_refs').fetchone()[0],1)
                    self.assertEqual(conn.execute("SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'inventedmigrationneedle'").fetchone()[0],pk)

    def test_thinking_text_becomes_searchable_and_remains_literal_in_raw(self):
        content={'content_type':'thoughts','thoughts':[{'summary':'invented thought label','content':'invented thought needle'}]}
        pk=self.node('thought',content=content)
        stored=self.conn.execute('SELECT content_text,raw_json FROM messages WHERE pk=?',(pk,)).fetchone()
        self.assertIn('invented thought needle',stored['content_text'])
        self.assertEqual(json.loads(stored['raw_json'])['message']['content'],content)
        self.assertTrue(self.graph(pk)['nodes'][0]['thinking'])


if __name__=='__main__':unittest.main()
