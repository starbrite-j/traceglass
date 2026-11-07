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
Node build, API key, or paid service is required. Automated regressions run on Linux, macOS, and Windows; local large-data
measurements use macOS with Python 3.14.

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
gzip are accepted, including UTF-8 files with a byte-order mark. Directory imports
discover `.json`, `.jsonl`, `.ndjson`, and their `.gz` variants without regard to
filename case. Conversation mappings and the supported chronological-record shape
receive ledger/graph indexing, including records in `conversations`, `items`, or
`data` list wrappers. Other JSON can be available in the raw viewer
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
- **Lineage:** start with connected conversation basins, then open a basin or
  evidence bridge to trace ancestors, siblings, stopped branches, and cross-chat
  references in both directions. Separate colors and line patterns identify outer
  `parent`, declared `children`, metadata `parent_id`, and shared message IDs.
  Select another export explicitly to compare copies; they never merge. Failed
  records and exported thinking have distinct shapes. Click a connection for its
  literal field/ID, or open the raw record. Expand the window, highlight a parent
  path, use FOCUS VIEW, and export the displayed evidence. Unloaded is not missing.
  See [connection rules and validation](docs/CONNECTED_LINEAGE_VALIDATION.md).
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

## Verification and backup

`python3 app.py verify --sources` performs a read-only consistency/source-hash
check. `python3 app.py backup --out /absolute/private/path/new-snapshot.sqlite3`
creates a checked SQLite snapshot including the notebook. Source archives need
separate backups. See the [verifier review](docs/VERIFIER_REVIEW.md) for the review
verdict, found/fixed holes, measurements, restore procedure, and production gates.

## Known limits

- Import spills large conversation mappings to temporary storage. Memory still
  scales with an individual decoded node/value, metadata, key indexes, and SQLite
  work. Input/value/depth/expansion/time budgets reject unsupported work explicitly.
  Indexes and temporary spools consume additional disk space; there is no promise
  that every multi-gigabyte export fits every PC.
- Duplicate JSON keys, invalid UTF-8, non-finite numbers (`NaN`, `Infinity`, or
  floating-point overflow), conflicting mapping-key/node identities, and repeated
  indexed identities within one file fail rather than select a winner. Recognized
  records with unsupported object-field types or non-object mapping nodes fail
  with a field-specific error. Missing and null node IDs stay distinct in raw
  record detail; the mapping key supplies the index identity in those cases.
  Unrepresentable timestamp values remain in raw detail and are excluded from
  numeric time fields.
  The failing file rolls back; earlier files in a multi-file import may remain.
  If that happens, their reconciliation may be incomplete. Start a fresh index
  after correcting the input instead of treating partial markers as final.
- Raw view reads the **current file**, not an immutable snapshot. Its displayed
  imported checksum is not reverified against each window. If a source changes
  after indexing, raw view and indexed detail may disagree.
- Raw windows are at most 256 KiB; raw search retains up to 5,000 matches with a
  4 GiB / five-second scan budget. Partial scans are labeled. Gzip seeks are capped
  at 64 MiB; extract the source to inspect later windows. Detail and legacy graph
  byte limits direct large requests to bounded source windows or connected Lineage.
- Graph and ancestor walks are bounded. The connected graph reports clipped
  windows, ambiguous candidates, unresolved references, and work limits explicitly.
  Its basin overview is a bounded summary; open a record for exact reference
  inspection. A basin groups a conversation occurrence, not a computed semantic
  attractor or a measured memory influence. Legacy graph APIs retain their
  conversation scope. [Exact bounds](docs/CONNECTED_LINEAGE_VALIDATION.md#bounds-and-limits).
- Annotations require **SAVE**. Unsaved drafts are not durable. Notebook findings
  are investigator assessments, not automatically verified facts.

Back up source archives independently. To start an empty workspace, stop the server
and move `data/` to a safe backup location; restarting creates a new local database.

## Measured large-data validation

The [verifier review](docs/VERIFIER_REVIEW.md) contains the current hardened-build
results. The following paragraph records the preceding connected-graph build.

With the connected graph indexes enabled, a real 2.00 GiB synthetic JSON import
with 165,344 records completed in **125.7 seconds**, using **225.8 MiB process peak
RSS through import** on the measured Mac. The basin overview took 0.49 seconds
(median of three HTTP requests). A separate 80,000-record branching workload
verified cross-chat references, shared IDs, failed records, and exported thinking.

See [connected-graph validation](docs/CONNECTED_LINEAGE_VALIDATION.md) for current
measurements, source/code hashes, failed iterations, limits, and reproduction
commands. The [earlier ingestion report](docs/LARGE_DATA_VALIDATION.md) retains
browser-upload evidence and the large single-conversation memory test. Those
measurements describe their pinned versions and workloads; they do not establish
support for every export up to the file picker's 20 GiB limit.

During an import, progress remains active through reconciliation. Statistics show
an explicitly labeled snapshot of the prior index and refresh when the import
finishes.

## Development

```sh
python3 -m unittest discover -s tests -v
```

Tests use synthetic records and temporary databases. See [CONTRIBUTING.md](CONTRIBUTING.md)
and [AGENTS.md](AGENTS.md) for evidence-preserving changes. See [SECURITY.md](SECURITY.md)
before reporting sensitive issues.

MIT licensed. See [LICENSE](LICENSE).
