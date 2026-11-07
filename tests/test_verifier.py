"""Adversarial inputs, HTTP boundaries and real process-crash recovery. Invented data only."""
import copy
import gzip
import hashlib
import http.client
import io
import json
import os
import random
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import operations
import relationships
import streaming


class VerifierTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.folder=Path(self.temporary.name)
        for key,value in [('DATA',self.folder/'state'),('DB_PATH',self.folder/'state/index.sqlite3')]:
            self.addCleanup(setattr,app,key,getattr(app,key));setattr(app,key,value)
        old=dict(app.JOB);self.addCleanup(app.JOB.update,old)
        app.init_db()
        self.source=self.folder/'invented.json'
        self.record={'title':'Invented conversation','mapping':{
            'node-a':{'id':'node-a','parent':None,'children':['node-b'],'message':{
                'id':'message-a','author':{'role':'user'},'status':'finished_successfully',
                'content':{'content_type':'text','parts':['invented café 日本語']},'metadata':{}}},
            'node-b':{'id':'node-b','parent':'node-a','children':[],'message':{
                'id':'message-b','author':{'role':'tool'},'status':'failed',
                'content':{'content_type':'system_error','text':'InventedError: intended fixture'},'metadata':{}}}},
            'id':'invented-conversation','current_node':'node-b'}

    def load(self, record=None):
        self.source.write_text(json.dumps([record or self.record],ensure_ascii=False),encoding='utf-8')
        return app.import_paths([self.source])

    def empty(self):
        with app.db(readonly=True) as c:
            for table in ('sources','messages','messages_fts','lineage_refs','lineage_events'):
                self.assertEqual(c.execute('SELECT count(*) FROM '+table).fetchone()[0],0,table)

    def server(self, **settings):
        server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        for key,value in settings.items():setattr(server,key,value)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        return server

    def request(self, server, path, headers=None):
        c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
        try:
            c.request('GET',path,headers=headers or {});r=c.getresponse()
            return r.status,dict(r.getheaders()),r.read()
        finally:c.close()

    def test_changed_or_replaced_source_is_rejected_and_rolls_back(self):
        insert=app.insert_message
        for replace in (False,True):
            self.source.write_text(json.dumps([self.record]))
            changed=False
            def mutate(*a,**kw):
                nonlocal changed
                result=insert(*a,**kw)
                if not changed:
                    changed=True
                    if replace:
                        replacement=self.folder/'replacement.json';replacement.write_text('[]');replacement.replace(self.source)
                    else:self.source.write_text('[]')
                return result
            with patch.object(app,'insert_message',side_effect=mutate):
                with self.assertRaises((ValueError,PermissionError)) as caught:app.import_paths([self.source])
                if isinstance(caught.exception,PermissionError):self.assertEqual(os.name,'nt')
                else:self.assertIn('Source changed',str(caught.exception))
            self.empty()

    def test_large_mapping_spills_and_preserves_metadata_after_mapping(self):
        with patch.object(streaming,'SPOOL_THRESHOLD',1):result=self.load()
        with app.db(readonly=True) as c:
            self.assertEqual(c.execute('SELECT title,current_node FROM conversations').fetchone()[:],('Invented conversation','node-b'))
            for r in c.execute('SELECT node_id,raw_json FROM messages'):
                self.assertEqual(json.loads(r['raw_json']),self.record['mapping'][r['node_id']])
            self.assertEqual(c.execute('SELECT sha256 FROM sources').fetchone()[0],app.sha256_file(self.source))
        self.assertEqual(result['rows'],2)

    def test_spool_disk_failure_closes_files_and_rolls_back(self):
        original=streaming.tempfile.TemporaryFile;files=[]
        def tracked():
            f=original();files.append(f);return f
        with patch.object(streaming,'SPOOL_THRESHOLD',1),patch.object(streaming.tempfile,'TemporaryFile',side_effect=tracked),patch.object(streaming.Mapping,'write',side_effect=OSError('invented disk full')):
            with self.assertRaisesRegex(OSError,'disk full'):self.load()
        self.assertTrue(files and all(f.closed for f in files));self.empty()

    def test_value_depth_expansion_and_metadata_budgets_fail_atomically(self):
        cases=[('MAX_VALUE_CHARS',40,[self.record]),('MAX_EXPANDED_CHARS',40,[self.record]),
               ('MAX_DEPTH',2,[self.record]),('MAX_MAPPING_KEYS',1,[self.record])]
        for setting,limit,value in cases:
            with self.subTest(setting=setting):
                self.source.write_text(json.dumps(value))
                with patch.object(streaming,setting,limit):
                    with self.assertRaises(ValueError):app.import_paths([self.source])
                self.empty()

    def test_compressed_expansion_budget_hashes_original_compressed_bytes(self):
        path=self.folder/'invented.json.gz'
        with gzip.open(path,'wt') as f:json.dump([self.record],f)
        original=path.read_bytes()
        with patch.object(streaming,'MAX_EXPANDED_CHARS',40):
            with self.assertRaisesRegex(ValueError,'decompressed'):app.import_paths([path])
        self.empty();app.import_paths([path])
        with app.db(readonly=True) as c:self.assertEqual(c.execute('SELECT sha256 FROM sources').fetchone()[0],hashlib.sha256(original).hexdigest())
        self.assertEqual(path.read_bytes(),original)

    def test_invalid_relationship_types_fail_instead_of_becoming_fake_ids(self):
        for field,value in [('parent',{}),('children','not an array'),('children',[{'id':'fake'}])]:
            record=copy.deepcopy(self.record);record['mapping']['node-b'][field]=value
            with self.subTest(field=field,value=value):
                with self.assertRaisesRegex(ValueError,'node.'+field):self.load(record)
                self.empty()

    def test_graph_does_not_decode_cached_large_raw_messages(self):
        self.load()
        with app.db(readonly=True) as c:
            pk=c.execute("SELECT pk FROM messages WHERE node_id='node-b'").fetchone()[0]
            with patch.object(relationships,'flags',side_effect=AssertionError('raw message decoding during cached graph read')):
                d=relationships.neighborhood(c,{'pk':[str(pk)]})
            self.assertTrue(next(n for n in d['nodes'] if n['pk']==pk)['error_reported'])

    def test_event_cache_invalidates_and_requires_repair_without_read_writes(self):
        self.load()
        with app.db() as c:
            pk=c.execute("SELECT pk FROM messages WHERE node_id='node-b'").fetchone()[0]
            c.execute("UPDATE messages SET status='finished_successfully' WHERE pk=?",(pk,))
            self.assertIsNone(c.execute('SELECT flags_json FROM lineage_events WHERE owner_pk=?',(pk,)).fetchone())
        with app.db(readonly=True) as c:
            before=c.total_changes
            with self.assertRaisesRegex(BlockingIOError,'needs repair'):relationships.neighborhood(c,{'pk':[str(pk)]})
            self.assertEqual(c.total_changes,before)

    def test_cross_site_reads_and_mutations_are_rejected_and_errors_have_headers(self):
        s=self.server()
        for headers in ({'Origin':'https://untrusted.test'},{'Origin':'null'},{'Sec-Fetch-Site':'cross-site'},{'Sec-Fetch-Site':'same-site'},{'Host':'untrusted.test'}):
            status,h,_=self.request(s,'/api/health',headers);self.assertEqual(status,403)
            self.assertEqual(h['X-Frame-Options'],'DENY');self.assertIn("frame-ancestors 'none'",h['Content-Security-Policy'])
        status,h,_=self.request(s,'/absent');self.assertEqual(status,404);self.assertEqual(h['Referrer-Policy'],'no-referrer')
        self.assertEqual(self.request(s,'/api/health',{'Sec-Fetch-Site':'none'})[0],200)

    def test_query_admission_preserves_health_and_returns_retryable_error(self):
        s=self.server()
        for _ in range(s.max_queries):s.query_slots.acquire()
        try:
            self.assertEqual(self.request(s,'/api/messages')[0],503)
            self.assertEqual(self.request(s,'/api/health')[0],200)
        finally:
            for _ in range(s.max_queries):s.query_slots.release()
        self.assertEqual(self.request(s,'/api/messages')[0],200)

    def test_request_framing_and_partial_body_are_rejected(self):
        s=self.server()
        for framing,body in [('Content-Length: 2\r\nContent-Length: 2',b'{}'),('Transfer-Encoding: chunked\r\nContent-Length: 2',b'{}'),('Content-Length: 20',b'{}')]:
            with socket.create_connection(('127.0.0.1',s.server_port),timeout=5) as c:
                header=f'POST /api/import HTTP/1.0\r\nHost: 127.0.0.1:{s.server_port}\r\nContent-Type: application/json\r\n{framing}\r\n\r\n'
                c.sendall(header.encode()+body);c.shutdown(socket.SHUT_WR)
                self.assertIn(b'400',c.recv(4096).split(b'\r\n',1)[0])

    def test_idle_connections_expire_and_admission_is_released(self):
        s=self.server(connection_timeout=.1)
        with socket.create_connection(('127.0.0.1',s.server_port),timeout=3) as c:
            c.sendall(b'GET /api/health HTTP/1.0\r\n')
            self.assertEqual(c.recv(4096),b'')
        self.assertEqual(self.request(s,'/api/health')[0],200)

    def test_logs_redact_queries_and_escape_terminal_controls(self):
        handler=object.__new__(app.Handler)
        with patch('sys.stderr',new_callable=io.StringIO) as out:
            handler.log_message('"%s" %s %s','GET /api/messages?q=invented-secret\x1b[31m HTTP/1.0',200,'-')
            result=out.getvalue()
        self.assertNotIn('invented-secret',result);self.assertNotIn('\x1b',result);self.assertIn('[redacted]',result)

    def test_raw_search_partial_scans_and_unicode_cache_keys_are_truthful(self):
        path=self.folder/'raw.json';path.write_text(json.dumps({'data':'É xx é '+('x'*10000)}),encoding='utf-8')
        with app.RAW_SEARCH_LOCK:app.RAW_SEARCH_CACHE.clear()
        with patch.object(app,'MAX_RAW_SCAN_BYTES',20):
            matches,scan=app._source_search_offsets(path,'absent')
        self.assertEqual(matches,[]);self.assertFalse(scan['scan_complete']);self.assertEqual(scan['stop_reason'],'byte_limit')
        path.write_text('{"upper":"É","lower":"é"}',encoding='utf-8')
        one,_=app._source_search_offsets(path,'É');two,_=app._source_search_offsets(path,'é')
        self.assertNotEqual(one,two)

    def test_backup_restores_committed_wal_notebook_and_refuses_overwrite(self):
        self.load()
        with app.db() as c:
            import investigations
            case=investigations.create(c,{'title':'Invented case','question':'Preserve evidence?'})
            investigations.add_entry(c,case['id'],{'kind':'evidence','text':'Invented pin','pk':1})
        backup=self.folder/'backup.sqlite3'
        with app.db(readonly=True) as c:
            index_id=operations.identity(c)
            result=operations.backup(c,backup)
            with self.assertRaises(FileExistsError):operations.backup(c,backup)
        restored=sqlite3.connect(backup);self.addCleanup(restored.close);restored.row_factory=sqlite3.Row
        self.assertEqual(operations.identity(restored),index_id)
        self.assertEqual(restored.execute('SELECT count(*) FROM investigation_events').fetchone()[0],2)
        self.assertEqual(restored.execute('SELECT count(*) FROM messages').fetchone()[0],2)
        self.assertEqual(result['sha256'],app.sha256_file(backup))
        self.assertEqual(operations.verify(restored,app.canonical_content_hash,relationships.flags,app.sha256_file,True)['status'],'passed')

    def test_readonly_verifier_finds_derived_and_source_tampering(self):
        self.load()
        with app.db(readonly=True) as c:
            before=c.total_changes;self.assertEqual(operations.verify(c,app.canonical_content_hash,relationships.flags,app.sha256_file,True)['status'],'passed');self.assertEqual(c.total_changes,before)
        with app.db() as c:c.execute("UPDATE messages SET content_text='invented tamper' WHERE pk=1")
        self.source.write_text('[]')
        with app.db(readonly=True) as c:
            d=operations.verify(c,app.canonical_content_hash,relationships.flags,app.sha256_file,True)
        self.assertEqual(d['status'],'failed');self.assertIn('source_checksum',{e['kind'] for e in d['errors']});self.assertIn('display_content_hash',{e['kind'] for e in d['errors']})

    def test_process_crash_rolls_back_file_and_recovers_durable_outcome(self):
        self.source.write_text(json.dumps([self.record]))
        code='''import os,sys
from pathlib import Path
import app
app.DATA=Path(sys.argv[1]);app.DB_PATH=app.DATA/'index.sqlite3';app.init_db()
original=app.insert_message
def die(*a,**kw):
 original(*a,**kw);os._exit(29)
app.insert_message=die
app.import_paths([sys.argv[2]])
'''
        p=subprocess.run([sys.executable,'-c',code,str(app.DATA),str(self.source)],cwd=app.ROOT,capture_output=True,timeout=15)
        self.assertEqual(p.returncode,29,p.stderr.decode())
        app.init_db();self.empty()
        with app.db(readonly=True) as c:self.assertEqual(operations.runs(c)[0]['state'],'interrupted')
        self.assertEqual(app.import_paths([self.source])['rows'],2)

    def test_other_process_cannot_take_import_lease(self):
        code='''import sys
from pathlib import Path
import operations
try:
 with operations.writer_lease(Path(sys.argv[1])):sys.exit(7)
except BlockingIOError:sys.exit(23)
'''
        with operations.writer_lease(app.DATA):
            p=subprocess.run([sys.executable,'-c',code,str(app.DATA)],cwd=app.ROOT,capture_output=True,timeout=10)
        self.assertEqual(p.returncode,23,p.stderr.decode())

    def test_decoder_differential_vectors_and_tiny_chunk_boundaries(self):
        rng=random.Random(20261106)
        decoder=json.JSONDecoder(object_pairs_hook=app.unique_json_object,parse_constant=app.reject_json_constant,parse_float=app.finite_json_float)
        def materialize(v):
            if isinstance(v,dict):return {k:materialize(x) for k,x in v.items()}
            if isinstance(v,list):return [materialize(x) for x in v]
            return v
        for i in range(400):
            value=copy.deepcopy(self.record)
            keys=list(value);rng.shuffle(keys);value={k:value[k] for k in keys}
            value['mapping']['node-a']['message']['content']['parts']=[rng.choice(['é','日本語','\\"\n\t','😀',str(rng.uniform(-1e8,1e8))])*rng.randrange(1,20)]
            wrapped={'items':[{'data':[value]}]} if i%3==0 else value
            text=json.dumps([wrapped],ensure_ascii=i%2==0,separators=(',',':'))
            self.source.write_text(text,encoding='utf-8')
            with patch.object(streaming,'CHUNK',17),patch.object(streaming,'SPOOL_THRESHOLD',1),streaming.Source(self.source,decoder) as source:
                actual=[materialize(x) for x in source.values()]
                self.assertEqual(actual,json.loads(text));self.assertEqual(source.verify(),app.sha256_file(self.source))

    def test_oversized_record_has_bounded_search_preview_and_detail_error(self):
        record=copy.deepcopy(self.record)
        record['mapping']['node-a']['message']['content']['parts']=['prefix '+('invented '*600000)+' frontierneedle']
        self.load(record)
        with app.db(readonly=True) as c:
            found=app.api_messages(c,{'q':['frontierneedle'],'search_mode':['text']})
            self.assertEqual(found['total'],1)
            self.assertIn('frontierneedle',found['items'][0]['content_preview'])
            self.assertTrue(found['items'][0]['passage_limited'])
            self.assertLessEqual(len(found['items'][0]['content_preview']),360)
            with self.assertRaises(app.ResourceLimitError):app.api_message(c,found['items'][0]['pk'])
        server=self.server();self.assertEqual(self.request(server,'/api/messages/1')[0],413)

    def test_shadowed_nested_mappings_remain_exact_in_conversation_metadata(self):
        record=copy.deepcopy(self.record);record['items']=[copy.deepcopy(self.record)]
        with patch.object(streaming,'SPOOL_THRESHOLD',1):self.load(record)
        with app.db(readonly=True) as c:
            stored=json.loads(c.execute('SELECT raw_json FROM conversations').fetchone()[0])
            self.assertEqual(stored,{k:v for k,v in record.items() if k!='mapping'})
            self.assertEqual(c.execute('SELECT count(*) FROM messages').fetchone()[0],2)

    def test_tool_attempt_survives_failure_before_outcome_and_recovers_once(self):
        self.load()
        import investigations
        with app.db() as c:case=investigations.create(c,{'title':'Invented','question':'Preserve interrupted attempt?'})
        def interrupted(*args):raise KeyboardInterrupt('invented interruption')
        with self.assertRaises(KeyboardInterrupt):
            with app.db() as c:investigations.run_tool(c,case['id'],{'tool':'inspect_record','pk':1},{'inspect_record':interrupted})
        app.init_db();app.init_db()
        with app.db(readonly=True) as c:
            events=investigations.read(c,case['id'])['events']
            requests=[e for e in events if e['kind']=='tool_request'];errors=[e for e in events if e['kind']=='tool_error']
            self.assertEqual(len(requests),1);self.assertEqual(len(errors),1)
            self.assertEqual(errors[0]['payload']['request_event_id'],requests[0]['id'])
            self.assertEqual(errors[0]['payload']['outcome'],'unrecorded_at_recovery')

    def test_input_byte_and_raw_reader_time_budgets_cannot_be_bypassed_by_gzip(self):
        self.source.write_text(json.dumps([self.record]))
        with patch.object(streaming,'MAX_SOURCE_BYTES',40):
            with self.assertRaisesRegex(ValueError,'byte budget'):app.import_paths([self.source])
        self.empty()
        reader=streaming.DigestReader(io.BytesIO(b'invented bytes'),-1)
        with self.assertRaisesRegex(ValueError,'elapsed-time'):reader.readinto(bytearray(4))

    def test_stat_and_fstat_timestamp_namespaces_are_compared_separately(self):
        from types import SimpleNamespace
        self.source.write_text(json.dumps([self.record]))
        decoder=json.JSONDecoder(object_pairs_hook=app.unique_json_object)
        with streaming.Source(self.source,decoder) as source:
            list(source.values())
            original=source.initial_path
            path_stat=SimpleNamespace(**{k:getattr(original,k) for k in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')})
            path_stat.st_ctime_ns+=123456
            source.initial_path=path_stat
            with patch.object(Path,'stat',return_value=path_stat):
                self.assertEqual(source.verify(),source.hashed.digest.hexdigest())

    def test_command_help_registers_all_subcommands_without_initialization(self):
        for args in (['--help'],['serve','--help'],['verify','--help'],['backup','--help']):
            with patch.object(app,'init_db',side_effect=AssertionError('Help initialized the index')),patch('sys.stdout',new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as result:app.main(args)
                self.assertEqual(result.exception.code,0)

    def test_verification_and_backup_commands_use_readonly_existing_index(self):
        self.load();destination=self.folder/'cli-backup.sqlite3'
        with patch.object(app,'init_db',side_effect=AssertionError('Read-only command initialized the index')):
            with patch('sys.stdout',new_callable=io.StringIO) as output:
                self.assertEqual(app.main(['verify','--sources']),0)
                self.assertEqual(json.loads(output.getvalue())['status'],'passed')
            with patch('sys.stdout',new_callable=io.StringIO) as output:
                self.assertEqual(app.main(['backup','--out',str(destination)]),0)
                self.assertEqual(json.loads(output.getvalue())['sha256'],app.sha256_file(destination))

    def test_real_serve_command_starts_and_reports_actual_bound_port(self):
        import queue
        code="""import sys
from pathlib import Path
import app
app.DATA=Path(sys.argv[1]);app.DB_PATH=app.DATA/'index.sqlite3'
app.main(['serve','--port','0'])
"""
        process=subprocess.Popen([sys.executable,'-u','-c',code,str(app.DATA)],cwd=app.ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        def stop():
            if process.poll() is None:process.terminate()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
            process.stdout.close();process.stderr.close()
        self.addCleanup(stop)
        ready=queue.Queue()
        threading.Thread(target=lambda:ready.put(process.stdout.readline()),daemon=True).start()
        line=ready.get(timeout=15)
        self.assertIn('Traceglass is running at http://127.0.0.1:',line)
        port=int(line.strip().rsplit(':',1)[1]);self.assertGreater(port,0)
        client=http.client.HTTPConnection('127.0.0.1',port,timeout=5)
        try:
            client.request('GET','/api/health');response=client.getresponse()
            self.assertEqual(response.status,200);self.assertTrue(json.loads(response.read())['ok'])
        finally:client.close()

    def test_wrapper_mapping_memory_budget_is_shared_and_spills(self):
        decoder=json.JSONDecoder(object_pairs_hook=app.unique_json_object)
        records=[]
        for i in range(20):
            record=copy.deepcopy(self.record);record['id']=f'invented-{i}';records.append(record)
        self.source.write_text(json.dumps({'items':records}))
        with patch.object(streaming,'SPOOL_THRESHOLD',1000),streaming.Source(self.source,decoder) as source:
            for item in source.values():
                self.assertLessEqual(source.mapping_budget[0],1000)
                self.assertTrue(any(r['mapping'].file for r in item['items']))
                self.assertEqual(sum(len(r['mapping']) for r in item['items']),40)

    def test_connection_admission_is_bounded_before_starting_worker(self):
        s=self.server();s.connection_slots.acquire()
        for _ in range(s.max_connections-1):s.connection_slots.acquire()
        try:self.assertEqual(self.request(s,'/api/health')[0],503)
        finally:
            for _ in range(s.max_connections):s.connection_slots.release()
        self.assertEqual(self.request(s,'/api/health')[0],200)

    def test_partial_dataset_after_process_crash_cannot_pass_verification(self):
        self.source.write_text(json.dumps([self.record]));second=self.folder/'second.json';second.write_text(self.source.read_text())
        code="""import os,sys
from pathlib import Path
import app
app.DATA=Path(sys.argv[1]);app.DB_PATH=app.DATA/'index.sqlite3';app.init_db()
original=app.insert_message
calls=0
def die(*a,**kw):
 global calls
 result=original(*a,**kw);calls+=1
 if calls==3:os._exit(29)
 return result
app.insert_message=die
app.import_paths(sys.argv[2:])
"""
        p=subprocess.run([sys.executable,'-c',code,str(app.DATA),str(self.source),str(second)],cwd=app.ROOT,capture_output=True,timeout=15)
        self.assertEqual(p.returncode,29,p.stderr.decode());app.init_db()
        with app.db(readonly=True) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM messages').fetchone()[0],2)
            self.assertEqual(app.api_stats(c)['import_state'],'interrupted')
            report=operations.verify(c,app.canonical_content_hash,relationships.flags,app.sha256_file,True)
            self.assertEqual(report['status'],'failed');self.assertIn('incomplete_dataset',{e['kind'] for e in report['errors']})

    def test_graph_matches_independent_breadth_first_oracle(self):
        rng=random.Random(971713)
        for case in range(50):
            conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
            try:
                conn.executescript(app.SCHEMA)
                conn.execute("INSERT INTO datasets VALUES(1,'invented','now','[]')")
                conn.execute("INSERT INTO sources(id,dataset_id,path,kind,size_bytes) VALUES(1,1,'invented','json',0)")
                adjacency={i:set() for i in range(20)};records=[];by_node={f'n{i}':i for i in range(20)}
                mids=[f'm{i//2}' for i in range(20)]
                for i in range(20):
                    parent=rng.randrange(20);meta=rng.randrange(20);children=rng.sample(range(20),2)
                    metadata={'parent_id':mids[meta] if i%2 else f'n{meta}'}
                    node={'id':f'n{i}','parent':f'n{parent}','children':[f'n{n}' for n in children],
                          'message':{'id':mids[i],'author':{'role':'assistant'},'metadata':metadata,'content':{'content_type':'text','parts':['invented']}}}
                    app.insert_message(conn,1,f'conversation-{i%3}','Invented',node,mapping_key=f'n{i}')
                    targets=[parent,*children]+([j for j in range(20) if mids[j]==mids[meta]] if i%2 else [meta])+[j for j in range(20) if mids[j]==mids[i]]
                    for j in targets:adjacency[i].add(j);adjacency[j].add(i)
                focus=rng.randrange(20);depth=rng.randrange(1,4);seen={focus};front={focus}
                for _ in range(depth):front=set().union(*(adjacency[n] for n in front))-seen if front else set();seen|=front
                graph=relationships.neighborhood(conn,{'pk':[str(focus+1)],'depth':[str(depth)]})
                self.assertEqual({int(n['node_id'][1:]) for n in graph['nodes']},seen,(case,focus,depth))
            finally:conn.close()

    def test_streaming_decoder_rejects_adversarial_syntax(self):
        bad=['[{},]','[,{}]','[{} {}]','[{}]{}','{"mapping":{"a":{},"a":{}}}',
             '{"mapping":{},"mapping":{}}','{"x":{"k":1,"k":2}}','[NaN]','[1e400]',
             '[truefalse]','[01]','["unterminated]','[{}\u00a0,{}]','{"items":[{},]}']
        decoder=json.JSONDecoder(object_pairs_hook=app.unique_json_object,parse_constant=app.reject_json_constant,parse_float=app.finite_json_float)
        for text in bad:
            self.source.write_text(text,encoding='utf-8')
            with self.subTest(text=text),streaming.Source(self.source,decoder) as source:
                with self.assertRaises(ValueError):list(source.values())


if __name__=='__main__':unittest.main()
