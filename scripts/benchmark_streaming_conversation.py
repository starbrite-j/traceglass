#!/usr/bin/env python3
"""Generate a large single conversation without first allocating that conversation."""
import argparse
import hashlib
import json
import platform
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import app
import operations
import relationships
from benchmark_large_import import peak_rss, request, digest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',type=Path,required=True)
    parser.add_argument('--records',type=int,default=11000)
    parser.add_argument('--chars',type=int,default=12000)
    args=parser.parse_args()
    if not 100<=args.records<=1000000 or not 100<=args.chars<=100000:parser.error('Invalid workload size')
    folder=args.workspace.resolve();folder.mkdir(parents=True,exist_ok=False)
    source=folder/'invented-single-conversation.json';samples={}
    with source.open('wb') as f:
        f.write(b'[{"mapping":{')
        for i in range(args.records):
            nid=f'invented-stream-{i}'
            text=(f'inventedstreammarker{i:012d} '+('Invented evidence preservation café 日本語. '*(args.chars//30+1)))[:args.chars]
            node={'id':nid,'parent':f'invented-stream-{i-1}' if i else None,
                  'children':[f'invented-stream-{i+1}'] if i<args.records-1 else [],
                  'message':{'id':nid,'author':{'role':'assistant'},'create_time':1750000000+i,
                             'status':'finished_successfully','content':{'content_type':'text','parts':[text]},'metadata':{'synthetic':True}}}
            if i in (0,args.records//2,args.records-1):samples[nid]=digest(node)
            if i:f.write(b',')
            f.write((json.dumps(nid)+':'+app.json_compact(node)).encode('utf-8'))
        # Metadata after mapping tests legal field order as well as memory use.
        f.write(b'},"id":"invented-single-conversation","title":"Invented streaming stress","current_node":'+json.dumps(f'invented-stream-{args.records-1}').encode()+b'}]')
    checksum=app.sha256_file(source);app.DATA=folder/'state';app.DB_PATH=app.DATA/'index.sqlite3';app.init_db()
    report={'status':'running','platform':platform.platform(),'python':platform.python_version(),'sqlite':sqlite3.sqlite_version,
            'source_bytes':source.stat().st_size,'source_sha256':checksum,'records':args.records,'conversations':1,
            'implementation_sha256':{n:app.sha256_file(ROOT/n) for n in ('app.py','streaming.py','relationships.py','operations.py','local_http.py','constellation.py','investigations.py','static/app.js','static/constellation.js','static/constellation-worker.js')},
            'benchmark_sha256':app.sha256_file(Path(__file__))}
    server=None
    try:
        start=time.perf_counter();result=app.import_paths([source]);report['import_seconds']=time.perf_counter()-start
        report['import_peak_rss_bytes']=peak_rss()
        with app.db(readonly=True) as c:
            assert result['rows']==args.records
            for nid,expected in samples.items():
                row=c.execute('SELECT pk,raw_json FROM messages WHERE node_id=?',(nid,)).fetchone()
                assert digest(json.loads(row['raw_json']))==expected
            focus=row['pk'];report['raw_sample_hashes_verified']=len(samples)
            c.execute('BEGIN')
            report['verification']=operations.verify(c,app.canonical_content_hash,relationships.flags,app.sha256_file,True)
            assert report['verification']['status']=='passed'
        server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        graph,seconds,size=request(server.server_port,f'/api/neighborhood?pk={focus}&depth=12&limit=900')
        assert graph['nodes'] and len(graph['nodes'])<=900
        report['connected_http']={'seconds':seconds,'response_bytes':size,'nodes':len(graph['nodes']),'edges':len(graph['edges'])}
        assert report['import_peak_rss_bytes']<256*1024**2,report['import_peak_rss_bytes']
        assert report['import_seconds']<120,report['import_seconds']
        assert app.sha256_file(source)==checksum
        report.update(status='passed',source_unchanged=True,peak_rss_bytes=peak_rss())
    except Exception as exc:
        report.update(status='failed',error=f'{type(exc).__name__}: {exc}');raise
    finally:
        if server:server.shutdown();server.server_close()
        (folder/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(report),flush=True)


if __name__=='__main__':main()
