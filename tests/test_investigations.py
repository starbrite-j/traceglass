import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

import app
import investigations as journal


def fixture(folder):
    mapping = {
        'root': {'id':'root','message':None,'parent':None,'children':['user']},
        'user': {'id':'user','parent':'root','children':['answer'], 'message':{
            'id':'user','author':{'role':'user'},'create_time':1753768114,
            'content':{'content_type':'text','parts':['Does the recorded parent link match?']},'metadata':{}}},
        'answer': {'id':'answer','parent':'user','children':[], 'message':{
            'id':'answer','author':{'role':'assistant'},'create_time':1753768115,
            'content':{'content_type':'text','parts':['Fixture answer — not archival evidence.']},
            'metadata':{'parent_id':'user','model_slug':'fixture'}}}
    }
    path=Path(folder)/'fixture.json'
    path.write_text(json.dumps([{'id':'fixture-conversation','title':'Workspace test fixture','mapping':mapping}]))
    app.import_paths([path])
    return path


class InvestigationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_db=app.DB_PATH
        self.addCleanup(setattr,app,'DB_PATH',self.old_db)
        app.DB_PATH=Path(self.tmp.name)/'test.sqlite3'
        app.init_db()
        self.source=fixture(self.tmp.name)
        self.original=self.source.read_bytes()
        with app.db() as conn:
            self.pk=conn.execute("SELECT pk FROM messages WHERE node_id='answer'").fetchone()[0]
            self.case=journal.create(conn,{'title':'Test','question':'Does the linkage agree?'})['id']

    def test_entries_revisions_and_immutable_original(self):
        with app.db() as conn:
            a=journal.add_entry(conn,self.case,{'kind':'hypothesis','text':'Maybe different','actor':'assistant'})
            b=journal.add_entry(conn,self.case,{'kind':'correction','text':'It matches in this source','revises':a['id']})
            data=journal.read(conn,self.case)
            self.assertEqual(data['investigation']['question'],'Does the linkage agree?')
            self.assertEqual(a['actor'],'user')
            self.assertEqual(b['payload']['revises'],a['id'])
            self.assertEqual([e['text'] for e in data['events']][1:],['Maybe different','It matches in this source'])
            other=journal.create(conn,{'title':'Other','question':'Separate'})['id']
            with self.assertRaises(ValueError):journal.add_entry(conn,other,{'kind':'note','text':'Cross-case edit','revises':a['id']})
        app.init_db()
        with app.db() as conn:self.assertEqual(len(journal.read(conn,self.case)['events']),3)
        self.assertEqual(self.source.read_bytes(),self.original)

    def test_pins_keep_server_resolved_provenance(self):
        with app.db() as conn:
            raw=conn.execute('SELECT raw_json FROM messages WHERE pk=?',(self.pk,)).fetchone()[0]
            e=journal.add_entry(conn,self.case,{'kind':'checkpoint','text':'Resume here','pk':self.pk,'source_id':999})
            ref=e['payload']['record']
            self.assertEqual(ref['node_id'],'answer')
            self.assertEqual(ref['source_path'],str(self.source.resolve()))
            self.assertNotEqual(ref['source_id'],999)
            self.assertTrue(ref['source_sha256'])
            self.assertEqual(conn.execute('SELECT raw_json FROM messages WHERE pk=?',(self.pk,)).fetchone()[0],raw)
            for pk in [None,True,'1',-1]:
                with self.assertRaises(ValueError):journal.record_reference(conn,pk)
            with self.assertRaises(LookupError):journal.record_reference(conn,999999)

    def test_event_and_case_pagination_no_loss(self):
        with app.db() as conn:
            for i in range(205):journal.add_entry(conn,self.case,{'kind':'note','text':str(i)})
            all_ids=[];before=None
            while True:
                d=journal.read(conn,self.case,before)
                self.assertLessEqual(len(d['events']),100)
                all_ids.extend(e['id'] for e in d['events'])
                before=d['before']
                if before is None:break
            self.assertEqual(len(all_ids),206);self.assertEqual(len(set(all_ids)),206)
            for i in range(101):journal.create(conn,{'title':str(i),'question':'Q'})
            first=journal.list_cases(conn);second=journal.list_cases(conn,first['before'])
            self.assertEqual(len(first['items'])+len(second['items']),102)

    def test_tools_are_explicit_bounded_reads_with_log(self):
        with app.db() as conn:
            readers={'trace_parents':lambda c,r:app.parent_trace_checks(c,r['source_id'],r['conversation_id'],r['node_id'])}
            result=journal.run_tool(conn,self.case,{'tool':'trace_parents','pk':self.pk},readers)
            self.assertTrue(result['ok'])
            self.assertEqual(result['event']['payload']['result']['outer']['stop']['reason'],'null_parent')
            self.assertEqual([e['kind'] for e in journal.read(conn,self.case)['events']],['question','tool_request','tool_result'])
            for tool in ['jev','shell','eval','https://example.com',{}]:
                with self.assertRaises(ValueError):journal.run_tool(conn,self.case,{'tool':tool,'pk':self.pk},readers)
            def fail(c,r):raise RuntimeError('test-only failure')
            result=journal.run_tool(conn,self.case,{'tool':'inspect_record','pk':self.pk},{'inspect_record':fail})
            self.assertFalse(result['ok'])
            self.assertEqual(journal.read(conn,self.case)['events'][-1]['kind'],'tool_error')
        self.assertEqual(self.source.read_bytes(),self.original)

    def test_validation(self):
        with app.db() as conn:
            for value in ['',None,123,'x'*161]:
                with self.assertRaises(ValueError):journal.create(conn,{'title':value,'question':'Question'})
            for kind in ['tool_result','system','unknown',None]:
                with self.assertRaises(ValueError):journal.add_entry(conn,self.case,{'kind':kind,'text':'test'})
            with self.assertRaises(LookupError):journal.read(conn,'missing')
            self.assertEqual(set(journal.read(conn,self.case)['connections'].values()),{'not_connected'})

    def test_http_endpoints_security_and_all_tools(self):
        server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        def request(method,path,body=None,headers=None):
            client=http.client.HTTPConnection('127.0.0.1',server.server_port)
            client.request(method,path,json.dumps(body) if body is not None else None,
                           headers or {'Content-Type':'application/json'})
            r=client.getresponse();status=r.status;data=json.loads(r.read());client.close()
            return status,data
        base='/api/investigations'
        status,c=request('POST',base,{'title':'HTTP','question':'Test'})
        self.assertEqual(status,201)
        for tool in sorted(journal.TOOLS):
            status,r=request('POST',f"{base}/{c['id']}/tools",{'tool':tool,'pk':self.pk})
            self.assertEqual(status,201);self.assertTrue(r['ok'])
        status,r=request('GET',f"{base}/{c['id']}")
        self.assertEqual(status,200);self.assertEqual(len(r['events']),7)
        for body,status in [([],400),({'title':'','question':'x'},400)]:
            self.assertEqual(request('POST',base,body)[0],status)
        self.assertEqual(request('POST',base,{}, {'Content-Type':'text/plain'})[0],415)
        self.assertEqual(request('POST',base,{}, {'Content-Type':'application/json','Origin':'https://attacker.test'})[0],403)
        self.assertEqual(request('POST',base,{}, {'Content-Type':'application/json','Host':'attacker.test'})[0],403)
        self.assertEqual(request('GET',base,headers={'Host':'attacker.test'})[0],403)
        self.assertEqual(request('GET',base+'/00000000')[0],404)


if __name__ == '__main__':
    import sys
    if '--serve-fixture' in sys.argv:
        with tempfile.TemporaryDirectory(prefix='traceglass-ui-test-') as folder:
            app.DB_PATH=Path(folder)/'fixture.sqlite3';app.init_db();fixture(folder)
            print('Isolated UI fixture: http://127.0.0.1:8770',flush=True)
            server=app.ThreadingHTTPServer(('127.0.0.1',8770),app.Handler)
            try:server.serve_forever()
            except KeyboardInterrupt:pass
            finally:server.server_close()
    else:unittest.main()
