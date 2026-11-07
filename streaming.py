"""Bounded import decoding; spill large conversation mappings, hash consumed bytes."""
import hashlib
import io
import json
import os
import stat
import tempfile
import time
from contextlib import ExitStack

CHUNK = 1024 * 1024
MAX_VALUE_CHARS = 64 * 1024**2
MAX_SOURCE_BYTES = 20 * 1024**3
MAX_EXPANDED_CHARS = 20 * 1024**3
MAX_DEPTH = 128
MAX_IMPORT_SECONDS = 1800
MAX_MAPPING_KEYS = 1_000_000
SPOOL_THRESHOLD = 8 * 1024**2
SPACE = ' \t\r\n'


class Mapping(dict):
    """Private importer mapping; small conversations stay in memory."""
    def __init__(self, budget=None):
        self.budget=budget if budget is not None else [0]
        self.memory_size=0
        self.memory = {}
        self.offsets = {}
        self.size = 0
        self.file = None

    def close(self):
        if self.file:
            self.file.close()
        self.budget[0]-=self.memory_size
        self.memory_size=0
        self.memory.clear()

    def __len__(self):
        return len(self.offsets) if self.file else len(self.memory)

    def add(self, key, value, size):
        if key in (self.offsets if self.file else self.memory):
            raise ValueError(f'Duplicate JSON key {key!r}')
        if len(self) >= MAX_MAPPING_KEYS:
            raise ValueError('Conversation exceeds mapping-key budget')
        self.size += size
        if self.file:
            self.write(key,value)
            return
        self.memory[key]=value
        self.memory_size+=size
        self.budget[0]+=size
        if self.budget[0]>SPOOL_THRESHOLD:
            self.file=tempfile.TemporaryFile()
            for prior_key,prior_value in self.memory.items():
                self.write(prior_key,prior_value)
            self.memory.clear()
            self.budget[0]-=self.memory_size
            self.memory_size=0

    def write(self, key, value):
        raw = json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode('ascii')
        offset = self.file.tell()
        self.file.write(raw)
        self.offsets[key] = (offset, len(raw))

    def items(self):
        if self.file is None:
            yield from self.memory.items()
        else:
            for key, (offset, length) in self.offsets.items():
                self.file.seek(offset)
                yield key, json.loads(self.file.read(length))


class DigestReader(io.RawIOBase):
    def __init__(self, handle, deadline):
        self.handle, self.digest = handle, hashlib.sha256()
        self.deadline=deadline
        self.bytes_read=0

    def readable(self):
        return True

    def readinto(self, target):
        if time.monotonic()>self.deadline:raise ValueError("Source import exceeded elapsed-time budget")
        count = self.handle.readinto(target)
        self.bytes_read+=count
        if self.bytes_read>MAX_SOURCE_BYTES:raise ValueError("Source exceeds byte budget")
        self.digest.update(memoryview(target)[:count])
        return count


