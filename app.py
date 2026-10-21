#!/usr/bin/env python3
from __future__ import annotations

import argparse
import difflib
import gzip
import hashlib
import json
import math
import mimetypes
import os
import re
import shutil
import sqlite3
import sys
import threading
import tempfile
import time
import traceback
import urllib.parse
import investigations
import constellation
from contextlib import contextmanager
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable, Iterator

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DATA = ROOT / "data"
SAMPLE_DIR = ROOT / "sample-data"
DB_PATH = DATA / "traceglass.sqlite3"
HOST = "127.0.0.1"
PORT = 8765

JOB_LOCK = threading.Lock()
JOB: dict[str, Any] = {
    "state": "idle", "label": "", "current": 0, "total": 0,
    "detail": "", "started_at": None, "finished_at": None, "error": None,
}
RAW_SEARCH_CACHE: dict[tuple[str, float, str], list[int]] = {}
UPLOAD_LOCK = threading.Lock()
MAX_UPLOAD_BYTES = 20 * 1024**3


def save_upload(stream: Any, name: str, size: int) -> Path:
    """Stream a selected file into a private local directory; never replace a source."""
    if not 0 < size <= MAX_UPLOAD_BYTES:
        raise ValueError("Choose a nonempty file up to 20 GiB, or use Local export path")
    if not name or name != Path(name).name or "/" in name or "\\" in name:
        raise ValueError("A filename without directory components is required")
    if not name.lower().endswith((".json", ".jsonl", ".ndjson", ".gz")):
        raise ValueError("Choose JSON, JSONL, NDJSON, or gzip; extract ZIP exports first")
    folder = DATA / "uploads"
    folder.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(folder).free < size + 100 * 1024**2:
        raise ValueError("Insufficient free disk space for this local copy")
    directory = Path(tempfile.mkdtemp(prefix="source-", dir=folder))
    # Keep user-supplied names out of filesystem paths; retain just the format suffix.
    suffix = ".json.gz" if name.lower().endswith(".gz") else Path(name).suffix.lower()
    path = directory / ("export" + suffix)
    try:
        with path.open("xb") as output:
            remaining = size
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("File transfer ended early; incomplete copy removed")
                output.write(chunk)
                remaining -= len(chunk)
        return path
    except Exception:
        path.unlink(missing_ok=True)
        directory.rmdir()
        raise


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  input_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dataset_summaries (
  dataset_id INTEGER PRIMARY KEY REFERENCES datasets(id) ON DELETE CASCADE,
  totals_json TEXT NOT NULL,
  roles_json TEXT NOT NULL,
  rules_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cross_event_state (
  conversation_id TEXT NOT NULL,
  node_id TEXT NOT NULL,
  dataset_count INTEGER NOT NULL,
  visible_versions INTEGER NOT NULL,
  min_omitted INTEGER NOT NULL,
  max_omitted INTEGER NOT NULL,
  PRIMARY KEY(conversation_id,node_id)
);
CREATE TABLE IF NOT EXISTS cross_content_variants (
  conversation_id TEXT NOT NULL,
  node_id TEXT NOT NULL,
  visible_text_hash TEXT NOT NULL,
  dataset_count INTEGER NOT NULL,
  canonical_versions INTEGER NOT NULL,
  PRIMARY KEY(conversation_id,node_id,visible_text_hash)
);
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY,
  dataset_id INTEGER REFERENCES datasets(id) ON DELETE SET NULL,
  path TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  sha256 TEXT,
  modified_at REAL,
  imported_at TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  row_count INTEGER NOT NULL DEFAULT 0,
  error TEXT
);
CREATE INDEX IF NOT EXISTS ix_sources_dataset ON sources(dataset_id,id);
CREATE TABLE IF NOT EXISTS conversations (
  pk INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  conversation_id TEXT NOT NULL,
  title TEXT,
  create_time REAL,
  update_time REAL,
  current_node TEXT,
  raw_json TEXT,
  UNIQUE(source_id, conversation_id)
);
CREATE TABLE IF NOT EXISTS messages (
  pk INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  conversation_id TEXT NOT NULL,
  node_id TEXT NOT NULL,
  message_id TEXT,
  parent_id TEXT,
  children_json TEXT NOT NULL DEFAULT '[]',
  role TEXT,
  author_name TEXT,
  create_time REAL,
  display_time REAL,
  timestamp_status TEXT NOT NULL DEFAULT 'missing',
  update_time REAL,
  content_type TEXT,
  status TEXT,
  recipient TEXT,
  channel TEXT,
  model_slug TEXT,
  default_model_slug TEXT,
  request_id TEXT,
  turn_exchange_id TEXT,
  finish_type TEXT,
  stop_tokens TEXT,
  content_text TEXT,
  content_hash TEXT,
  visible_text_hash TEXT,
  canonical_content_hash TEXT,
  metadata_json TEXT,
  raw_json TEXT NOT NULL,
  source_locator_json TEXT,
  chronological_sequence INTEGER,
  UNIQUE(source_id, conversation_id, node_id)
);
CREATE INDEX IF NOT EXISTS ix_messages_time ON messages(create_time, pk);
CREATE INDEX IF NOT EXISTS ix_messages_conv ON messages(source_id, conversation_id);
CREATE INDEX IF NOT EXISTS ix_messages_parent ON messages(source_id, conversation_id, parent_id);
CREATE INDEX IF NOT EXISTS ix_messages_dataset_node ON messages(conversation_id,node_id,source_id);
CREATE INDEX IF NOT EXISTS ix_messages_dataset_parent ON messages(conversation_id,parent_id,source_id);
CREATE INDEX IF NOT EXISTS ix_messages_mid ON messages(message_id);
CREATE INDEX IF NOT EXISTS ix_messages_mid_hash ON messages(message_id,content_hash);
CREATE INDEX IF NOT EXISTS ix_messages_request ON messages(request_id);
CREATE INDEX IF NOT EXISTS ix_messages_role ON messages(role);
CREATE TABLE IF NOT EXISTS edges (
  source_id INTEGER NOT NULL,
  conversation_id TEXT NOT NULL,
  parent_id TEXT NOT NULL,
  child_id TEXT NOT NULL,
  declared_by TEXT NOT NULL,
  PRIMARY KEY(source_id, conversation_id, parent_id, child_id, declared_by)
);
CREATE INDEX IF NOT EXISTS ix_edges_child ON edges(source_id, conversation_id, child_id);
CREATE TABLE IF NOT EXISTS anomalies (
  id INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL,
  conversation_id TEXT NOT NULL,
  node_id TEXT NOT NULL,
  rule TEXT NOT NULL,
  severity TEXT NOT NULL,
  title TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  UNIQUE(source_id, conversation_id, node_id, rule)
);
CREATE INDEX IF NOT EXISTS ix_anomaly_node ON anomalies(source_id, conversation_id, node_id);
CREATE INDEX IF NOT EXISTS ix_anomaly_rule ON anomalies(rule, severity);
CREATE TABLE IF NOT EXISTS annotations (
  source_id INTEGER NOT NULL,
  conversation_id TEXT NOT NULL,
  node_id TEXT NOT NULL,
  review_state TEXT NOT NULL DEFAULT 'unreviewed',
  note TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL,
  PRIMARY KEY(source_id, conversation_id, node_id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  content_text, title, node_id UNINDEXED, conversation_id UNINDEXED
);
"""


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)
        conn.executescript(investigations.SCHEMA)
        source_columns = {row[1] for row in conn.execute("PRAGMA table_info(sources)")}
        if "dataset_id" not in source_columns:
            conn.execute("ALTER TABLE sources ADD COLUMN dataset_id INTEGER REFERENCES datasets(id) ON DELETE SET NULL")
        message_columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        if "display_time" not in message_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN display_time REAL")
        if "timestamp_status" not in message_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN timestamp_status TEXT NOT NULL DEFAULT 'missing'")
        if "visible_text_hash" not in message_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN visible_text_hash TEXT")
        if "canonical_content_hash" not in message_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN canonical_content_hash TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_messages_mid_visible_hash "
            "ON messages(message_id,visible_text_hash)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_messages_mid_canonical_hash "
            "ON messages(message_id,canonical_content_hash)"
        )
        pending = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE visible_text_hash IS NULL "
            "OR canonical_content_hash IS NULL"
        ).fetchone()[0]
        while pending:
            rows = conn.execute(
                "SELECT pk,raw_json FROM messages WHERE visible_text_hash IS NULL "
                "OR canonical_content_hash IS NULL LIMIT 2000"
            ).fetchall()
            if not rows:
                break
            updates = []
            for row in rows:
                try:
                    node = json.loads(row["raw_json"])
                except (TypeError, json.JSONDecodeError):
                    node = {}
                content = ((node.get("message") or {}).get("content") or {})
                visible_hash = hashlib.sha256(
                    visible_content_text(content).encode("utf-8", "replace")
                ).hexdigest()
                updates.append((visible_hash, canonical_content_hash(content), row["pk"]))
            conn.executemany(
                "UPDATE messages SET visible_text_hash=?,canonical_content_hash=? WHERE pk=?",
                updates,
            )
            conn.commit()
            pending -= len(rows)
        conn.execute("""
          UPDATE messages SET
            display_time=CASE
              WHEN create_time BETWEEN 946684800 AND 4102444800 THEN create_time
              WHEN create_time BETWEEN 946684800000 AND 4102444800000 THEN create_time/1000.0
              ELSE NULL END,
            timestamp_status=CASE
              WHEN create_time IS NULL THEN 'missing'
              WHEN create_time BETWEEN 946684800 AND 4102444800 THEN 'seconds'
              WHEN create_time BETWEEN 946684800000 AND 4102444800000 THEN 'milliseconds_normalized'
              ELSE 'out_of_range' END
          WHERE display_time IS NULL OR timestamp_status='missing'
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS ix_messages_display_time ON messages(display_time,pk)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_messages_source_display "
            "ON messages(source_id,display_time,pk)"
        )
        repair_context_search(conn)


def repair_context_search(conn: sqlite3.Connection) -> int:
    """Repair derived search/display text only; preserve raw records and canonical hashes."""
    rows = conn.execute("""
        SELECT m.pk,m.raw_json,m.content_text,m.node_id,m.conversation_id,c.title
        FROM messages m LEFT JOIN conversations c
          ON c.source_id=m.source_id AND c.conversation_id=m.conversation_id
        WHERE m.content_type='user_editable_context'
    """).fetchall()
    changed = 0
    for row in rows:
        raw = json.loads(row['raw_json'])
        content = (raw.get('message') or {}).get('content') or {}
        text = flatten_content(content)
        if text == (row['content_text'] or ''):
            continue
        conn.execute('UPDATE messages SET content_text=?,content_hash=? WHERE pk=?',
                     (text, hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest(), row['pk']))
        conn.execute('DELETE FROM messages_fts WHERE rowid=?', (row['pk'],))
        conn.execute('INSERT INTO messages_fts(rowid,content_text,title,node_id,conversation_id) VALUES (?,?,?,?,?)',
                     (row['pk'],text,row['title'] or '',row['node_id'],row['conversation_id']))
        changed += 1
    return changed


