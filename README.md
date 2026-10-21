# Traceglass

**Follow the record. Keep the provenance.**

Traceglass is a local-first viewer for ChatGPT JSON exports: a searchable ledger,
interactive lineage graphs, metadata inspection, export comparison, and an
investigation notebook. It helps people and AI-assisted investigators ask concrete
questions about timelines and trace answers back to source records.

**Early preview — useful, evolving, and not a forensic certification tool.**
Structural markers describe what the export contains, not what caused a model's
behavior. A hidden system node, differing parent fields, or a missing record does
not by itself establish a runtime failure or interference.

## Run locally

Requires Python 3.10+ with SQLite 3.35+ and its FTS5/JSON support. No pip packages,
Node build, API key, or paid service is required. The release was tested locally on
macOS with Python 3.14; other platforms need further validation.

1. Download this repository with **Code → Download ZIP**, then extract it (or clone it).
2. Open a terminal in the extracted project folder.
3. Run:

   ```sh
   python3 app.py serve
   ```

4. Open **http://127.0.0.1:8765** in your browser.
5. Click **LOAD SOURCE → YOUR JSON EXPORT**, choose your file, and click **LOAD**.

On Windows, `py app.py serve` may be the appropriate command. If port 8765 is busy,
use `python3 app.py serve --port 8766` and open the address printed in the terminal.
Keep the terminal running; Ctrl+C stops the server. Restarting preserves your index
and saved notebook.

## Bring your own data

Personal import is the main workflow. No real conversation exports are included.

- **Choose a file:** the browser transfers it only to your local Traceglass server.
  The server copies it in bounded chunks into `data/uploads/`, then indexes it.
  The picker supports a nonempty file up to 20 GiB; allow additional disk space for
  the index. An interrupted transfer is removed; a completed copy remains even if
  indexing fails.
- **Local export path:** enter an absolute file or directory path. This indexes the
  source in place without making another source copy. Prefer it for very large
  exports or multiple files. Keep those source files available and unchanged.
- **Optional synthetic demo:** invented IDs, text, tool activity, and an intentional
  parent mismatch demonstrate navigation. They are not historical evidence.

Extract ZIP exports first. JSON arrays, whitespace-separated JSON/NDJSON/JSONL, and
gzip are accepted. Conversation mappings and the supported chronological-record
shape receive ledger/graph indexing. Other JSON can be available in the raw viewer
without conversation indexing; arbitrary agent-log formats are not automatically
understood. Images, audio, and attachments are not decoded.

CLI alternatives:

```sh
python3 app.py import "/absolute/path/to/conversations.json"
python3 app.py import "/absolute/path/to/export-directory"
python3 app.py import-sample
```

Do not run CLI imports concurrently with a server import. To compare exports, keep
them at different paths and import each export separately. Copies are retained;
record counts are not a claim about the number of independent events. Same-path
reimport is deliberately refused in this preview to protect existing provenance.

## Explore from the outside in

- **Ledger:** filter roles and structural markers, search text/IDs/metadata, type a
  page number, and open any record for its full fields.
- **Lineage:** inspect branches, expand a bounded graph, trace ancestors in batches,
  and compare recorded outer `parent` with `message.metadata.parent_id` and other
  export copies. Unloaded is not the same as missing.
- **JSON:** read bounded windows of the original serialized source and search the
  selected file. This is separate from indexed ledger search.
- **Investigate:** preserve a question, pin source records, run explicit inspection
  tools, and keep notes, hypotheses, findings, and append-only corrections distinct.

Tool records and hidden metadata stay inspectable. Missing fields and explicit
values remain available in record detail. Export disagreements are not silently
resolved into a single "true" version.

There is **no conversational model, voice service, or external inference API
connected** in this release. The local notebook and bounded JSON endpoints provide
a foundation for later AI-assisted workflows; they are not autonomous agents.

## Privacy and boundaries

The server binds to `127.0.0.1`. Source files are read-only; indexes, copied imports,
annotations, and notebooks live in `data/`. Saved search views live in browser local
storage. No analytics, remote fonts, or automatic cloud uploads are configured.

**Your local data is not encrypted by Traceglass.** Anyone with access to this
computer/account may be able to read it. Do not expose the server through public
hosting, tunnels, or a reverse proxy: it has no login or multi-user isolation.
Treat exports and rendered metadata as sensitive, including screenshots.

`data/` is ignored by Git. Never upload your exports, databases, private notes,
credentials, or a ZIP of your entire working folder to a public issue/repository.
The synthetic demo is the only intended public sample data.

## Known limits

- Import is incremental across top-level JSON values, **not constant-memory**:
  the largest individual conversation/value must fit in memory. Indexes consume
  disk space; there is no promise that every multi-gigabyte export fits every PC.
- Duplicate JSON keys, invalid UTF-8, conflicting mapping-key/node identities, and
  repeated indexed identities within one file fail rather than select a winner.
  The failing file rolls back; earlier files in a multi-file import may remain.
  If that happens, their reconciliation may be incomplete. Start a fresh index
  after correcting the input instead of treating partial markers as final.
- Raw view reads the **current file**, not an immutable snapshot. Its displayed
  imported checksum is not reverified against each window. If a source changes
  after indexing, raw view and indexed detail may disagree.
- Raw windows are at most 256 KiB; raw search retains up to 5,000 matches and may
  scan the whole file. Gzip seeks can require decompression from the beginning.
- Graph and ancestor walks are bounded. Large conversations still require some
  whole-conversation metadata work. Children-only edges, unusual export schemas,
  and cross-conversation references need careful source inspection.
- Annotations require **SAVE**. Unsaved drafts are not durable. Notebook findings
  are investigator assessments, not automatically verified facts.

Back up source archives independently. To start an empty workspace, stop the server
and move `data/` to a safe backup location; restarting creates a new local database.

## Development

```sh
python3 -m unittest discover -s tests -v
```

Tests use synthetic records and temporary databases. See [CONTRIBUTING.md](CONTRIBUTING.md)
and [AGENTS.md](AGENTS.md) for evidence-preserving changes. See [SECURITY.md](SECURITY.md)
before reporting sensitive issues.

MIT licensed. See [LICENSE](LICENSE).
