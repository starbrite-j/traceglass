"""Single-writer leases, durable import outcomes, read-only verification and backup."""
import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS index_identity(singleton INTEGER PRIMARY KEY CHECK(singleton=1),id TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS import_runs (
 id TEXT PRIMARY KEY, dataset_id INTEGER, started_at TEXT NOT NULL,
 finished_at TEXT, state TEXT NOT NULL, phase TEXT NOT NULL,
 input_json TEXT NOT NULL, error TEXT, rows_imported INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_import_run_dataset ON import_runs(dataset_id,started_at);
"""


@contextmanager
def writer_lease(folder):
    folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    if os.name!='nt':os.chmod(folder,0o700)
    fd=os.open(folder/'.import.lock',os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
    locked=False
    try:
        if os.name=='nt':
            import msvcrt
            if os.fstat(fd).st_size==0:os.write(fd,b'0')
            os.lseek(fd,0,os.SEEK_SET)
            try:msvcrt.locking(fd,msvcrt.LK_NBLCK,1)
            except OSError as exc:raise BlockingIOError('Another process owns the import/migration lease') from exc
        else:
            import fcntl
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError as exc:raise BlockingIOError('Another process owns the import/migration lease') from exc
        locked=True
        yield
    finally:
        if locked:
            if os.name=='nt':
                os.lseek(fd,0,os.SEEK_SET);msvcrt.locking(fd,msvcrt.LK_UNLCK,1)
            else:fcntl.flock(fd,fcntl.LOCK_UN)
        os.close(fd)


def identity(conn):
    row=conn.execute('SELECT id FROM index_identity WHERE singleton=1').fetchone()
    return row[0] if row else None


def recover(conn, stamp):
    conn.execute('INSERT OR IGNORE INTO index_identity VALUES(1,?)',(str(uuid.uuid4()),))
    return conn.execute("UPDATE import_runs SET state='interrupted',phase='interrupted',finished_at=?,error='Process ended before recording completion; reconciliation may be incomplete' WHERE state='running'",(stamp,)).rowcount


def begin(conn, dataset, paths, stamp):
    run=str(uuid.uuid4())
    conn.execute('INSERT INTO import_runs(id,dataset_id,started_at,state,phase,input_json) VALUES(?,?,?,\'running\',\'reading\',?)',
                 (run,dataset,stamp,json.dumps([str(p) for p in paths])))
    return run


def finish(conn, run, state, stamp, rows, error=None):
    conn.execute('UPDATE import_runs SET state=?,phase=?,finished_at=?,rows_imported=?,error=? WHERE id=?',
                 (state,state,stamp,rows,error,run))


def runs(conn, dataset=0):
    return [dict(r) for r in conn.execute('SELECT * FROM import_runs WHERE (?=0 OR dataset_id=?) ORDER BY started_at DESC LIMIT 20',(dataset,dataset))]


def verify(conn, content_hash, flags, hash_file, check_sources=False):
    """Never initialize, migrate or repair the inspected index."""
    report={'index_id':identity(conn),'status':'passed','sqlite_integrity':[], 'foreign_key_errors':[], 'errors':[], 'records_checked':0,'sources':[]}
    report['sqlite_integrity']=[r[0] for r in conn.execute('PRAGMA integrity_check')]
    report['foreign_key_errors']=[tuple(r) for r in conn.execute('PRAGMA foreign_key_check')]
    def fail(kind,pk=None):
        if len(report['errors'])<100:report['errors'].append({'kind':kind,'pk':pk})
        report['status']='failed'
    if report['sqlite_integrity']!=['ok'] or report['foreign_key_errors']:fail('database_integrity')
    for r in conn.execute('SELECT m.*,e.flags_json FROM messages m LEFT JOIN lineage_events e ON e.owner_pk=m.pk ORDER BY m.pk'):
        report['records_checked']+=1
        try:
            raw=json.loads(r['raw_json']);content=((raw.get('message') or {}).get('content') or {})
            msg=raw.get('message') or {};author=msg.get('author') or {}
            if r['status']!=msg.get('status') or r['role']!=author.get('role') or r['message_id']!=msg.get('id') or r['parent_id']!=raw.get('parent') or json.loads(r['metadata_json'])!=(msg.get('metadata') or {}) or json.loads(r['children_json'])!=(raw.get('children') or []):fail('derived_fields',r['pk'])
            if hashlib.sha256((r['content_text'] or '').encode('utf-8','replace')).hexdigest()!=r['content_hash']:fail('display_content_hash',r['pk'])
            if content_hash(content)!=r['canonical_content_hash']:fail('canonical_content_hash',r['pk'])
            if r['flags_json'] is None or json.loads(r['flags_json'])!=flags(r):fail('event_summary',r['pk'])
        except (ValueError,TypeError,AttributeError):fail('invalid_record_json',r['pk'])
    # Compare reference multisets in SQL, retaining ordinal and literal types.
    missing=conn.execute("""WITH expected(owner_pk,field,ordinal,target_id) AS (
      SELECT pk,'node.parent',-1,parent_id FROM messages WHERE typeof(parent_id)='text' AND parent_id<>'' UNION ALL
      SELECT pk,'message.metadata.parent_id',-1,json_extract(metadata_json,'$.parent_id') FROM messages WHERE json_type(metadata_json,'$.parent_id')='text' AND json_extract(metadata_json,'$.parent_id')<>'' UNION ALL
      SELECT m.pk,'node.children',CAST(j.key AS INTEGER),j.value FROM messages m,json_each(m.children_json) j WHERE j.type='text' AND j.value<>'')
      SELECT count(*) FROM (SELECT * FROM expected EXCEPT SELECT * FROM lineage_refs)""").fetchone()[0]
    actual=conn.execute('SELECT count(*) FROM lineage_refs').fetchone()[0]
    expected=conn.execute("SELECT (SELECT count(*) FROM messages WHERE typeof(parent_id)='text' AND parent_id<>'')+(SELECT count(*) FROM messages WHERE json_type(metadata_json,'$.parent_id')='text' AND json_extract(metadata_json,'$.parent_id')<>'')+(SELECT count(*) FROM messages m,json_each(m.children_json) j WHERE j.type='text' AND j.value<>'')").fetchone()[0]
    if missing or actual!=expected:fail('reference_index')
    fts=conn.execute('SELECT count(*) FROM messages_fts').fetchone()[0]
    orphan=conn.execute('SELECT count(*) FROM messages_fts f LEFT JOIN messages m ON m.pk=f.rowid WHERE m.pk IS NULL').fetchone()[0]
    if fts!=report['records_checked'] or orphan:fail('full_text_membership')
    mismatch=conn.execute('SELECT count(*) FROM messages_fts f JOIN messages m ON m.pk=f.rowid WHERE f.content_text<>m.content_text OR f.node_id<>m.node_id OR f.conversation_id<>m.conversation_id').fetchone()[0]
    if mismatch:fail('full_text_content')
    incomplete=conn.execute("SELECT count(*) FROM import_runs WHERE state<>'complete'").fetchone()[0]
    report['incomplete_import_runs']=incomplete
    pending=conn.execute("SELECT count(*) FROM import_runs r WHERE r.state<>'complete' AND EXISTS(SELECT 1 FROM sources s WHERE s.dataset_id=r.dataset_id)").fetchone()[0]
    report['incomplete_datasets']=pending
    if pending:fail('incomplete_dataset')
    if check_sources:
        for r in conn.execute('SELECT id,path,sha256 FROM sources ORDER BY id'):
            try:matched=hash_file(Path(r['path']))==r['sha256']
            except OSError:matched=False
            report['sources'].append({'source_id':r['id'],'matches_imported_sha256':matched})
            if not matched:fail('source_checksum',r['id'])
    report['scope']='Read-only structural consistency, derived event/reference checks and optional source hashes; not an independent original-record comparison or a tamper-proof signature.'
    return report


def backup(conn, destination):
    """SQLite backup API includes committed WAL state; never copy a live DB file."""
    destination=Path(destination).expanduser().resolve()
    if destination.exists():raise FileExistsError('Backup destination already exists')
    destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=destination.with_name('.'+destination.name+'.partial-'+uuid.uuid4().hex)
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(fd)
    try:
        target=sqlite3.connect(temporary)
        try:
            deadline=time.monotonic()+300
            def progress(*args):
                if time.monotonic()>deadline:raise TimeoutError('Backup exceeded 300-second budget')
            conn.backup(target,pages=256,progress=progress)
            if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Backup integrity check failed')
        finally:target.close()
        with temporary.open('r+b') as f:
            digest=hashlib.sha256()
            while chunk:=f.read(1024**2):digest.update(chunk)
            os.fsync(f.fileno())
        # Publishing via link is atomic and refuses an existing destination.
        os.link(temporary,destination)
        return {'status':'passed','path':str(destination),'sha256':digest.hexdigest(),'bytes':destination.stat().st_size,
                'scope':'SQLite snapshot includes index and notebook; source archives must be backed up separately.'}
    finally:temporary.unlink(missing_ok=True)