def json_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def hidden_metadata_occurrences(metadata: Any) -> list[dict[str, Any]]:
    """Return every metadata key/value occurrence containing ``hidden``.

    Message content is intentionally out of scope. Paths begin at
    ``message.metadata`` so the UI cannot confuse a word in conversation text
    with a field or value supplied in metadata.
    """
    hits: list[dict[str, Any]] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}"
                key_hit = "hidden" in str(key).lower()
                value_hit = isinstance(child, str) and "hidden" in child.lower()
                if key_hit or value_hit:
                    hits.append({
                        "path": child_path,
                        "key": str(key),
                        "value": child if isinstance(child, (str, int, float, bool)) or child is None else child,
                        "match": "key_and_value" if key_hit and value_hit else ("key" if key_hit else "value"),
                    })
                walk(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    walk(metadata if isinstance(metadata, (dict, list)) else {}, "message.metadata")
    return hits


def hidden_metadata_occurrences_in_record(record: Any) -> list[dict[str, Any]]:
    """Find hidden mentions inside every object explicitly named metadata."""
    hits: list[dict[str, Any]] = []

    def find_metadata(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                if str(key).lower() == "metadata":
                    scoped = hidden_metadata_occurrences(child)
                    prefix = child_path
                    for hit in scoped:
                        relative = str(hit["path"]).removeprefix("message.metadata")
                        hits.append(dict(hit) | {"path": prefix + relative})
                else:
                    find_metadata(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                find_metadata(child, f"{path}[{index}]")

    find_metadata(record, "")
    return hits


def scalar_time(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("epoch")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def normalized_time(value: Any) -> tuple[float | None, float | None, str]:
    """Return literal numeric value, sortable/display seconds, and its classification."""
    raw = scalar_time(value)
    if raw is None:
        return None, None, "missing"
    if 946_684_800 <= raw <= 4_102_444_800:
        return raw, raw, "seconds"
    if 946_684_800_000 <= raw <= 4_102_444_800_000:
        return raw, raw / 1000.0, "milliseconds_normalized"
    return raw, None, "out_of_range"


def flatten_content(content: Any) -> str:
    if not isinstance(content, dict):
        return ""
    if content.get('content_type') == 'user_editable_context':
        # These are labeled context fields, not ordinary user utterances.
        return '\n\n'.join(f'{key}:\n{content[key]}'
                           for key in ('user_profile', 'user_instructions')
                           if isinstance(content.get(key), str) and content[key])
    parts = content.get("parts")
    if isinstance(parts, list):
        out: list[str] = []
        for part in parts:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict):
                if isinstance(part.get("text"), str):
                    out.append(part["text"])
                else:
                    out.append(json_compact(part))
            elif part is not None:
                out.append(str(part))
        return "\n".join(out)
    if isinstance(content.get("text"), str):
        return content["text"]
    return ""


def visible_content_text(content: Any) -> str:
    """Extract human-readable utterance text without serializing asset metadata."""
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts")
    if isinstance(parts, list):
        out: list[str] = []
        for part in parts:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict):
                for key in ("text", "transcript", "transcription", "content"):
                    if isinstance(part.get(key), str):
                        out.append(part[key])
                        break
        return "\n".join(out)
    for key in ("text", "content", "transcript", "transcription"):
        if isinstance(content.get(key), str):
            return content[key]
    return ""


def canonical_content_hash(content: Any) -> str:
    payload = json.dumps(
        content if isinstance(content, dict) else {}, ensure_ascii=False,
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while data := f.read(chunk):
            h.update(data)
    return h.hexdigest()


def open_text(path: Path):
    if path.suffix.lower() == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="strict")
    return path.open("rt", encoding="utf-8", errors="strict")


def unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key {key!r}; import stopped without selecting a version")
        result[key] = value
    return result


def iter_json_values(path: Path) -> Iterator[Any]:
    """Bound the consumed buffer; memory still scales with the largest JSON value.

    Array separators/termination are strict. Outside arrays, whitespace-separated
    JSON values support NDJSON and single pretty-printed objects.
    """
    decoder = json.JSONDecoder(object_pairs_hook=unique_json_object)
    with open_text(path) as f:
        buf = ""
        pos = 0
        eof = False

        def compact() -> None:
            nonlocal buf, pos
            if pos >= 1024 * 1024:
                buf = buf[pos:]
                pos = 0

        def fill(min_available: int = 1) -> None:
            nonlocal buf, eof
            while not eof and len(buf) - pos < min_available:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    eof = True
                    break
                buf += chunk

        def skip_space() -> None:
            nonlocal pos
            while True:
                fill()
                while pos < len(buf) and buf[pos].isspace():
                    pos += 1
                if pos < len(buf) or eof:
                    return
                compact()

        def value() -> Any:
            nonlocal pos
            while True:
                try:
                    obj, end = decoder.raw_decode(buf, pos)
                    # A number or literal may straddle a read boundary.
                    if not eof and (end == len(buf) or
                                    (type(obj) in (int, float) and buf[end:end+1] in ('.', 'e', 'E'))):
                        fill(len(buf) - pos + 1)
                        continue
                    pos = end
                    return obj
                except json.JSONDecodeError:
                    if eof:
                        raise
                    fill(len(buf) - pos + 1)

        def finish_array() -> None:
            nonlocal pos
            pos += 1
            skip_space()
            if pos < len(buf):
                raise ValueError("Unexpected data after top-level JSON array")

        fill()
        skip_space()
        if pos >= len(buf):
            return
        first = buf[pos]
        if first == "[":
            pos += 1
            skip_space()
            if pos < len(buf) and buf[pos] == "]":
                finish_array()
                return
            while True:
                compact()
                skip_space()
                yield value()
                skip_space()
                if pos >= len(buf):
                    raise ValueError("Unclosed top-level JSON array")
                if buf[pos] == "]":
                    finish_array()
                    return
                if buf[pos] != ",":
                    raise ValueError("Expected comma between JSON array entries")
                pos += 1
                # The next value() rejects a trailing comma or absent entry.
        else:
            while True:
                compact()
                skip_space()
                if eof and pos >= len(buf):
                    return
                yield value()
                if pos < len(buf) and not buf[pos].isspace():
                    raise ValueError("JSON records must be separated by whitespace")


def candidate_files(inputs: Iterable[str | Path]) -> list[Path]:
    out: list[Path] = []
    seen: set[Path] = set()
    for raw in inputs:
        path = Path(raw).expanduser().resolve()
        found: Iterable[Path]
        if path.is_dir():
            found = sorted(
                p for p in path.rglob("*")
                if p.is_file() and (
                    p.name.endswith(".json") or p.name.endswith(".ndjson")
                    or p.name.endswith(".json.gz") or p.name.endswith(".ndjson.gz")
                )
            )
        else:
            found = [path]
        for p in found:
            if p.exists() and p not in seen:
                seen.add(p)
                out.append(p)
    return out


def normalized_records(obj: Any) -> Iterator[dict[str, Any]]:
    """Yield one normalized conversation wrapper or chronological record."""
    if not isinstance(obj, dict):
        return
    if isinstance(obj.get("mapping"), dict):
        yield {"kind": "conversation", "value": obj}
    elif obj.get("reader_schema") and isinstance(obj.get("mapping_node"), dict):
        yield {"kind": "chronological", "value": obj}
    else:
        for key in ("conversations", "items", "data"):
            value = obj.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict) and isinstance(item.get("mapping"), dict):
                        yield {"kind": "conversation", "value": item}
                break


def insert_message(
    conn: sqlite3.Connection, source_id: int, conversation_id: str,
    title: str, node: dict[str, Any], source_locator: Any = None,
    chronological_sequence: int | None = None,
) -> int:
    msg = node.get("message") or {}
    node_id = str(node.get("id") or msg.get("id") or f"missing-{source_id}-{time.time_ns()}")
    message_id = msg.get("id")
    author = msg.get("author") or {}
    metadata = msg.get("metadata") or {}
    finish = metadata.get("finish_details") or {}
    content = msg.get("content") or {}
    text = flatten_content(content)
    content_hash = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    visible_text_hash = hashlib.sha256(
        visible_content_text(content).encode("utf-8", "replace")
    ).hexdigest()
    exact_content_hash = canonical_content_hash(content)
    children = node.get("children") if isinstance(node.get("children"), list) else []
    create_time, display_time, timestamp_status = normalized_time(msg.get("create_time"))
    values = (
        source_id, conversation_id, node_id, message_id, node.get("parent"),
        json_compact(children), author.get("role"), author.get("name"),
        create_time, display_time, timestamp_status, scalar_time(msg.get("update_time")),
        content.get("content_type"), msg.get("status"), msg.get("recipient"),
        msg.get("channel"), metadata.get("model_slug"), metadata.get("default_model_slug"),
        metadata.get("request_id"), metadata.get("turn_exchange_id"), finish.get("type"),
        json_compact(finish.get("stop_tokens")) if finish.get("stop_tokens") is not None else None,
        text, content_hash, visible_text_hash, exact_content_hash,
        json_compact(metadata), json_compact(node),
        json_compact(source_locator) if source_locator is not None else None,
        chronological_sequence,
    )
    conn.execute("""
      INSERT INTO messages(
        source_id,conversation_id,node_id,message_id,parent_id,children_json,role,author_name,
        create_time,display_time,timestamp_status,update_time,content_type,status,recipient,channel,model_slug,default_model_slug,
        request_id,turn_exchange_id,finish_type,stop_tokens,content_text,content_hash,
        visible_text_hash,canonical_content_hash,metadata_json,
        raw_json,source_locator_json,chronological_sequence
      ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, values)
    row = conn.execute(
        "SELECT pk FROM messages WHERE source_id=? AND conversation_id=? AND node_id=?",
        (source_id, conversation_id, node_id),
    ).fetchone()
    pk = int(row[0])
    conn.execute("DELETE FROM messages_fts WHERE rowid=?", (pk,))
    conn.execute(
        "INSERT INTO messages_fts(rowid,content_text,title,node_id,conversation_id) VALUES (?,?,?,?,?)",
        (pk, text, title or "", node_id, conversation_id),
    )
    parent = node.get("parent")
    if parent:
        conn.execute(
            "INSERT OR IGNORE INTO edges VALUES (?,?,?,?,?)",
            (source_id, conversation_id, str(parent), node_id, "child.parent"),
        )
    for child in children:
        conn.execute(
            "INSERT OR IGNORE INTO edges VALUES (?,?,?,?,?)",
            (source_id, conversation_id, node_id, str(child), "parent.children"),
        )
    return 1


def import_file(path: Path, dataset_id: int) -> int:
    stat = path.stat()
    kind = "gzip" if path.suffix == ".gz" else "json"
    with db() as conn:
        if conn.execute("SELECT 1 FROM sources WHERE path=?", (str(path),)).fetchone():
            raise ValueError("This source path is already indexed. Same-path replacement is disabled in the preview. Keep a new export at a distinct path to retain both versions.")
        conn.execute(
            "INSERT INTO sources(dataset_id,path,kind,size_bytes,modified_at,status) VALUES(?,?,?,?,?,?)",
            (dataset_id, str(path), kind, stat.st_size, stat.st_mtime, "importing"),
        )
        source_id = int(conn.execute("SELECT id FROM sources WHERE path=?", (str(path),)).fetchone()[0])
        count = 0
        for obj in iter_json_values(path):
            for record in normalized_records(obj):
                value = record["value"]
                if record["kind"] == "conversation":
                    conversation_id = str(value.get("id") or value.get("conversation_id") or f"conversation-{count}")
                    title = value.get("title") or "Untitled conversation"
                    conn.execute("""
                      INSERT INTO conversations(
                        source_id,conversation_id,title,create_time,update_time,current_node,raw_json
                      ) VALUES(?,?,?,?,?,?,?)
                    """, (source_id, conversation_id, title, scalar_time(value.get("create_time")),
                           scalar_time(value.get("update_time")), value.get("current_node"),
                           json_compact({k: v for k, v in value.items() if k != "mapping"})))
                    for node_id, node in (value.get("mapping") or {}).items():
                        if not isinstance(node, dict):
                            continue
                        if node.get("id") and node["id"] != node_id:
                            raise ValueError(f"Mapping key {node_id!r} differs from embedded node.id {node['id']!r}; this preview cannot index both identities safely")
                        if not node.get("id"):
                            node = dict(node)
                            node["id"] = str(node_id)
                        count += insert_message(conn, source_id, conversation_id, title, node)
                else:
                    conv = value.get("conversation") or {}
                    conversation_id = str(conv.get("conversation_id") or "unknown")
                    title = conv.get("title") or "Untitled conversation"
                    conn.execute("""
                      INSERT OR IGNORE INTO conversations(source_id,conversation_id,title,raw_json)
                      VALUES(?,?,?,?)
                    """, (source_id, conversation_id, title, json_compact(conv)))
                    node = dict(value.get("mapping_node") or {})
                    node["message"] = value.get("message")
                    count += insert_message(
                        conn, source_id, conversation_id, title, node,
                        value.get("source_locator"), value.get("chronological_sequence"),
                    )
                if count and count % 1000 == 0:
                    with JOB_LOCK:
                        JOB["detail"] = f"{path.name}: {count:,} records"
                    # Keep this file atomic. A later parse failure must not
                    # commit deletion/replacement of its previous indexed copy.
        source_status = "ready" if count else "skipped"
        source_error = None if count else "No supported conversation or chronological records found"
        conn.execute(
            "UPDATE sources SET sha256=?,imported_at=?,status=?,row_count=?,error=? WHERE id=?",
            (sha256_file(path), utc_now(), source_status, count, source_error, source_id),
        )
        conn.commit()
    return count


def add_anomaly(conn: sqlite3.Connection, row: sqlite3.Row, rule: str,
                severity: str, title: str, evidence: dict[str, Any]) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO anomalies(source_id,conversation_id,node_id,rule,severity,title,evidence_json) "
        "VALUES(?,?,?,?,?,?,?)",
        (row["source_id"], row["conversation_id"], row["node_id"], rule, severity, title, json_compact(evidence)),
    )


def refresh_dataset_summary(conn: sqlite3.Connection, dataset_id: int) -> None:
    totals = dict(conn.execute("""
      SELECT
        (SELECT COUNT(*) FROM sources WHERE dataset_id=:dataset_id AND status='ready') sources,
        (SELECT COUNT(DISTINCT m.conversation_id) FROM messages m JOIN sources s ON s.id=m.source_id
         WHERE s.dataset_id=:dataset_id) conversations,
        (SELECT COUNT(*) FROM messages m JOIN sources s ON s.id=m.source_id
         WHERE s.dataset_id=:dataset_id) messages,
        (SELECT COUNT(*) FROM (
           SELECT m.conversation_id,m.node_id FROM messages m JOIN sources s ON s.id=m.source_id
           WHERE s.dataset_id=:dataset_id GROUP BY m.conversation_id,m.node_id
         )) unique_events,
        (SELECT COUNT(*) FROM anomalies a JOIN sources s ON s.id=a.source_id
         WHERE s.dataset_id=:dataset_id) anomalies,
        (SELECT MIN(m.display_time) FROM messages m JOIN sources s ON s.id=m.source_id
         WHERE s.dataset_id=:dataset_id) min_time,
        (SELECT MAX(m.display_time) FROM messages m JOIN sources s ON s.id=m.source_id
         WHERE s.dataset_id=:dataset_id) max_time
    """, {"dataset_id": dataset_id}).fetchone())
    totals["duplicate_event_copies"] = totals["messages"] - totals["unique_events"]
    totals["events_seen_elsewhere"] = conn.execute("""
      SELECT COUNT(*) FROM messages m JOIN sources s ON s.id=m.source_id
      JOIN cross_event_state x ON x.conversation_id=m.conversation_id AND x.node_id=m.node_id
      WHERE s.dataset_id=? AND x.dataset_count>1
    """, (dataset_id,)).fetchone()[0]
    totals["conflicting_events_elsewhere"] = conn.execute("""
      SELECT COUNT(*) FROM messages m JOIN sources s ON s.id=m.source_id
      JOIN cross_event_state x ON x.conversation_id=m.conversation_id AND x.node_id=m.node_id
      WHERE s.dataset_id=? AND m.message_id IS NOT NULL AND x.visible_versions>1
        AND COALESCE(json_extract(m.raw_json,'$.message.content.content_omitted'),0)=0
    """, (dataset_id,)).fetchone()[0]
    totals["variant_events_elsewhere"] = conn.execute("""
      SELECT COUNT(*) FROM messages m JOIN sources s ON s.id=m.source_id
      JOIN cross_content_variants v ON v.conversation_id=m.conversation_id
        AND v.node_id=m.node_id AND v.visible_text_hash=m.visible_text_hash
      WHERE s.dataset_id=? AND m.message_id IS NOT NULL
        AND COALESCE(json_extract(m.raw_json,'$.message.content.content_omitted'),0)=0
    """, (dataset_id,)).fetchone()[0]
    totals["omitted_events_elsewhere"] = conn.execute("""
      SELECT COUNT(*) FROM messages m JOIN sources s ON s.id=m.source_id
      JOIN cross_event_state x ON x.conversation_id=m.conversation_id AND x.node_id=m.node_id
      WHERE s.dataset_id=? AND m.message_id IS NOT NULL AND x.min_omitted<>x.max_omitted
    """, (dataset_id,)).fetchone()[0]
    roles = [dict(row) for row in conn.execute("""
      SELECT COALESCE(m.role,'unknown') value,COUNT(*) count
      FROM messages m JOIN sources s ON s.id=m.source_id
      WHERE s.dataset_id=? GROUP BY m.role ORDER BY count DESC
    """, (dataset_id,))]
    rules = [dict(row) for row in conn.execute("""
      SELECT a.rule,a.severity,a.title,COUNT(*) count
      FROM anomalies a JOIN sources s ON s.id=a.source_id
      WHERE s.dataset_id=? GROUP BY a.rule,a.severity,a.title ORDER BY count DESC
    """, (dataset_id,))]
    conn.execute("""
      INSERT INTO dataset_summaries(dataset_id,totals_json,roles_json,rules_json,updated_at)
      VALUES(?,?,?,?,?) ON CONFLICT(dataset_id) DO UPDATE SET
        totals_json=excluded.totals_json,roles_json=excluded.roles_json,
        rules_json=excluded.rules_json,updated_at=excluded.updated_at
    """, (dataset_id, json_compact(totals), json_compact(roles), json_compact(rules), utc_now()))


def analyze_dataset(dataset_id: int) -> None:
    """Rebuild markers only after every source in a dataset is available."""
    with db() as conn:
        conn.execute(
            "DELETE FROM anomalies WHERE source_id IN "
            "(SELECT id FROM sources WHERE dataset_id=?)", (dataset_id,)
        )

        def insert_rule(rule: str, severity: str, title: str,
                        select_sql: str, params: tuple[Any, ...]) -> None:
            conn.execute(f"""
              INSERT OR IGNORE INTO anomalies(
                source_id,conversation_id,node_id,rule,severity,title,evidence_json
              )
              SELECT source_id,conversation_id,node_id,?,?,?,evidence_json
              FROM ({select_sql})
            """, (rule, severity, title, *params))

        scope = "JOIN sources s ON s.id=m.source_id WHERE s.dataset_id=?"
        insert_rule("missing_create_time", "high", "Missing message timestamp", f"""
          SELECT m.source_id,m.conversation_id,m.node_id,
                 json_object('message.create_time',NULL) evidence_json
          FROM messages m {scope} AND m.create_time IS NULL
        """, (dataset_id,))
        insert_rule("timestamp_unit_milliseconds", "medium", "Millisecond timestamp normalized for display", f"""
          SELECT m.source_id,m.conversation_id,m.node_id,
                 json_object('message.create_time.raw',m.create_time,
                             'display_time.seconds',m.display_time,
                             'normalization','raw value divided by 1000') evidence_json
          FROM messages m {scope} AND m.timestamp_status='milliseconds_normalized'
        """, (dataset_id,))
        insert_rule("timestamp_out_of_range", "high", "Timestamp cannot be rendered safely", f"""
          SELECT m.source_id,m.conversation_id,m.node_id,
                 json_object('message.create_time.raw',m.create_time,
                             'display_time',NULL) evidence_json
          FROM messages m {scope} AND m.timestamp_status='out_of_range'
        """, (dataset_id,))
        insert_rule("node_message_id_mismatch", "high", "Node and message IDs differ", f"""
          SELECT m.source_id,m.conversation_id,m.node_id,
                 json_object('mapping_node.id',m.node_id,'message.id',m.message_id) evidence_json
          FROM messages m {scope} AND m.message_id IS NOT NULL AND m.message_id<>m.node_id
        """, (dataset_id,))
        insert_rule("metadata_parent_mismatch", "high", "Metadata parent differs from graph parent", f"""
          SELECT m.source_id,m.conversation_id,m.node_id,
                 json_object('mapping_node.parent',m.parent_id,
                             'message.metadata.parent_id',json_extract(m.metadata_json,'$.parent_id')) evidence_json
          FROM messages m {scope}
          AND json_extract(m.metadata_json,'$.parent_id') IS NOT NULL
          AND COALESCE(CAST(json_extract(m.metadata_json,'$.parent_id') AS TEXT),'')
              <> COALESCE(m.parent_id,'')
        """, (dataset_id,))
        insert_rule("missing_parent_node", "high", "Referenced parent is absent from the imported corpus", f"""
          SELECT m.source_id,m.conversation_id,m.node_id,
                 json_object('mapping_node.parent',m.parent_id,
                             'scope_note','Absence is relative to the currently imported dataset.') evidence_json
          FROM messages m {scope} AND m.parent_id IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM messages p JOIN sources ps ON ps.id=p.source_id
            WHERE ps.dataset_id=? AND p.conversation_id=m.conversation_id AND p.node_id=m.parent_id
          )
        """, (dataset_id, dataset_id))
        insert_rule("missing_parent_backlink", "medium", "Parent omits this child", f"""
          SELECT m.source_id,m.conversation_id,m.node_id,
                 json_object('parent',m.parent_id,'expected_child',m.node_id,
                             'scope_note','No copy of the parent in this dataset declares this child.') evidence_json
          FROM messages m {scope} AND m.parent_id IS NOT NULL
          AND EXISTS (
            SELECT 1 FROM messages p JOIN sources ps ON ps.id=p.source_id
            WHERE ps.dataset_id=? AND p.conversation_id=m.conversation_id AND p.node_id=m.parent_id
          )
          AND NOT EXISTS (
            SELECT 1 FROM messages p JOIN sources ps ON ps.id=p.source_id,
                   json_each(p.children_json) child
            WHERE ps.dataset_id=? AND p.conversation_id=m.conversation_id
              AND p.node_id=m.parent_id AND CAST(child.value AS TEXT)=m.node_id
          )
        """, (dataset_id, dataset_id, dataset_id))
        insert_rule("timestamp_before_parent", "medium", "Child timestamp precedes parent", """
          SELECT m.source_id,m.conversation_id,m.node_id,
                 json_object('child.display_time',m.display_time,
                             'earliest_parent.display_time',MIN(p.display_time)) evidence_json
          FROM messages m JOIN sources s ON s.id=m.source_id
          JOIN messages p ON p.conversation_id=m.conversation_id AND p.node_id=m.parent_id
          JOIN sources ps ON ps.id=p.source_id
          WHERE s.dataset_id=? AND ps.dataset_id=?
            AND m.display_time IS NOT NULL AND p.display_time IS NOT NULL
          GROUP BY m.pk HAVING m.display_time<MIN(p.display_time)
        """, (dataset_id, dataset_id))
        insert_rule("missing_child_node", "medium", "Declared child is absent from the imported corpus", """
          SELECT m.source_id,m.conversation_id,m.node_id,
                 json_object('missing_children',json_group_array(CAST(declared.value AS TEXT)),
                             'scope_note','Absence is relative to the currently imported dataset.') evidence_json
          FROM messages m JOIN sources s ON s.id=m.source_id, json_each(m.children_json) declared
          WHERE s.dataset_id=? AND NOT EXISTS (
            SELECT 1 FROM messages child JOIN sources cs ON cs.id=child.source_id
            WHERE cs.dataset_id=? AND child.conversation_id=m.conversation_id
              AND child.node_id=CAST(declared.value AS TEXT)
          )
          GROUP BY m.pk
        """, (dataset_id, dataset_id))
        insert_rule("child_parent_mismatch", "high", "Child points to a different parent", """
          SELECT m.source_id,m.conversation_id,m.node_id,
                 json_object('declared_children',json_group_array(CAST(declared.value AS TEXT)),
                             'expected_parent',m.node_id,
                             'scope_note','The child exists, but no copy in this dataset points back to this parent.') evidence_json
          FROM messages m JOIN sources s ON s.id=m.source_id, json_each(m.children_json) declared
          WHERE s.dataset_id=?
          AND EXISTS (
            SELECT 1 FROM messages child JOIN sources cs ON cs.id=child.source_id
            WHERE cs.dataset_id=? AND child.conversation_id=m.conversation_id
              AND child.node_id=CAST(declared.value AS TEXT)
          )
          AND NOT EXISTS (
            SELECT 1 FROM messages child JOIN sources cs ON cs.id=child.source_id
            WHERE cs.dataset_id=? AND child.conversation_id=m.conversation_id
              AND child.node_id=CAST(declared.value AS TEXT) AND child.parent_id=m.node_id
          )
          GROUP BY m.pk
        """, (dataset_id, dataset_id, dataset_id))
        insert_rule("branch_point", "info", "Multiple child branches", f"""
          SELECT m.source_id,m.conversation_id,m.node_id,
                 json_object('children',json(m.children_json),'count',json_array_length(m.children_json)) evidence_json
          FROM messages m {scope} AND json_array_length(m.children_json)>1
        """, (dataset_id,))
        insert_rule("stop_token_marker", "info", "Stop-token finish marker present", f"""
          SELECT m.source_id,m.conversation_id,m.node_id,
                 json_object('finish_type',m.finish_type,'stop_tokens',json(m.stop_tokens)) evidence_json
          FROM messages m {scope} AND m.stop_tokens IS NOT NULL
        """, (dataset_id,))
        # Metadata is a separate evidentiary layer from message text. Preserve
        # every key/value mention of "hidden", including false-valued fields,
        # and give the visually-hidden flag its own unmistakable marker.
        metadata_rows = conn.execute("""
          SELECT m.source_id,m.conversation_id,m.node_id,m.raw_json
          FROM messages m JOIN sources s ON s.id=m.source_id
          WHERE s.dataset_id=? AND lower(m.raw_json) LIKE '%hidden%'
        """, (dataset_id,)).fetchall()
        for metadata_row in metadata_rows:
            try:
                record = json.loads(metadata_row["raw_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                record = {}
            hits = hidden_metadata_occurrences_in_record(record)
            if not hits:
                continue
            add_anomaly(
                conn, metadata_row, "hidden_metadata_mention", "medium",
                "Metadata contains a hidden field or value",
                {"scope": "all objects named metadata in the message record; ordinary message text excluded", "occurrences": hits},
            )
            visual_hits = [
                hit for hit in hits
                if str(hit.get("key", "")).lower() == "is_visually_hidden_from_conversation"
            ]
            if visual_hits:
                explicitly_true = any(hit.get("value") is True or hit.get("value") == 1 for hit in visual_hits)
                add_anomaly(
                    conn, metadata_row, "visually_hidden_from_conversation", "high",
                    "Visual-hidden metadata field is present",
                    {"explicitly_true": explicitly_true, "occurrences": visual_hits},
                )
        insert_rule("conflicting_message_versions", "high", "Same conversation node has conflicting content", """
          WITH conflicts AS (
            SELECT m.conversation_id,m.node_id,COUNT(DISTINCT m.content_hash) versions
            FROM messages m JOIN sources s ON s.id=m.source_id
            WHERE s.dataset_id=? AND m.message_id IS NOT NULL
            GROUP BY m.conversation_id,m.node_id HAVING versions>1
          )
          SELECT m.source_id,m.conversation_id,m.node_id,
                 json_object('message_id',m.message_id,'content_versions',conflicts.versions) evidence_json
          FROM messages m JOIN sources s ON s.id=m.source_id
          JOIN conflicts ON conflicts.conversation_id=m.conversation_id AND conflicts.node_id=m.node_id
          WHERE s.dataset_id=?
        """, (dataset_id, dataset_id))
        insert_rule("message_id_reused_across_conversations", "info", "Message ID appears in multiple conversation records", """
          WITH reused AS (
            SELECT m.message_id,COUNT(DISTINCT m.conversation_id) conversations
            FROM messages m JOIN sources s ON s.id=m.source_id
            WHERE s.dataset_id=? AND m.message_id IS NOT NULL
            GROUP BY m.message_id HAVING conversations>1
          )
          SELECT m.source_id,m.conversation_id,m.node_id,
                 json_object('message_id',m.message_id,'conversation_count',reused.conversations,
                             'scope_note','Reuse is recorded separately and is not itself a content conflict.') evidence_json
          FROM messages m JOIN sources s ON s.id=m.source_id
          JOIN reused ON reused.message_id=m.message_id
          WHERE s.dataset_id=?
        """, (dataset_id, dataset_id))
        refresh_dataset_summary(conn, dataset_id)
        conn.commit()


def analyze_cross_dataset_conflicts() -> None:
    """Mark content disagreement while keeping every source occurrence intact."""
    with db() as conn:
        conn.execute("DELETE FROM anomalies WHERE rule IN "
                     "('cross_dataset_content_conflict','cross_dataset_text_conflict',"
                     "'cross_dataset_content_variant','cross_dataset_content_omission')")
        conn.execute("DELETE FROM cross_content_variants")
        conn.execute("DELETE FROM cross_event_state")
        conn.execute("""
          INSERT INTO cross_event_state(
            conversation_id,node_id,dataset_count,visible_versions,min_omitted,max_omitted
          )
          SELECT m.conversation_id,m.node_id,COUNT(DISTINCT s.dataset_id),
                 COUNT(DISTINCT CASE
                   WHEN COALESCE(json_extract(m.raw_json,'$.message.content.content_omitted'),0)=0
                   THEN m.visible_text_hash END),
                 MIN(COALESCE(json_extract(m.raw_json,'$.message.content.content_omitted'),0)),
                 MAX(COALESCE(json_extract(m.raw_json,'$.message.content.content_omitted'),0))
          FROM messages m JOIN sources s ON s.id=m.source_id
          GROUP BY m.conversation_id,m.node_id
          HAVING COUNT(DISTINCT s.dataset_id)>1
        """)
        conn.execute("""
          INSERT INTO cross_content_variants(
            conversation_id,node_id,visible_text_hash,dataset_count,canonical_versions
          )
          SELECT m.conversation_id,m.node_id,m.visible_text_hash,
                 COUNT(DISTINCT s.dataset_id),COUNT(DISTINCT m.canonical_content_hash)
          FROM messages m JOIN sources s ON s.id=m.source_id
          WHERE COALESCE(json_extract(m.raw_json,'$.message.content.content_omitted'),0)=0
          GROUP BY m.conversation_id,m.node_id,m.visible_text_hash
          HAVING COUNT(DISTINCT s.dataset_id)>1 AND COUNT(DISTINCT m.canonical_content_hash)>1
        """)
        conn.execute("""
          INSERT OR IGNORE INTO anomalies(
            source_id,conversation_id,node_id,rule,severity,title,evidence_json
          )
          SELECT m.source_id,m.conversation_id,m.node_id,
                 'cross_dataset_text_conflict','high',
                 'Visible message text differs in another dataset',
                 json_object(
                   'message_id',m.message_id,
                   'current_visible_text_hash',m.visible_text_hash,
                   'scope_note','At least one retained occurrence with this message ID has different extracted human-readable text.'
                 )
          FROM messages m JOIN sources s ON s.id=m.source_id
          JOIN cross_event_state x ON x.conversation_id=m.conversation_id AND x.node_id=m.node_id
          WHERE m.message_id IS NOT NULL
            AND x.visible_versions>1
            AND COALESCE(json_extract(m.raw_json,'$.message.content.content_omitted'),0)=0
        """)
        conn.execute("""
          INSERT OR IGNORE INTO anomalies(
            source_id,conversation_id,node_id,rule,severity,title,evidence_json
          )
          SELECT m.source_id,m.conversation_id,m.node_id,
                 'cross_dataset_content_variant','info',
                 'Non-text content structure differs in another dataset',
                 json_object(
                   'message_id',m.message_id,
                   'visible_text_hash',m.visible_text_hash,
                   'current_canonical_content_hash',m.canonical_content_hash,
                   'scope_note','The extracted visible text matches, but canonical content JSON differs. This includes asset metadata and schema drift.'
                 )
          FROM messages m JOIN sources s ON s.id=m.source_id
          JOIN cross_content_variants v ON v.conversation_id=m.conversation_id
            AND v.node_id=m.node_id AND v.visible_text_hash=m.visible_text_hash
          WHERE m.message_id IS NOT NULL
            AND COALESCE(json_extract(m.raw_json,'$.message.content.content_omitted'),0)=0
        """)
        conn.execute("""
          INSERT OR IGNORE INTO anomalies(
            source_id,conversation_id,node_id,rule,severity,title,evidence_json
          )
          SELECT m.source_id,m.conversation_id,m.node_id,
                 'cross_dataset_content_omission','info',
                 'Content availability differs in another dataset',
                 json_object(
                   'message_id',m.message_id,
                   'current_content_omitted',COALESCE(json_extract(m.raw_json,'$.message.content.content_omitted'),0),
                   'scope_note','Another retained dataset has a different content_omitted state. This is availability or export-shape drift, not proof that visible text changed.'
                 )
          FROM messages m JOIN sources s ON s.id=m.source_id
          JOIN cross_event_state x ON x.conversation_id=m.conversation_id AND x.node_id=m.node_id
          WHERE m.message_id IS NOT NULL AND x.min_omitted<>x.max_omitted
        """)
        for row in conn.execute("SELECT DISTINCT dataset_id FROM sources WHERE dataset_id IS NOT NULL"):
            refresh_dataset_summary(conn, int(row["dataset_id"]))
        conn.commit()


def analyze_source(source_id: int) -> None:
    """Compatibility wrapper for callers that know only a source ID."""
    with db() as conn:
        row = conn.execute("SELECT dataset_id FROM sources WHERE id=?", (source_id,)).fetchone()
    if row and row["dataset_id"] is not None:
        analyze_dataset(int(row["dataset_id"]))


def import_paths(paths: Iterable[str | Path]) -> dict[str, Any]:
    files = candidate_files(paths)
    if not files:
        raise FileNotFoundError("No supported JSON, NDJSON, or GZIP files found")
    total_rows = 0
    with db() as conn:
        previous_dataset_ids = {
            int(row["dataset_id"])
            for path in files
            for row in conn.execute("SELECT dataset_id FROM sources WHERE path=?", (str(path),))
            if row["dataset_id"] is not None
        }
        label = " + ".join(path.name for path in files[:2])
        if len(files) > 2:
            label += f" + {len(files) - 2} more"
        dataset_id = int(conn.execute(
            "INSERT INTO datasets(name,created_at,input_json) VALUES(?,?,?) RETURNING id",
            (label, utc_now(), json_compact([str(path) for path in files])),
        ).fetchone()[0])
    with JOB_LOCK:
        JOB.update(state="running", label="Indexing source records", current=0, total=len(files),
                   detail="", started_at=utc_now(), finished_at=None, error=None)
    try:
        for idx, path in enumerate(files, 1):
            with JOB_LOCK:
                JOB["current"] = idx - 1
                JOB["detail"] = path.name
            total_rows += import_file(path, dataset_id)
            with JOB_LOCK:
                JOB["current"] = idx
        with JOB_LOCK:
            JOB["detail"] = "Reconciling lineage and structural markers across the complete dataset"
        analyze_dataset(dataset_id)
        for previous_id in sorted(previous_dataset_ids - {dataset_id}):
            with db() as conn:
                remains = conn.execute(
                    "SELECT 1 FROM sources WHERE dataset_id=? LIMIT 1", (previous_id,)
                ).fetchone()
            if remains:
                analyze_dataset(previous_id)
        with db() as conn:
            conn.execute(
                "DELETE FROM datasets WHERE id<>? AND id NOT IN "
                "(SELECT DISTINCT dataset_id FROM sources WHERE dataset_id IS NOT NULL)",
                (dataset_id,),
            )
        analyze_cross_dataset_conflicts()
        with JOB_LOCK:
            JOB.update(state="complete", detail=f"Indexed {total_rows:,} records", finished_at=utc_now())
        return {"dataset_id": dataset_id, "files": len(files), "rows": total_rows}
    except Exception as exc:
        with JOB_LOCK:
            JOB.update(state="error", error=f"{type(exc).__name__}: {exc}", finished_at=utc_now())
        raise


def start_import(paths: list[str]) -> bool:
    with JOB_LOCK:
        if JOB["state"] == "running":
            return False
        JOB.update(state="running", label="Preparing import", error=None)
    def runner() -> None:
        try:
            import_paths(paths)
        except Exception as exc:
            # Discovery may fail before import_paths enters its own try block.
            with JOB_LOCK:
                JOB.update(state="error", error=f"{type(exc).__name__}: {exc}", finished_at=utc_now())
            traceback.print_exc()
    try:
        threading.Thread(target=runner, name="traceglass-import", daemon=True).start()
    except Exception as exc:
        with JOB_LOCK:
            JOB.update(state="error", error=str(exc))
        raise
    return True


def parse_int(value: str | None, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(value or default)))
    except ValueError:
        return default


def api_stats(conn: sqlite3.Connection, q: dict[str, list[str]] | None = None) -> dict[str, Any]:
    q = q or {}
    requested = parse_int((q.get("dataset_id") or [None])[0], 0, 0, 2**31)
    available = conn.execute("""
      SELECT d.id,d.name,d.created_at,COUNT(s.id) file_count,
             SUM(CASE WHEN s.status='ready' THEN 1 ELSE 0 END) source_count,
             COALESCE(json_extract(ds.totals_json,'$.messages'),SUM(s.row_count),0) record_count
      FROM datasets d JOIN sources s ON s.dataset_id=d.id
      LEFT JOIN dataset_summaries ds ON ds.dataset_id=d.id
      GROUP BY d.id ORDER BY d.id DESC
    """).fetchall()
    dataset_ids = {int(row["id"]) for row in available}
    dataset_id = requested if requested in dataset_ids else (int(available[0]["id"]) if available else 0)
    datasets = [dict(row) for row in available]
    if not dataset_id:
        empty = {"sources": 0, "conversations": 0, "messages": 0, "unique_events": 0,
                 "duplicate_event_copies": 0, "events_seen_elsewhere": 0,
                 "conflicting_events_elsewhere": 0, "variant_events_elsewhere": 0,
                 "omitted_events_elsewhere": 0,
                 "anomalies": 0,
                 "min_time": None, "max_time": None}
        return {"active_dataset_id": None, "datasets": [], "totals": empty,
                "roles": [], "rules": [], "sources": [], "job": dict(JOB)}

    summary = conn.execute(
        "SELECT totals_json,roles_json,rules_json FROM dataset_summaries WHERE dataset_id=?",
        (dataset_id,),
    ).fetchone()
    if not summary:
        refresh_dataset_summary(conn, dataset_id)
        conn.commit()
        summary = conn.execute(
            "SELECT totals_json,roles_json,rules_json FROM dataset_summaries WHERE dataset_id=?",
            (dataset_id,),
        ).fetchone()
    totals = json.loads(summary["totals_json"])
    roles = json.loads(summary["roles_json"])
    rules = json.loads(summary["rules_json"])
    sources = [dict(r) for r in conn.execute("""
      SELECT s.id,s.dataset_id,s.path,s.kind,s.size_bytes,s.sha256,s.imported_at,
             s.status,s.row_count,s.error,
             CASE WHEN s.sha256 IS NULL THEN 0 ELSE
               (SELECT COUNT(*) FROM sources copy WHERE copy.sha256=s.sha256 AND copy.id<>s.id)
             END duplicate_copies
      FROM sources s WHERE s.dataset_id=? ORDER BY s.id
    """, (dataset_id,))]
    return {"active_dataset_id": dataset_id, "datasets": datasets, "totals": totals,
            "roles": roles, "rules": rules, "sources": sources, "job": dict(JOB)}


def metadata_filter(path: str, value_json: str) -> tuple[str, list[Any], str, Any]:
    """Compile a bounded, typed metadata predicate; never interpolate user SQL."""
    path = re.sub(r'^(?:\$\.|message\.metadata\.|metadata\.)', '', path.strip())
    if len(path)>256 or not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*(?:\[(?:\*|\d+)\])*(?:\.[A-Za-z_][A-Za-z_0-9]*(?:\[(?:\*|\d+)\])*)*', path):
        raise ValueError('Use a metadata path such as content_references[*].items[*].attribution')
    if path.count('[*]')>8 or len(value_json)>4096:
        raise ValueError('Metadata filter is too large')
    value = json.loads(value_json, parse_constant=lambda x: (_ for _ in ()).throw(ValueError('Non-finite value')))
    if isinstance(value, (list, dict)):
        raise ValueError('Exact value must be a JSON scalar: string, number, boolean, or null')
    if isinstance(value,float) and not math.isfinite(value) or type(value) is int and not -(2**63)<=value<2**63:
        raise ValueError('Number is outside the supported finite range')
    parts = path.split('[*]')
    def build(expr, depth):
        part = parts[depth]
        jp = '$' + (('.' + part) if depth == 0 else part)
        if depth == len(parts)-1:
            kind = 'null' if value is None else 'true' if value is True else 'false' if value is False else 'text' if isinstance(value,str) else 'number'
            if kind in ('null','true','false'):
                return f'json_type({expr},?)=?', [jp,kind]
            type_check = f"json_type({expr},?) IN ('integer','real')" if kind=='number' else f"json_type({expr},?)='text'"
            return f'({type_check} AND json_extract({expr},?)=?)', [jp,jp,value]
        alias = f'exact_meta_{depth}'
        child = f"CASE WHEN {alias}.type IN ('object','array') THEN {alias}.value ELSE '{{}}' END"
        # For a wildcard at the end, compare json_each's native type and atom.
        if depth+1 == len(parts)-1 and parts[depth+1]=='':
            kind = 'null' if value is None else 'true' if value is True else 'false' if value is False else 'text' if isinstance(value,str) else 'number'
            if kind in ('null','true','false'):
                clause,params=f'{alias}.type=?',[kind]
            elif kind=='text':
                clause,params=f"{alias}.type='text' AND {alias}.atom=?",[value]
            else:
                clause,params=f"{alias}.type IN ('integer','real') AND {alias}.atom=?",[value]
        else:
            clause, params = build(child,depth+1)
        return (f"EXISTS(SELECT 1 FROM json_each(CASE WHEN json_type({expr},?)='array' THEN json_extract({expr},?) ELSE '[]' END) {alias} WHERE {clause})", [jp,jp]+params)
    sql, params = build('m.metadata_json',0)
    return sql,params,path,value


def matching_metadata_paths(metadata: Any, path: str, value: Any) -> list[str]:
    tokens = re.findall(r'[A-Za-z_][A-Za-z_0-9]*|\[(?:\*|\d+)\]',path)
    states = [(metadata,'message.metadata')]
    for token in tokens:
        nxt=[]
        for obj,prefix in states:
            if token=='[*]' and isinstance(obj,list):
                nxt.extend((v,f'{prefix}[{i}]') for i,v in enumerate(obj))
            elif token.startswith('[') and isinstance(obj,list):
                i=int(token[1:-1])
                if i<len(obj): nxt.append((obj[i],f'{prefix}[{i}]'))
            elif isinstance(obj,dict) and token in obj:
                nxt.append((obj[token],f'{prefix}.{token}'))
        states=nxt
    def equal(a,b):
        return a==b and (type(a) is type(b) or type(a) in (int,float) and type(b) in (int,float))
    return [p for v,p in states if equal(v,value)]


def matching_passage(obj: Any, needle: str, prefix: str) -> tuple[str,str] | None:
    """Return a field locator and a short passage, not an unlocated JSON blob."""
    if isinstance(obj,dict):
        for key,value in obj.items():
            found=matching_passage(value,needle,f'{prefix}.{key}')
            if found: return found
    elif isinstance(obj,list):
        for i,value in enumerate(obj):
            found=matching_passage(value,needle,f'{prefix}[{i}]')
            if found: return found
    elif isinstance(obj,str) and needle:
        clean=re.sub(r'\s+',' ',obj)
        words=re.findall(r'\w+',needle)
        match=re.search(r'\W+'.join(re.escape(w) for w in words),clean,re.I) if words else None
        if match:
            start=max(0,match.start()-60);end=min(len(clean),max(start+360,match.end()))
            return prefix, ('…' if start else '')+clean[start:end]+('…' if end<len(clean) else '')
    return None


def api_messages(conn: sqlite3.Connection, q: dict[str, list[str]]) -> dict[str, Any]:
    page = parse_int((q.get("page") or [None])[0], 1, 1, 1_000_000)
    page_size = parse_int((q.get("page_size") or [None])[0], 100, 20, 250)
    where = ["1=1"]
    params: list[Any] = []
    exact_path = ((q.get('meta_path') or [''])[0]).strip()
    exact_value = None
    if exact_path:
        exact_sql,exact_params,exact_path,exact_value=metadata_filter(exact_path,(q.get('meta_value') or [''])[0])
        where.append(exact_sql); params.extend(exact_params)
    search = ((q.get("q") or [""])[0]).strip()
    search_mode = ((q.get("search_mode") or ["auto"])[0]).strip()
    if search_mode not in {"auto", "text", "ids", "metadata"}:
        raise ValueError("search_mode must be auto, text, ids, or metadata")
    effective_mode = search_mode
    needle = search
    join_fts = ""
    join_scope = ""
    if search:
        metadata_search = search[5:].strip() if search_mode == "auto" and search.lower().startswith("meta:") else ""
        # IDs are provenance, not prose. FTS deliberately leaves node_id and
        # conversation_id unindexed, so route identifier-shaped searches to
        # the literal columns instead of promising a search the index cannot do.
        identifier_search = bool(
            re.fullmatch(r"(?:[0-9a-fA-F]{8,}(?:-[0-9a-fA-F]+)*|req_[A-Za-z0-9_-]+)", search)
        )
        if search_mode == "auto":
            effective_mode = "metadata" if metadata_search else "ids" if identifier_search else "text"
        needle = metadata_search or search
        if effective_mode == "metadata":
            if metadata_search.lower() == "hidden":
                where.append("EXISTS(SELECT 1 FROM anomalies mh WHERE mh.source_id=m.source_id AND mh.conversation_id=m.conversation_id AND mh.node_id=m.node_id AND mh.rule='hidden_metadata_mention')")
            else:
                where.append("instr(lower(m.metadata_json),lower(?))>0")
                params.append(needle)
        elif effective_mode == "ids":
            columns = ("node_id", "message_id", "conversation_id", "request_id", "turn_exchange_id")
            where.append("(" + " OR ".join(f"m.{col} LIKE ? ESCAPE '\\'" for col in columns) + ")")
            literal = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            params.extend([literal] * len(columns))
        else:
            join_fts = "JOIN messages_fts ON messages_fts.rowid=m.pk"
            where.append("messages_fts MATCH ?")
            params.append('"' + search.replace('"', '""') + '"')
    for field, key in (("m.role", "role"), ("m.conversation_id", "conversation_id"),
                       ("m.model_slug", "model"), ("m.source_id", "source_id")):
        value = ((q.get(key) or [""])[0]).strip()
        if value:
            where.append(f"{field}=?")
            params.append(value)
    dataset_id = parse_int((q.get("dataset_id") or [None])[0], 0, 0, 2**31)
    if dataset_id:
        join_scope = "JOIN sources dataset_source ON dataset_source.id=m.source_id"
        where.append("dataset_source.dataset_id=?")
        params.append(dataset_id)
    anomaly = ((q.get("anomaly") or [""])[0]).strip()
    if anomaly:
        if anomaly == "any":
            where.append("EXISTS(SELECT 1 FROM anomalies ax WHERE ax.source_id=m.source_id AND ax.conversation_id=m.conversation_id AND ax.node_id=m.node_id)")
        else:
            where.append("EXISTS(SELECT 1 FROM anomalies ax WHERE ax.source_id=m.source_id AND ax.conversation_id=m.conversation_id AND ax.node_id=m.node_id AND ax.rule=?)")
            params.append(anomaly)
    start = scalar_time((q.get("start") or [None])[0])
    end = scalar_time((q.get("end") or [None])[0])
    if start is not None:
        where.append("m.display_time>=?"); params.append(start)
    if end is not None:
        where.append("m.display_time<=?"); params.append(end)
    clause = " AND ".join(where)
    count = conn.execute(
        f"SELECT COUNT(*) FROM messages m {join_scope} {join_fts} WHERE {clause}", params
    ).fetchone()[0]
    pages = max(1, (count + page_size - 1) // page_size)
    page = min(page, pages)
    offset = (page - 1) * page_size
    rows = conn.execute(f"""
      WITH page_pks AS (
        SELECT m.pk FROM messages m {join_scope} {join_fts}
        WHERE {clause}
        ORDER BY m.display_time IS NULL,m.display_time,m.pk LIMIT ? OFFSET ?
      )
      SELECT m.pk,m.source_id,m.conversation_id,c.title,m.node_id,m.message_id,m.parent_id,m.role,
             m.create_time raw_create_time,m.display_time create_time,m.timestamp_status,
             m.content_type,m.status,m.model_slug,m.request_id,m.turn_exchange_id,
             m.content_text,m.stop_tokens,m.children_json,m.content_hash,m.metadata_json,
             json_extract(m.raw_json,'$.message.content') content_json,
             COUNT(DISTINCT a.id) anomaly_count,
             GROUP_CONCAT(DISTINCT a.rule) anomaly_rules,
             MAX(CASE WHEN a.rule='visually_hidden_from_conversation' THEN 1 ELSE 0 END) visually_hidden_present,
             MAX(CASE WHEN a.rule='visually_hidden_from_conversation'
                       AND json_extract(a.evidence_json,'$.explicitly_true')=1 THEN 1 ELSE 0 END) visually_hidden,
             COALESCE(n.review_state,'unreviewed') review_state,COALESCE(n.tags,'') tags
      FROM page_pks page JOIN messages m ON m.pk=page.pk
      LEFT JOIN conversations c ON c.source_id=m.source_id AND c.conversation_id=m.conversation_id
      LEFT JOIN anomalies a ON a.source_id=m.source_id AND a.conversation_id=m.conversation_id AND a.node_id=m.node_id
      LEFT JOIN annotations n ON n.source_id=m.source_id AND n.conversation_id=m.conversation_id AND n.node_id=m.node_id
      GROUP BY m.pk ORDER BY m.display_time IS NULL,m.display_time,m.pk
    """, params + [page_size, offset]).fetchall()
    items=[]
    for row in rows:
        d=dict(row)
        d["content_preview"] = re.sub(r"\s+", " ", d.pop("content_text") or "")[:360]
        md=json.loads(d.pop('metadata_json') or '{}')
        content=json.loads(d.pop('content_json') or '{}')
        d['match_path']=''
        if exact_path:
            matches=matching_metadata_paths(md,exact_path,exact_value)
            d['match_path']=matches[0] if matches else 'message.metadata.'+exact_path
            d['content_preview']=json.dumps(exact_value,ensure_ascii=False)
            d['matching_field_count']=len(matches)
        elif search:
            if effective_mode == 'ids':
                paths = {'node_id':'node.id', 'message_id':'message.id', 'conversation_id':'conversation.id',
                         'request_id':'message.metadata.request_id', 'turn_exchange_id':'message.metadata.turn_exchange_id'}
                hits = [(path, str(d[key])) for key,path in paths.items() if needle.lower() in str(d.get(key) or '').lower()]
                d['matching_paths'] = [path for path,_ in hits]
                hit = hits[0] if hits else None
            else:
                hit=matching_passage(md if effective_mode == 'metadata' else content,needle,
                                     'message.metadata' if effective_mode == 'metadata' else 'message.content')
            if not hit and effective_mode == 'text':
                hit=matching_passage(d['title'],needle,'conversation.title')
            if hit: d['match_path'],d['content_preview']=hit
        d["children"] = json.loads(d.pop("children_json") or "[]")
        d["anomaly_rules"] = (d["anomaly_rules"] or "").split(",") if d["anomaly_rules"] else []
        d["hidden_metadata"] = "hidden_metadata_mention" in d["anomaly_rules"]
        d["visually_hidden_present"] = bool(d["visually_hidden_present"])
        d["visually_hidden"] = bool(d["visually_hidden"])
        items.append(d)
    return {"items": items, "page": page, "page_size": page_size, "total": count,
            "pages": pages, "search_scope": {"requested_mode":search_mode,
                "effective_mode":effective_mode if search else "none", "query":needle,
                "dataset_id":dataset_id or None, "source_id":(q.get('source_id') or [None])[0],
                "note":"Filtered indexed records only; original-file JSON search is separate."}}


def parent_trace_checks(conn: sqlite3.Connection, source_id: int, conversation_id: str,
                        node_id: str, limit: int = 200) -> dict[str, Any]:
    """Literal field walks: no substituted fields or links borrowed from other exports."""
    cache: dict[str, Any] = {}

    def get(nid):
        if nid not in cache:
            row = conn.execute("""
                SELECT pk,node_id,role,parent_id,children_json,metadata_json,
                       json_type(raw_json,'$.parent') parent_type
                FROM messages WHERE source_id=? AND conversation_id=? AND node_id=?
            """, (source_id, conversation_id, nid)).fetchone()
            if row:
                item = dict(row)
                item['metadata'] = json.loads(item.pop('metadata_json') or '{}')
                item['children'] = json.loads(item.pop('children_json') or '[]')
                cache[nid] = item
            else:
                cache[nid] = None
        return cache[nid]

    def walk(field):
        steps, seen, issues = [], set(), []
        current = node_id
        stop = {'reason': 'limit', 'node_id': current}
        for _ in range(limit):
            if current in seen:
                stop = {'reason': 'cycle', 'node_id': current}
                break
            item = get(current)
            if item is None:
                stop = {'reason': 'target_not_in_source', 'node_id': current}
                break
            seen.add(current)
            md = item['metadata'] if isinstance(item['metadata'], dict) else {}
            present = item['parent_type'] is not None if field == 'parent' else 'parent_id' in md
            value = item['parent_id'] if field == 'parent' else md.get('parent_id')
            state = 'missing' if not present else 'null' if value is None else 'value'
            step = {'pk': item['pk'], 'node_id': current, 'role': item['role'],
                    'field_state': state, 'parent': value,
                    'hidden': md.get('is_visually_hidden_from_conversation') is True}
            steps.append(step)
            if not present or value is None:
                stop = {'reason': 'missing_field' if not present else 'null_parent', 'node_id': current}
                break
            if (field == 'parent' and item['parent_type'] != 'text') or not isinstance(value, str) or not value:
                stop = {'reason': 'invalid_parent_value', 'node_id': current}
                break
            if field == 'parent':
                parent = get(value)
                step['parent_lists_child'] = (current in parent['children']) if parent else None
                if parent and not step['parent_lists_child']:
                    issues.append({'reason': 'missing_child_backlink', 'node_id': current, 'parent': value})
            current = value
            stop = {'reason': 'limit', 'node_id': current}
        return {'field': field, 'steps': steps, 'stop': stop, 'issues': issues}

    selected = get(node_id)
    md = selected['metadata'] if selected and isinstance(selected['metadata'], dict) else {}
    comparison = 'metadata_parent_missing'
    if 'parent_id' in md:
        comparison = ('outer_parent_missing' if selected['parent_type'] is None else
                      'equal' if md['parent_id'] == selected['parent_id'] else 'different')
    return {'scope': 'same source file and conversation only', 'source_id': source_id,
            'limit': limit, 'comparison': comparison,
            'outer': walk('parent'), 'metadata': walk('message.metadata.parent_id')}


def parent_versions(conn: sqlite3.Connection, conversation_id: str, node_id: str) -> dict[str, Any]:
    """Keep each imported source appearance separate; resolve targets without merging graphs."""
    rows = conn.execute("""
        SELECT m.pk,m.source_id,m.role,m.parent_id,m.metadata_json,m.children_json,
               json_type(m.raw_json,'$.parent') outer_type,
               s.dataset_id,s.path source_path
        FROM messages m JOIN sources s ON s.id=m.source_id
        WHERE m.conversation_id=? AND m.node_id=?
        ORDER BY s.dataset_id DESC,m.source_id LIMIT 51
    """, (conversation_id, node_id)).fetchall()
    items = []
    for row in rows[:50]:
        item = dict(row)
        md = json.loads(item.pop('metadata_json') or '{}')
        md = md if isinstance(md, dict) else {}
        item['outer'] = {'state': 'missing' if item['outer_type'] is None else
                         'null' if item['outer_type'] == 'null' else
                         'value' if item['outer_type'] == 'text' and item['parent_id'] else 'invalid',
                         'value': item.pop('parent_id')}
        item['metadata_parent'] = {'state': 'missing' if 'parent_id' not in md else
                                   'null' if md['parent_id'] is None else
                                   'value' if isinstance(md['parent_id'], str) and md['parent_id'] else 'invalid',
                                   'value': md.get('parent_id')}
        item['children'] = json.loads(item.pop('children_json') or '[]')
        for key in ('outer', 'metadata_parent'):
            field = item[key]
            target = field['value']
            if field['state'] != 'value' or not isinstance(target, str) or not target:
                field['target_scope'] = 'not_a_reference'
                field['targets'] = []
                continue
            matches = conn.execute("""
                SELECT p.pk,p.source_id,p.role,p.author_name,p.children_json
                FROM messages p JOIN sources ps ON ps.id=p.source_id
                WHERE p.conversation_id=? AND p.node_id=? AND ps.dataset_id=?
                ORDER BY p.source_id LIMIT 51
            """, (conversation_id, target, item['dataset_id'])).fetchall()
            field['targets_truncated'] = len(matches) > 50
            field['targets'] = [dict(m) for m in matches[:50]]
            for match in field['targets']:
                children = json.loads(match.pop('children_json') or '[]')
                match['lists_child'] = node_id in children
            field['target_scope'] = ('same_source' if any(m['source_id'] == item['source_id'] for m in matches)
                                     else 'other_file_same_dataset' if matches else 'absent_in_dataset')
        item.pop('outer_type')
        items.append(item)
    return {'items': items, 'truncated': len(rows) > 50,
            'scope': 'Same conversation ID and node ID; imported copies are not independent events.'}


def parent_key_candidates(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Suggest direct-scope spelling variants for inspection, never repair them."""
    result = []
    message = raw.get('message') or {}
    md = message.get('metadata') or {} if isinstance(message, dict) else {}
    for prefix, obj, expected in [('node', raw, 'parent'), ('node.message.metadata', md, 'parent_id')]:
        if not isinstance(obj, dict):
            continue
        normalize = lambda s: re.sub(r'[^a-z]', '', s.lower())
        for key, value in obj.items():
            if key != expected and difflib.SequenceMatcher(None, normalize(key), normalize(expected)).ratio() >= .8:
                result.append({'path': f'{prefix}.{key}', 'expected_key_in_this_scope': expected,
                               'value': value, 'note': 'Similar spelling only; not proof of a typo.'})
    return result


def api_message(conn: sqlite3.Connection, pk: int) -> dict[str, Any] | None:
    row = conn.execute("""
      SELECT m.*,c.title,s.path source_path,s.sha256 source_sha256,s.dataset_id source_dataset_id,
             COALESCE(n.review_state,'unreviewed') review_state,
             COALESCE(n.note,'') note,COALESCE(n.tags,'') tags
      FROM messages m
      LEFT JOIN conversations c ON c.source_id=m.source_id AND c.conversation_id=m.conversation_id
      LEFT JOIN sources s ON s.id=m.source_id
      LEFT JOIN annotations n ON n.source_id=m.source_id AND n.conversation_id=m.conversation_id AND n.node_id=m.node_id
      WHERE m.pk=?
    """, (pk,)).fetchone()
    if not row:
        return None
    d=dict(row)
    d["raw_create_time"] = d["create_time"]
    d["create_time"] = d["display_time"]
    for key in ("children_json", "metadata_json", "raw_json", "source_locator_json"):
        name = key.removesuffix("_json")
        try: d[name] = json.loads(d.pop(key) or ("[]" if key == "children_json" else "{}"))
        except json.JSONDecodeError: d[name] = d.pop(key)
    d["anomalies"] = [dict(a) | {"evidence": json.loads(a["evidence_json"])} for a in conn.execute(
        "SELECT rule,severity,title,evidence_json FROM anomalies WHERE source_id=? AND conversation_id=? AND node_id=? ORDER BY CASE severity WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,rule",
        (d["source_id"], d["conversation_id"], d["node_id"]),
    )]
    for a in d["anomalies"]: a.pop("evidence_json", None)
    d["hidden_metadata_hits"] = hidden_metadata_occurrences_in_record(d.get("raw"))
    d["parent_checks"] = parent_trace_checks(conn, d["source_id"], d["conversation_id"], d["node_id"])
    d["parent_versions"] = parent_versions(conn, d["conversation_id"], d["node_id"])
    d["parent_key_candidates"] = parent_key_candidates(d.get("raw") or {})
    current_omitted = int(bool(
        (((d.get("raw") or {}).get("message") or {}).get("content") or {}).get("content_omitted")
    ))
    d["other_appearances"] = [dict(row) for row in conn.execute("""
      SELECT other.pk,os.dataset_id,other.source_id,os.path source_path,
             other.content_hash,other.visible_text_hash,other.canonical_content_hash,
             other.role,other.display_time,
             CASE
               WHEN other.canonical_content_hash=? THEN 'exact_content'
               WHEN COALESCE(json_extract(other.raw_json,'$.message.content.content_omitted'),0)<>?
                 THEN 'content_availability_variant'
               WHEN other.visible_text_hash=? THEN 'same_text_content_variant'
               ELSE 'differing_visible_text'
             END relation
      FROM messages other JOIN sources os ON os.id=other.source_id
      WHERE os.dataset_id<>? AND other.conversation_id=? AND other.node_id=?
      ORDER BY os.dataset_id DESC,other.source_id LIMIT 50
    """, (d["canonical_content_hash"], current_omitted, d["visible_text_hash"],
          d["source_dataset_id"], d["conversation_id"], d["node_id"]))]
    return d


def _source_search_offsets(path: Path, query: str, limit: int = 5000) -> list[int]:
    """Locate literal UTF-8 text in the serialized source without modifying it."""
    cache_key = (str(path), path.stat().st_mtime, query.lower())
    cached = RAW_SEARCH_CACHE.get(cache_key)
    if cached is not None:
        return cached
    needle = query.encode("utf-8", "replace").lower()
    if not needle:
        return []
    opener = gzip.open if path.suffix == ".gz" else open
    offsets: list[int] = []
    overlap = b""
    consumed = 0
    with opener(path, "rb") as handle:
        while True:
            chunk = handle.read(2 * 1024 * 1024)
            if not chunk:
                break
            combined = overlap + chunk
            base = consumed - len(overlap)
            lowered = combined.lower()
            start = 0
            while len(offsets) < limit:
                found = lowered.find(needle, start)
                if found < 0:
                    break
                absolute = base + found
                if not offsets or absolute > offsets[-1]:
                    offsets.append(absolute)
                start = found + max(1, len(needle))
            consumed += len(chunk)
            overlap = combined[-max(0, len(needle) - 1):] if len(needle) > 1 else b""
            if len(offsets) >= limit:
                break
    if len(RAW_SEARCH_CACHE) >= 24:
        RAW_SEARCH_CACHE.pop(next(iter(RAW_SEARCH_CACHE)))
    RAW_SEARCH_CACHE[cache_key] = offsets
    return offsets


def api_source_raw(conn: sqlite3.Connection, source_id: int,
                   q: dict[str, list[str]]) -> dict[str, Any] | None:
    """Return a bounded exact-text window from any imported JSON-family file."""
    row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    if not row:
        return None
    source = dict(row)
    path = Path(source["path"])
    if not path.is_file():
        raise FileNotFoundError(f"Source is no longer available: {path}")
    length = parse_int((q.get("length") or [None])[0], 96 * 1024, 4096, 256 * 1024)
    offset = parse_int((q.get("offset") or [None])[0], 0, 0, 2**63 - 1)
    query = ((q.get("q") or [""])[0]).strip()
    match_index = parse_int((q.get("match") or [None])[0], 0, 0, 4999)
    matches: list[int] = []
    if query:
        matches = _source_search_offsets(path, query)
        if matches:
            match_index = min(match_index, len(matches) - 1)
            offset = max(0, matches[match_index] - length // 3)
        else:
            match_index = 0

    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as handle:
        handle.seek(offset)
        data = handle.read(length)
        eof = not bool(handle.read(1))
    total_bytes: int | None = path.stat().st_size if path.suffix != ".gz" else None
    if eof:
        total_bytes = offset + len(data)
    return {
        "source": {
            "id": source["id"], "dataset_id": source["dataset_id"],
            "path": source["path"], "kind": source["kind"],
            "status": source["status"], "size_bytes": source["size_bytes"],
            "imported_sha256": source["sha256"], "current_bytes_verified": False,
            "row_count": source["row_count"],
            "error": source["error"],
        },
        "offset": offset, "end_offset": offset + len(data),
        "total_bytes": total_bytes, "eof": eof,
        "content": data.decode("utf-8", "replace"),
        "query": query, "matches": matches, "match_count": len(matches),
        "match_index": match_index,
        "match_limit_reached": len(matches) >= 5000,
        "scope_note": "Current on-disk source window, not an immutable snapshot. The imported checksum has NOT been reverified against these bytes. UTF-8 characters split at window boundaries may display as replacement characters.",
    }


def api_lineage(conn: sqlite3.Connection, q: dict[str, list[str]]) -> dict[str, Any]:
    conv = ((q.get("conversation_id") or [""])[0]).strip()
    source_id = parse_int((q.get("source_id") or [None])[0], 0, 0, 2**31)
    focus = ((q.get("focus") or [""])[0]).strip()
    depth = parse_int((q.get("depth") or [None])[0], 5, 1, 20)
    if not conv or not source_id:
        return {"nodes": [], "edges": [], "truncated": False}
    dataset_row = conn.execute("SELECT dataset_id FROM sources WHERE id=?", (source_id,)).fetchone()
    if not dataset_row:
        return {"nodes": [], "edges": [], "truncated": False}
    active_dataset_id = int(dataset_row["dataset_id"])
    all_rows = conn.execute("""
      WITH selected_dataset AS (SELECT dataset_id FROM sources WHERE id=?),
      ranked AS (
        SELECT m.*,ROW_NUMBER() OVER (
          PARTITION BY m.node_id ORDER BY CASE WHEN m.source_id=? THEN 0 ELSE 1 END,m.source_id DESC
        ) pick
        FROM messages m JOIN sources sx ON sx.id=m.source_id
        WHERE sx.dataset_id=(SELECT dataset_id FROM selected_dataset) AND m.conversation_id=?
      )
      SELECT m.pk,m.source_id,sx.dataset_id,m.node_id,m.parent_id,m.role,
             m.display_time create_time,m.content_text,m.metadata_json,m.raw_json,
             COUNT(a.id) anomaly_count
      FROM ranked m LEFT JOIN anomalies a
      ON a.source_id=m.source_id AND a.conversation_id=m.conversation_id AND a.node_id=m.node_id
      JOIN sources sx ON sx.id=m.source_id
      WHERE m.pick=1 GROUP BY m.pk
    """, (source_id, source_id, conv)).fetchall()
    by_id={r["node_id"]:r for r in all_rows}
    children: dict[str,list[str]]={}
    for r in all_rows:
        if r["parent_id"]:
            children.setdefault(r["parent_id"],[]).append(r["node_id"])
    selected: set[str]=set()
    if focus and focus in by_id:
        cur=focus
        for _ in range(depth):
            if not cur or cur not in by_id: break
            selected.add(cur); cur=by_id[cur]["parent_id"]
        frontier=[focus]
        for _ in range(depth):
            frontier=[c for n in frontier for c in children.get(n,[])]
            selected.update(frontier)
    else:
        roots=[r["node_id"] for r in all_rows if not r["parent_id"] or r["parent_id"] not in by_id]
        frontier=roots[:8]
        selected.update(frontier)
        for _ in range(depth):
            frontier=[c for n in frontier for c in children.get(n,[])]
            selected.update(frontier)
    limit=700
    truncated=len(selected)>limit
    selected=set(list(selected)[:limit])

    def rows_for_ids(node_ids: set[str], all_appearances: bool = False) -> list[sqlite3.Row]:
        if not node_ids:
            return []
        ids=list(node_ids)[:limit]
        marks=",".join("?" for _ in ids)
        if all_appearances:
            return conn.execute(f"""
              SELECT m.pk,m.source_id,s.dataset_id,m.node_id,m.parent_id,m.role,
                     m.display_time create_time,m.content_text,m.metadata_json,m.raw_json,
                     (SELECT COUNT(*) FROM anomalies a WHERE a.source_id=m.source_id
                       AND a.conversation_id=m.conversation_id AND a.node_id=m.node_id) anomaly_count
              FROM messages m JOIN sources s ON s.id=m.source_id
              WHERE m.conversation_id=? AND m.node_id IN ({marks})
              ORDER BY s.dataset_id,m.source_id
            """, [conv, *ids]).fetchall()
        return conn.execute(f"""
          WITH ranked AS (
            SELECT m.pk,m.source_id,s.dataset_id,m.node_id,m.parent_id,m.role,
                   m.display_time create_time,m.content_text,m.metadata_json,m.raw_json,
                   ROW_NUMBER() OVER (
                     PARTITION BY m.node_id
                     ORDER BY CASE WHEN m.source_id=? THEN 0
                                   WHEN s.dataset_id=? THEN 1 ELSE 2 END,
                              s.dataset_id DESC,m.source_id DESC
                   ) pick
            FROM messages m JOIN sources s ON s.id=m.source_id
            WHERE m.conversation_id=? AND m.node_id IN ({marks})
          )
          SELECT r.*,(SELECT COUNT(*) FROM anomalies a WHERE a.source_id=r.source_id
                    AND a.conversation_id=? AND a.node_id=r.node_id) anomaly_count
          FROM ranked r WHERE r.pick=1
        """, [source_id,active_dataset_id,conv,*ids,conv]).fetchall()

    # Preserve the active export's graph, then pull in parents referenced by
    # metadata or by another export. This keeps genuine version disagreement
    # visible instead of silently choosing one parent chain.
    enriched: dict[str,sqlite3.Row]={nid:by_id[nid] for nid in selected}
    frontier=set(selected)
    for _ in range(depth):
        appearances=rows_for_ids(frontier, all_appearances=True)
        referenced: set[str]=set()
        for r in appearances:
            if r["parent_id"]:
                referenced.add(str(r["parent_id"]))
            try:
                metadata=json.loads(r["metadata_json"] or "{}")
            except json.JSONDecodeError:
                metadata={}
            if metadata.get("parent_id"):
                referenced.add(str(metadata["parent_id"]))
        referenced-=set(enriched)
        if not referenced or len(enriched)>=limit:
            break
        new_rows=rows_for_ids(referenced)
        if not new_rows:
            break
        for r in new_rows:
            if len(enriched)>=limit:
                truncated=True
                break
            enriched[r["node_id"]]=r
        frontier={r["node_id"] for r in new_rows}

    appearances=rows_for_ids(set(enriched), all_appearances=True)
    appearance_by_node: dict[str,list[sqlite3.Row]]={}
    for r in appearances:
        appearance_by_node.setdefault(r["node_id"],[]).append(r)

    edge_map: dict[tuple[str,str],dict[str,Any]]={}
    def add_edge(parent: Any, child: str, kind: str, dataset_id: int, edge_source_id: int) -> None:
        if not parent:
            return
        parent=str(parent)
        if parent not in enriched or child not in enriched:
            return
        entry=edge_map.setdefault((parent,child),{
            "source":parent,"target":child,"kinds":set(),"dataset_ids":set(),"source_ids":set(),
        })
        entry["kinds"].add(kind);entry["dataset_ids"].add(int(dataset_id));entry["source_ids"].add(int(edge_source_id))

    for nid,node_rows in appearance_by_node.items():
        for r in node_rows:
            graph_kind="active_graph" if int(r["dataset_id"])==active_dataset_id else "other_export_graph"
            add_edge(r["parent_id"],nid,graph_kind,r["dataset_id"],r["source_id"])
            try:
                metadata=json.loads(r["metadata_json"] or "{}")
            except json.JSONDecodeError:
                metadata={}
            metadata_parent=metadata.get("parent_id")
            if metadata_parent and str(metadata_parent)!=str(r["parent_id"] or ""):
                add_edge(metadata_parent,nid,"metadata_parent",r["dataset_id"],r["source_id"])

    nodes=[]
    for nid,r in enriched.items():
        node_rows=appearance_by_node.get(nid,[r])
        datasets=sorted({int(x["dataset_id"]) for x in node_rows})
        active_rows=[x for x in node_rows if int(x["dataset_id"])==active_dataset_id]
        chosen=active_rows[0] if active_rows else r
        try:
            metadata=json.loads(chosen["metadata_json"] or "{}")
        except json.JSONDecodeError:
            metadata={}
        try:
            raw_record=json.loads(chosen["raw_json"] or "{}")
        except json.JSONDecodeError:
            raw_record={}
        hidden_hits=hidden_metadata_occurrences_in_record(raw_record)
        graph_parent=chosen["parent_id"]
        metadata_parent=metadata.get("parent_id")
        alternate_parents={str(x["parent_id"]) for x in node_rows
                           if int(x["dataset_id"])!=active_dataset_id and x["parent_id"]}
        corroborated=bool(metadata_parent and str(metadata_parent) in alternate_parents)
        layout_parent=str(metadata_parent) if corroborated and str(metadata_parent) in enriched else graph_parent
        text=re.sub(r"\s+"," ",chosen["content_text"] or "")
        hidden=bool(metadata.get("is_visually_hidden_from_conversation"))
        nodes.append({
            "pk":chosen["pk"],"id":nid,"parent":layout_parent,"graph_parent":graph_parent,
            "metadata_parent":metadata_parent,"metadata_parent_corroborated":corroborated,
            "role":chosen["role"],"time":chosen["create_time"],
            "preview":text[:120] or ("[hidden empty message]" if hidden else "[empty message]"),
            "anomalies":chosen["anomaly_count"],"hidden":hidden,"dataset_ids":datasets,
            "hidden_metadata":bool(hidden_hits),"hidden_metadata_hits":hidden_hits,
            "source_dataset_id":int(chosen["dataset_id"]),
            "supplemental":active_dataset_id not in datasets,
        })
    edges=[]
    for entry in edge_map.values():
        edges.append({
            "source":entry["source"],"target":entry["target"],
            "kinds":sorted(entry["kinds"]),"dataset_ids":sorted(entry["dataset_ids"]),
            "source_ids":sorted(entry["source_ids"]),
        })
    return {"nodes":nodes,"edges":edges,"truncated":truncated,"conversation_id":conv,
            "active_dataset_id":active_dataset_id,
            "legend":{"active_graph":"solid: outer parent in selected dataset",
                      "other_export_graph":"cyan dash: outer parent in another export",
                      "metadata_parent":"magenta dots: message.metadata.parent_id"}}


class Handler(BaseHTTPRequestHandler):
    server_version = "Traceglass/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def send_json(self, value: Any, status: int = 200) -> None:
        raw = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'")
        self.end_headers(); self.wfile.write(raw)

    def read_json(self) -> Any:
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            raise ValueError("application/json required")
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 10_000_000:
            raise ValueError("JSON request must be 1–10000000 bytes")
        body = json.loads(self.rfile.read(length), object_pairs_hook=unique_json_object)
        if not isinstance(body, dict):
            raise ValueError("JSON object required")
        return body

    def do_GET(self) -> None:
        parsed=urllib.parse.urlparse(self.path)
        q=urllib.parse.parse_qs(parsed.query)
        try:
            if self.headers.get("Host", "") not in {f"127.0.0.1:{self.server.server_port}",f"localhost:{self.server.server_port}"}:
                self.send_json({"error":"Local hostname required"},403); return
            if parsed.path == "/api/health":
                self.send_json({"ok":True,"database":str(DB_PATH),"time":utc_now()}); return
            with db() as conn:
                if parsed.path == "/api/investigations":
                    before=int(q["before"][0]) if "before" in q else None
                    self.send_json(investigations.list_cases(conn,before)); return
                if re.fullmatch(r"/api/investigations/[a-f0-9-]+",parsed.path):
                    before=int(q["before"][0]) if "before" in q else None
                    self.send_json(investigations.read(conn,parsed.path.rsplit("/",1)[1],before)); return
                if parsed.path == "/api/stats": self.send_json(api_stats(conn,q)); return
                if parsed.path == "/api/atlas": self.send_json(constellation.atlas(conn,q)); return
                if parsed.path == "/api/constellation": self.send_json(constellation.graph(conn,q)); return
                if parsed.path == "/api/parent-trace":
                    sid=constellation.integer(q,'source_id',0,1,2**31)
                    limit=constellation.integer(q,'limit',500,1,1000)
                    self.send_json(parent_trace_checks(conn,sid,q.get('conversation_id',[''])[0],q.get('node_id',[''])[0],limit)); return
                if parsed.path == "/api/messages": self.send_json(api_messages(conn,q)); return
                if parsed.path.startswith("/api/messages/"):
                    item=api_message(conn,int(parsed.path.rsplit("/",1)[1]))
                    self.send_json(item if item else {"error":"not found"}, 200 if item else 404); return
                if parsed.path.startswith("/api/sources/") and parsed.path.endswith("/raw"):
                    source_id=int(parsed.path.split("/")[3])
                    item=api_source_raw(conn,source_id,q)
                    self.send_json(item if item else {"error":"not found"}, 200 if item else 404); return
                if parsed.path == "/api/lineage": self.send_json(api_lineage(conn,q)); return
            self.serve_static(parsed.path)
        except LookupError as exc:
            self.send_json({"error":str(exc)},404)
        except ValueError as exc:
            self.send_json({"error":str(exc)},400)
        except Exception as exc:
            traceback.print_exc(); self.send_json({"error":f"{type(exc).__name__}: {exc}"},500)

    def do_POST(self) -> None:
        try:
            # Reject cross-site mutations, including requests to a rebound hostname.
            allowed_hosts={f"127.0.0.1:{self.server.server_port}",f"localhost:{self.server.server_port}"}
            host=self.headers.get("Host", "")
            origin=self.headers.get("Origin")
            if host not in allowed_hosts or (origin and origin != f"http://{host}"):
                self.send_json({"error":"Local same-origin requests only"},403); return
            if urllib.parse.urlparse(self.path).path == "/api/upload":
                if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/octet-stream":
                    self.send_json({"error":"application/octet-stream required"},415); return
                if self.headers.get("Transfer-Encoding"):
                    raise ValueError("A fixed Content-Length is required")
                if not UPLOAD_LOCK.acquire(blocking=False):
                    self.send_json({"error":"Another local file transfer is running"},409); return
                try:
                    query=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    self.connection.settimeout(60)
                    path=save_upload(self.rfile, query.get("name", [""])[0], int(self.headers.get("Content-Length", "0")))
                finally:
                    UPLOAD_LOCK.release()
                self.send_json({"path":str(path), "stored_locally":True},201); return
            if self.path == "/api/investigations" or self.path.startswith("/api/investigations/"):
                if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                    self.send_json({"error":"application/json required"},415); return
                size=int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 100000:
                    self.send_json({"error":"Investigation request must be 1–100000 bytes"},413); return
                body=self.read_json()
                if not isinstance(body,dict): raise ValueError("JSON object required")
                with db() as conn:
                    if self.path == "/api/investigations":
                        result=investigations.create(conn,body)
                    else:
                        match=re.fullmatch(r"/api/investigations/([a-f0-9-]+)/(events|tools)",self.path)
                        if not match:
                            self.send_json({"error":"not found"},404); return
                        case_id,action=match.groups()
                        if action == "events": result=investigations.add_entry(conn,case_id,body)
                        else:
                            def inspect(c,ref):
                                row=c.execute("SELECT role,content_type,display_time,request_id,turn_exchange_id,model_slug,parent_id,children_json FROM messages WHERE pk=?",(ref["pk"],)).fetchone()
                                return dict(row)
                            readers={"inspect_record":inspect,
                                     "trace_parents":lambda c,r:parent_trace_checks(c,r["source_id"],r["conversation_id"],r["node_id"]),
                                     "compare_exports":lambda c,r:parent_versions(c,r["conversation_id"],r["node_id"])}
                            result=investigations.run_tool(conn,case_id,body,readers)
                self.send_json(result,201); return
            if self.path == "/api/import":
                body=self.read_json(); paths=body.get("paths") or []
                if not isinstance(paths,list) or not all(isinstance(p,str) for p in paths):
                    self.send_json({"error":"paths must be a list of local paths"},400); return
                if not start_import(paths): self.send_json({"error":"an import is already running"},409); return
                self.send_json({"started":True},202); return
            if self.path == "/api/import/sample":
                paths=[str(p) for p in candidate_files([SAMPLE_DIR])]
                if not paths: self.send_json({"error":"sample files are not installed"},404); return
                if not start_import(paths): self.send_json({"error":"an import is already running"},409); return
                self.send_json({"started":True,"files":len(paths)},202); return
            if self.path.startswith("/api/messages/") and self.path.endswith("/annotation"):
                pk=int(self.path.split("/")[3]); body=self.read_json()
                with db() as conn:
                    row=conn.execute("SELECT source_id,conversation_id,node_id FROM messages WHERE pk=?",(pk,)).fetchone()
                    if not row: self.send_json({"error":"not found"},404); return
                    state=body.get("review_state","unreviewed")
                    if state not in {"unreviewed","reviewing","confirmed","dismissed"}:
                        self.send_json({"error":"invalid review_state"},400); return
                    note=str(body.get("note", ""))[:20000]; tags=str(body.get("tags", ""))[:2000]
                    conn.execute("""
                      INSERT INTO annotations(source_id,conversation_id,node_id,review_state,note,tags,updated_at)
                      VALUES(?,?,?,?,?,?,?) ON CONFLICT(source_id,conversation_id,node_id) DO UPDATE SET
                      review_state=excluded.review_state,note=excluded.note,tags=excluded.tags,updated_at=excluded.updated_at
                    """,(row["source_id"],row["conversation_id"],row["node_id"],state,note,tags,utc_now()))
                self.send_json({"saved":True,"review_state":state,"note":note,"tags":tags}); return
            self.send_json({"error":"not found"},404)
        except LookupError as exc:
            self.send_json({"error":str(exc)},404)
        except (ValueError, TypeError) as exc:
            self.send_json({"error":str(exc)},400)
        except Exception as exc:
            traceback.print_exc(); self.send_json({"error":f"{type(exc).__name__}: {exc}"},500)

    def serve_static(self, request_path: str) -> None:
        rel="index.html" if request_path in {"","/"} else request_path.lstrip("/")
        target=(STATIC/rel).resolve()
        if STATIC.resolve() not in target.parents and target != STATIC.resolve():
            self.send_error(HTTPStatus.FORBIDDEN); return
        if not target.is_file(): self.send_error(HTTPStatus.NOT_FOUND); return
        raw=target.read_bytes(); self.send_response(200)
        self.send_header("Content-Type",mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length",str(len(raw)))
        self.send_header("Cache-Control","no-cache")
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("Content-Security-Policy","default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'")
        self.end_headers(); self.wfile.write(raw)


def main(argv: list[str] | None = None) -> int:
    parser=argparse.ArgumentParser(description="Traceglass local OpenAI export observatory")
    sub=parser.add_subparsers(dest="command")
    serve=sub.add_parser("serve"); serve.add_argument("--port",type=int,default=PORT)
    imp=sub.add_parser("import"); imp.add_argument("paths",nargs="+")
    sub.add_parser("import-sample")
    args=parser.parse_args(argv)
    init_db()
    if args.command in {None,"serve"}:
        port=getattr(args,"port",PORT)
        server=ThreadingHTTPServer((HOST,port),Handler)
        print(f"Traceglass is running at http://{HOST}:{port}",flush=True)
        try: server.serve_forever()
        except KeyboardInterrupt: pass
        finally: server.server_close()
        return 0
    if args.command=="import": result=import_paths(args.paths)
    else: result=import_paths([SAMPLE_DIR])
    print(json.dumps(result,indent=2)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
