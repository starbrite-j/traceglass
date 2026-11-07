#!/usr/bin/env python3
"""Replay an existing invented benchmark source with the current import decoder."""
import argparse
import json
import platform
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import app
import streaming
from benchmark_large_import import digest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',type=Path,required=True,help='Existing synthetic large-import benchmark workspace')
    parser.add_argument('--out',type=Path,required=True,help='New report file; never overwritten')
    args=parser.parse_args()
    if args.out.exists():parser.error('Report destination already exists')
    manifest=json.loads((args.workspace/'manifest.json').read_text())
    path=args.workspace/'invented-conversations.json'
    samples={v['node_id']:v['raw_sha256'] for v in manifest['samples']}
    records=conversations=verified=0;started=time.perf_counter()
    decoder=json.JSONDecoder(object_pairs_hook=app.unique_json_object,parse_constant=app.reject_json_constant,parse_float=app.finite_json_float)
    with streaming.Source(path,decoder) as source:
        for obj in source.values():
            for record in app.normalized_records(obj):
                assert record['kind']=='conversation','This replay program expects the invented conversation fixture'
                conversations+=1
                for nid,node in record['value']['mapping'].items():
                    records+=1
                    if nid in samples:
                        assert digest(node)==samples[nid],nid
                        verified+=1
        checksum=source.verify()
    assert checksum==manifest['sha256'] and records==manifest['records'] and conversations==manifest['conversations']
    assert verified==len(samples)
    report={'status':'passed','scope':'Full source decoding, consumed-byte SHA-256, stable source signature, counts and original node samples. No new SQLite import or HTTP timing is claimed.',
            'python':platform.python_version(),'platform':platform.platform(),'seconds':time.perf_counter()-started,
            'source_bytes':path.stat().st_size,'source_sha256':checksum,'records':records,'conversations':conversations,'raw_samples_verified':verified,
            'implementation_sha256':{n:app.sha256_file(ROOT/n) for n in ('app.py','streaming.py','scripts/benchmark_large_import.py')},
            'verifier_sha256':app.sha256_file(Path(__file__))}
    with args.out.open('x',encoding='utf-8') as f:json.dump(report,f,indent=2)
    print(json.dumps(report))


if __name__=='__main__':main()