class Source:
    def __init__(self, path, decoder):
        self.path, self.decoder = path, decoder
        self.buf, self.pos, self.eof, self.expanded = '', 0, False, 0
        self.stack = ExitStack()
        self.files = self.stack
        self.metadata_chars = 0
        self.mapping_budget=[0]
        self.deadline=time.monotonic()+MAX_IMPORT_SECONDS

    def __enter__(self):
        import gzip
        try:
            # Reject devices/FIFOs before opening; nonblocking open also closes
            # the replacement race between stat and open for named pipes.
            if not stat.S_ISREG(self.path.stat().st_mode):
                raise ValueError('A regular source file is required')
            fd = os.open(self.path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
            raw = self.stack.enter_context(os.fdopen(fd, 'rb', buffering=0))
            self.initial = os.fstat(raw.fileno())
            if not stat.S_ISREG(self.initial.st_mode):
                raise ValueError('A regular source file is required')
            if self.initial.st_size>MAX_SOURCE_BYTES:raise ValueError("Source exceeds 20 GiB byte budget")
            self.raw = raw
            self.hashed = DigestReader(raw,self.deadline)
            binary = self.stack.enter_context(io.BufferedReader(self.hashed))
            if self.path.suffix.lower() == '.gz':
                binary = self.stack.enter_context(gzip.GzipFile(fileobj=binary, mode='rb'))
            self.text = self.stack.enter_context(io.TextIOWrapper(binary, encoding='utf-8-sig', errors='strict'))
            return self
        except BaseException:
            self.stack.close()
            raise

    def __exit__(self, *args):
        self.stack.close()
        self.files.close()

    def verify(self):
        if time.monotonic()>self.deadline:raise ValueError("Source import exceeded elapsed-time budget")
        if not self.eof:
            raise ValueError('Source was not consumed completely')
        def signature(s):
            return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns
        if signature(self.initial) != signature(os.fstat(self.raw.fileno())) or signature(self.initial) != signature(self.path.stat()):
            raise ValueError('Source changed during import; the file transaction was rolled back')
        return self.hashed.digest.hexdigest()

    def fill(self, needed=1):
        while not self.eof and len(self.buf)-self.pos < needed:
            if time.monotonic()>self.deadline:raise ValueError("Source import exceeded elapsed-time budget")
            if self.pos:
                self.buf, self.pos = self.buf[self.pos:], 0
            chunk = self.text.read(min(CHUNK, MAX_EXPANDED_CHARS-self.expanded+1))
            self.expanded += len(chunk)
            if self.expanded > MAX_EXPANDED_CHARS:
                raise ValueError('Source exceeds decompressed-character budget')
            if not chunk:
                self.eof = True
                break
            self.buf += chunk

    def peek(self):
        self.fill()
        return self.buf[self.pos:self.pos+1]

    def space(self):
        while self.peek() and self.peek() in SPACE:
            self.pos += 1

    def take(self, character):
        self.space()
        if self.peek() != character:
            raise ValueError('Expected JSON '+character)
        self.pos += 1

    def decoded(self, depth_offset=0):
        self.space()
        while True:
            try:
                value, end = self.decoder.raw_decode(self.buf, self.pos)
                size = end-self.pos
                if size > MAX_VALUE_CHARS:
                    raise ValueError('JSON value exceeds per-value character budget')
                if not self.eof and (end == len(self.buf) or (type(value) in (int,float) and self.buf[end:end+1] in ('.','e','E'))):
                    self.fill(size+1)
                    continue
                self.pos = end
                pending = [(value,depth_offset)]
                while pending:
                    item, depth = pending.pop()
                    if depth > MAX_DEPTH:
                        raise ValueError('JSON nesting exceeds depth budget')
                    if isinstance(item, dict):
                        pending.extend((v,depth+1) for v in item.values())
                    elif isinstance(item, list):
                        pending.extend((v,depth+1) for v in item)
                return value, size
            except RecursionError as exc:
                raise ValueError('JSON nesting exceeds decoder depth') from exc
            except json.JSONDecodeError:
                if self.eof:
                    raise
            available = len(self.buf)-self.pos
            if available > MAX_VALUE_CHARS:
                raise ValueError('JSON value exceeds per-value character budget')
            self.fill(min(MAX_VALUE_CHARS+1,max(1,available*2)))

    def metadata(self, size):
        self.metadata_chars += size
        if self.metadata_chars > MAX_VALUE_CHARS:
            raise ValueError('Record metadata exceeds character budget')

    def array(self, candidate, depth):
        self.take('[');self.space()
        if self.peek() == ']':
            self.pos += 1
            return
        while True:
            yield self.value(candidate, depth+1)
            self.space()
            if self.peek() == ']':
                self.pos += 1
                return
            self.take(',')

    def value(self, candidate, depth):
        if depth > MAX_DEPTH:
            raise ValueError('JSON nesting exceeds depth budget')
        self.space()
        if not candidate or self.peek() != '{':
            return self.decoded(depth)[0]
        self.take('{');self.space();result = {}
        if self.peek() == '}':
            self.pos += 1
            return result
        while True:
            key, key_size = self.decoded()
            self.metadata(key_size)
            if not isinstance(key,str):
                raise ValueError('JSON object key must be a string')
            if key in result:
                raise ValueError(f'Duplicate JSON key {key!r}')
            self.take(':');self.space()
            if key == 'mapping' and self.peek() == '{':
                mapping = Mapping(self.mapping_budget);self.stack.callback(mapping.close)
                self.take('{');self.space()
                if self.peek() != '}':
                    while True:
                        nid, key_size = self.decoded()
                        self.metadata(key_size)
                        if not isinstance(nid,str) or len(nid)>4096:
                            raise ValueError('Mapping key must be a string')
                        self.take(':');node,size = self.decoded(depth+2);mapping.add(nid,node,size)
                        self.space()
                        if self.peek() == '}':break
                        self.take(',')
                self.take('}');result[key] = mapping
            elif key in ('conversations','items','data') and self.peek() == '[':
                result[key] = list(self.array(True,depth+1))
            else:
                result[key], size = self.decoded(depth+1)
                self.metadata(size)
            self.space()
            if self.peek() == '}':
                self.pos += 1
                return result
            self.take(',')

    def values(self):
        self.space()
        if self.peek() == '[':
            # Release spool resources after each top-level conversation.
            self.take('[');self.space()
            if self.peek() == ']':self.pos += 1
            else:
                while True:
                    self.metadata_chars = 0
                    with ExitStack() as resources:
                        old=self.stack;self.stack=resources
                        try:yield self.value(True,1)
                        finally:self.stack=old
                    self.space()
                    if self.peek() == ']':self.pos += 1;break
                    self.take(',')
            self.space()
            if self.peek():raise ValueError('Unexpected data after top-level JSON array')
        else:
            while self.peek():
                self.metadata_chars = 0
                with ExitStack() as resources:
                    old=self.stack;self.stack=resources
                    try:yield self.value(True,0)
                    finally:self.stack=old
                if self.peek() and self.peek() not in SPACE:
                    raise ValueError('JSON records must be separated by whitespace')
                self.space()


def metadata_value(value):
    """Preserve shadowed nested mappings in stored conversation metadata."""
    budget=[0]
    def walk(item):
        if isinstance(item,Mapping):
            budget[0]+=item.size
            if budget[0]>MAX_VALUE_CHARS:
                raise ValueError('Nested conversation metadata exceeds materialization budget')
            return {k:walk(v) for k,v in item.items()}
        if isinstance(item,dict):return {k:walk(v) for k,v in item.items()}
        if isinstance(item,list):return [walk(v) for v in item]
        return item
    return walk(value)
