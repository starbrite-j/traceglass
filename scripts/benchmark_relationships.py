#!/usr/bin/env python3
"""Real invented JSON import and HTTP graph checks; optionally retain the demo."""
import argparse
import hashlib
import json
import platform
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import app
from benchmark_large_import import request, peak_rss


def generate(path,count):
    titles=['Recovery / failed attempt','Context bridge','Branch / alternative method','Tool execution / retries',
            'Exported thoughts','Branch / continuation','Investigation / results','Boundary / missing target',
            'Source comparison','Session / handoff','Sibling / stopped branch','Return / verification']
    per=(count+11)//12;written=0
    with path.open('x',encoding='utf-8') as stream:
        stream.write('[\n')
        for c in range(12):
            mapping={};size=min(per,count-written)
            for i in range(size):
                nid=f'fixture-c{c}-n{i}';parent=f'fixture-c{c}-n{(i-1)//2}' if i else None
                children=[f'fixture-c{c}-n{j}' for j in (2*i+1,2*i+2) if j<size]
                md={'synthetic':True,'request_id':f'invented-request-{c}-{i}'}
                if i%31==0:md['parent_id']=f'fixture-c{(c-1)%12}-n{i}'
                content={'content_type':'text','parts':[f'Invented event {c}/{i}. Inspect the declared relationships and the order of attempts.']}
                role='user' if i%5==0 else 'tool' if i%5==2 else 'assistant'
                status='failed' if i%37==2 else 'finished_successfully'
                if status=='failed':content={'content_type':'system_error','text':'Invented timeout: upstream operation did not complete.'}
                if i%23==4:content={'content_type':'thoughts','thoughts':[{'summary':'Invented exported summary','content':'Invented analysis record; preserve the original field and source.'}]}
                msg={'id':f'shared-invented-{c//2}-{i}' if i<min(20,size) else nid,'author':{'role':role},'metadata':md,
                     'status':status,'create_time':1750000000+i*12+c,'recipient':'python' if i%5==1 else 'all','content':content}
                mapping[nid]={'id':nid,'parent':parent,'children':children,'message':msg}
            if c:stream.write(',\n')
            stream.write(json.dumps({'id':f'fixture-conversation-{c}','title':titles[c],'mapping':mapping,
                                      'current_node':f'fixture-c{c}-n0'},ensure_ascii=False))
            written+=size
        stream.write('\n]')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',type=Path,required=True)
    parser.add_argument('--records',type=int,default=80000)
    parser.add_argument('--serve-port',type=int,default=0)
    args=parser.parse_args()
    if not 120<=args.records<=1000000:parser.error('--records must be 120–1000000')
    folder=args.workspace.resolve();folder.mkdir(parents=True,exist_ok=False)
    source=folder/'invented-connected.json';generate(source,args.records)
    before=app.sha256_file(source);app.DATA=folder/'state';app.DB_PATH=app.DATA/'traceglass.sqlite3';app.init_db()
    start=time.perf_counter();result=app.import_paths([source]);elapsed=time.perf_counter()-start
    server=app.ThreadingHTTPServer(('127.0.0.1',args.serve_port),app.Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start();port=server.server_port
    request(port,'/api/health')
    report={'started_utc':app.utc_now(),'platform':platform.platform(),'python':platform.python_version(),'sqlite':sqlite3.sqlite_version,'benchmark_sha256':app.sha256_file(Path(__file__)),'status':'running','records':args.records,'source_bytes':source.stat().st_size,'source_sha256':before,
            'import_seconds':elapsed,'app_sha256':app.sha256_file(ROOT/'app.py'),
            'relationships_sha256':app.sha256_file(ROOT/'relationships.py'),'checks':[]}
    try:
        with app.db(readonly=True) as conn:
            focus=conn.execute("SELECT pk FROM messages WHERE node_id='fixture-c0-n0'").fetchone()[0]
            assert conn.execute('SELECT count(*) FROM messages').fetchone()[0]==args.records
            assert conn.execute('PRAGMA quick_check').fetchone()[0]=='ok'
        for name,path in [('basins','/api/basins?dataset_id='+str(result['dataset_id'])),
                          ('neighborhood_300',f'/api/neighborhood?pk={focus}&limit=300&depth=4'),
                          ('neighborhood_900',f'/api/neighborhood?pk={focus}&limit=900&depth=6')]:
            d,seconds,response_bytes=request(port,path,timeout=30)
            assert d['nodes'] and d['edges']
            assert any(e['kind']=='metadata' for e in d['edges'])
            assert any(e['kind']=='identity' for e in d['edges'])
            if name.startswith('neighborhood'):
                assert len(d['nodes'])<=d['limit']
                assert len({n['id'] for n in d['nodes']})==len(d['nodes'])
                assert d['conversation_count']>1
                assert any(n['thinking'] for n in d['nodes'])
                assert any(n['error_reported'] for n in d['nodes'])
            assert seconds<10,(name,seconds)
            report['checks'].append({'name':name,'seconds':seconds,'response_bytes':response_bytes,
                'nodes':len(d['nodes']),'edges':len(d['edges']),'truncated':d['truncated']})
        after=app.sha256_file(source);assert before==after
        report.update(status='passed',source_unchanged=True,peak_rss_bytes=peak_rss())
    except Exception as exc:
        report.update(status='failed',error=f'{type(exc).__name__}: {exc}');raise
    finally:
        (folder/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(report),flush=True)
    if args.serve_port:
        print('Connected synthetic demo ready at http://127.0.0.1:'+str(port),flush=True)
        try:
            while True:time.sleep(1)
        except KeyboardInterrupt:pass
    server.shutdown();server.server_close()


if __name__=='__main__':main()
