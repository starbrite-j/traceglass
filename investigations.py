"""Local investigation journal. No network, model, shell, or source-file writes."""
import json
import uuid
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS investigations (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, question TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS investigation_events (
 id INTEGER PRIMARY KEY, investigation_id TEXT NOT NULL REFERENCES investigations(id),
 created_at TEXT NOT NULL, actor TEXT NOT NULL, kind TEXT NOT NULL,
 text TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_investigation_events
 ON investigation_events(investigation_id,id);
"""
NOTE_KINDS = {"note", "question", "hypothesis", "correction", "finding", "next_step"}
TOOLS = {"inspect_record", "trace_parents", "compare_exports"}


def now():
    return datetime.now(timezone.utc).isoformat()


def text_field(value, name, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must contain 1–{maximum} characters")
    return value.strip()


def require_case(conn, case_id):
    row = conn.execute("SELECT * FROM investigations WHERE id=?", (case_id,)).fetchone()
    if row is None:
        raise LookupError("Investigation not found")
    return dict(row)


def append(conn, case_id, actor, kind, text, payload=None):
    stamp = now()
    cur = conn.execute("""INSERT INTO investigation_events
      (investigation_id,created_at,actor,kind,text,payload_json) VALUES(?,?,?,?,?,?)""",
      (case_id, stamp, actor, kind, text, json.dumps(payload or {}, ensure_ascii=False)))
    conn.execute("UPDATE investigations SET updated_at=? WHERE id=?", (stamp, case_id))
    return {"id": cur.lastrowid, "created_at": stamp, "actor": actor,
            "kind": kind, "text": text, "payload": payload or {}}


def create(conn, body):
    title = text_field(body.get("title"), "Title", 160)
    question = text_field(body.get("question"), "Original question", 12000)
    case_id, stamp = str(uuid.uuid4()), now()
    conn.execute("INSERT INTO investigations VALUES(?,?,?,?,?)",
                 (case_id, title, question, stamp, stamp))
    append(conn, case_id, "user", "question", question)
    return require_case(conn, case_id)


def list_cases(conn, before=None):
    # Stable creation-order cursor; no investigation is lost beyond a list cap.
    rows = conn.execute("SELECT rowid cursor,* FROM investigations WHERE rowid<? ORDER BY rowid DESC LIMIT 101",
                        (before if before is not None else 9223372036854775807,)).fetchall()
    items = [dict(r) for r in rows[:100]]
    return {"items": items, "before": items[-1]["cursor"] if len(rows) > 100 else None}


def read(conn, case_id, before=None):
    case = require_case(conn, case_id)
    rows = conn.execute("""SELECT * FROM investigation_events
        WHERE investigation_id=? AND id<? ORDER BY id DESC LIMIT 101""",
        (case_id, before if before is not None else 9223372036854775807)).fetchall()
    items = []
    for r in reversed(rows[:100]):
        item = dict(r)
        item["payload"] = json.loads(item.pop("payload_json"))
        items.append(item)
    return {"investigation": case, "events": items,
            "before": items[0]["id"] if len(rows) > 100 else None,
            "connections": {"model": "not_connected", "voice": "not_connected", "jev": "not_connected"}}


def record_reference(conn, pk):
    if type(pk) is not int or pk <= 0:
        raise ValueError("A positive integer record pk is required")
    row = conn.execute("""SELECT m.pk,m.source_id,m.conversation_id,m.node_id,
        m.source_locator_json,m.content_hash,s.path source_path,s.sha256 source_sha256
        FROM messages m JOIN sources s ON s.id=m.source_id WHERE m.pk=?""", (pk,)).fetchone()
    if row is None:
        raise LookupError("Record no longer exists in the index")
    ref = dict(row)
    ref["locator"] = json.loads(ref.pop("source_locator_json") or "{}")
    return ref


def add_entry(conn, case_id, body):
    require_case(conn, case_id)
    kind = body.get("kind")
    text = text_field(body.get("text"), "Entry", 20000)
    if kind in NOTE_KINDS:
        payload = {}
        if "revises" in body:
            target = body["revises"]
            if type(target) is not int:
                raise ValueError("Revision target must be an entry ID")
            prior = conn.execute("SELECT actor,kind FROM investigation_events WHERE id=? AND investigation_id=?",
                                 (target, case_id)).fetchone()
            if prior is None or prior["actor"] != "user" or prior["kind"] not in NOTE_KINDS:
                raise ValueError("Only notebook entries in this investigation can be revised")
            payload["revises"] = target
    elif kind in {"evidence", "checkpoint"}:
        payload = {"record": record_reference(conn, body.get("pk"))}
    else:
        raise ValueError("Unknown entry kind")
    return append(conn, case_id, "user", kind, text, payload)


def run_tool(conn, case_id, body, readers):
    require_case(conn, case_id)
    tool = body.get("tool")
    if not isinstance(tool, str) or tool not in TOOLS:
        raise ValueError("Tool not enabled. Only local inspect, parent trace, and export comparison are available")
    ref = record_reference(conn, body.get("pk"))
    request = append(conn, case_id, "user", "tool_request", tool, {"record": ref})
    # Each reader is provided explicitly by the server, never resolved from user code.
    try:
        result = readers[tool](conn, ref)
    except Exception:
        append(conn, case_id, "local_tool", "tool_error", "Local read failed; no result was inferred.",
               {"request_event_id": request["id"], "tool": tool, "record": ref})
        return {"ok": False, "error": "Local read failed; the attempt was recorded"}
    event = append(conn, case_id, "local_tool", "tool_result", tool,
                   {"request_event_id": request["id"], "tool": tool, "record": ref, "result": result})
    return {"ok": True, "event": event}
