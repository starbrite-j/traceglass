# Large-data validation

Traceglass was exercised against a **2,147,490,402-byte synthetic JSON export**
containing **165,344 records in 10,334 conversations** on November 6, 2026. The final
run passed the configured acceptance budgets and all correctness checks. The
test generates actual data, imports it through the application, and calls its real
loopback HTTP endpoints; no API responses, databases, or import results are mocked.

The [measurement file](validation/verified-2gib.json),
[benchmark program](../scripts/benchmark_large_import.py), and commands below make
this result inspectable and reproducible. Generated exports and indexes stay in
ignored local storage. Public measurement files contain invented identifiers,
checksums, counts, timings, and system versions, not personal conversations.

## Measured results

Machine: Apple M2 Max, 12 CPU cores, 96 GiB installed RAM. Software: macOS 15.7.7,
Python 3.14.7, SQLite 3.53.4. This is one workstation, not a hardware compatibility
certification. The source generator uses varied message lengths, multilingual text,
unique IDs, and searchable per-record markers. Text is assembled from deterministic
invented paragraphs; this is not a representative sample of every export.

| Workload | Records / conversations | Import and reconciliation | Process peak RSS through import | Result |
| --- | ---: | ---: | ---: | --- |
| 2.00 GiB export, final code | 165,344 / 10,334 | 87.06 s | 233.84 MiB | Passed |
| 136.27 MiB single conversation, before parser fix | 11,000 / 1 | 30.30 s | 14,655.14 MiB | Correct data; excessive memory |
| Same single conversation, after parser fix | 11,000 / 1 | 5.80 s | 2,208.42 MiB | Passed; still substantial memory |

RSS is the Python benchmark/server process's high-water mark through import,
including source generation and HTTP monitoring in that process. It excludes the
browser and filesystem cache. This conservative measurement is not a claim that
the importer alone used every byte. The before/after conversation files have the
same checksum and shape; full results are in
[before](validation/giant-before.json) and [after](validation/giant-after.json).

The final 2 GiB acceptance budgets were **512 MiB process RSS**, **180 seconds for
import/reconciliation**, **five seconds per post-import HTTP response**, and a
**three-second timeout for each live probe**. All 17 live monitoring rounds passed.
The slowest sampled progress response was 1.83 ms and statistics response 1.24 ms.

| HTTP operation after the 2 GiB import | Median of 3 requests | Slowest request |
| --- | ---: | ---: |
| First ledger page | 54.97 ms | 55.18 ms |
| Last ledger page | 52.28 ms | 53.71 ms |
| Indexed search for final-record marker | 4.24 ms | 4.42 ms |
| Literal ID search | 281.13 ms | 282.12 ms |
| Exact metadata filter | 1,153.57 ms | 1,190.13 ms |
| Conversation atlas | 49.78 ms | 49.78 ms |
| 16-node conversation graph | 1.85 ms | 2.01 ms |
| Bounded raw-source window | 0.69 ms | 0.72 ms |
| Original-file search | 2.32 ms | 1,517.18 ms |

Original-file search caches match offsets. Its slowest request scanned the source;
the later requests reused those offsets. The filesystem cache was already warmed
by generation/import, so these are not cold-storage measurements. There is one
full run per variant, not a statistical performance study. Every timing and
response size remains available in the measurement files.

## Correctness checks

The benchmark checks actual table counts: 165,344 messages, 165,344 full-text rows,
10,334 conversations, and 310,020 separately declared parent/child edges. It verifies
18 sampled raw records against generator-produced hashes, including records across
the file and its final record. It compares the generator checksum, imported source
checksum, and post-import file checksum to confirm the source stayed unchanged.

SQLite `quick_check` and FTS5 `integrity-check` pass. HTTP checks exercise first and
last ledger pages, indexed text and ID searches, typed metadata filtering, record
detail, atlas, graph windows, bounded parent walks, raw windows, and original-file
search. Reopening schema initialization preserves counts and provenance. Graph and
ancestor limits remain explicit rather than claiming every node has been returned.

The ordinary regression suite contains 56 tests, including malformed and late
failing imports, duplicate keys/identities, invalid UTF-8, source preservation,
wrapped records, optional nulls, and database-independent progress/statistics.

## Actual browser upload

The generated 2,147,490,402-byte file was also selected through Brave's native file
picker and loaded with the application's **YOUR JSON EXPORT → LOAD** button.
The local upload returned HTTP 201, and copying plus indexing/reconciliation
completed in **114.69 seconds**. Indexing/reconciliation accounted for 112.97
seconds; this second import also compared the retained export copies.

The uploaded file's full SHA-256 matched the generated source. The selected
browser-loaded dataset contained 165,344 records, and its final-record search and
18 sampled raw-record hashes matched. The original indexed copy remained intact:
the database held two sources and 330,688 message occurrences. Those are retained
copies, not 330,688 independent events. The UI visibly identifies the other copy.

The HTTP log recorded 140 progress polls over the import, with a median interval
of 0.805 seconds and at most two requests in any one-second bucket. This checks
the fixed browser scheduler against the former multiplying-request behavior.
See the [browser-upload measurements](validation/browser-upload.json) and
[verification program](../scripts/verify_browser_upload.py). The final record,
source identity, and lineage controls were also inspected in the live browser.

## Failures found during this work

The first 2 GiB run indexed correct data but **failed** its live responsiveness
check. A statistics request timed out while the large transaction was finalized.
Two intermediate runs also failed that probe. Those failures are retained in
[baseline](validation/baseline-2gib.json),
[progress-endpoint iteration](validation/status-endpoint-2gib.json), and
[read-only iteration](validation/readonly-2gib.json). Read-only connections alone
did not remove the pause.

The fixes add database-independent import status and a labeled snapshot of the
index from before the current import. Cached counts are explicitly identified as
prior state; completed totals are refreshed after reconciliation. Browser testing
also exposed two overlapping polling schedulers, which multiplied requests during
a long import. A single scheduler now owns polling and retries.

The single-conversation test exposed repeated partial JSON decoding after each
1 MiB read. Incomplete values now grow geometrically before another decode attempt,
and refilling occurs outside the exception handler. The same 143 MB source fell
from approximately 14.31 GiB to 2.16 GiB process peak RSS on the measured machine.
Source identity and sample hashes remained unchanged.

Other corrected ingestion failures include missing JSONL and mixed-case directory
discovery, optional UTF-8 BOM handling, wrapped chronological records, invalid
object-field types, non-finite values/timestamps, uppercase gzip raw viewing, and
preserving missing versus null node IDs in raw record detail.

The first cloud regression run also caught an atlas SQL query that passed with
the workstation's SQLite build but failed on Linux and Windows with
`no such column: c.current_node`. The replacement selects the declared current
node, then falls back to the latest indexed node within the same source and
conversation. A regression checks both choices and a missing current node. This
query-only correction does not change the importer measured above; the corrected
query is covered by the subsequent cloud smoke test.

## Reproduce

The benchmark harness currently uses the Unix `resource` module and runs on macOS
or Linux. It requires no additional Python packages. Each command requires a new
workspace directory and refuses to reuse an existing one.

```sh
python3 -m unittest discover -s tests -v

python3 scripts/benchmark_large_import.py \
  --workspace data/benchmarks/reproduce-2gib \
  --size-mib 2048 \
  --max-rss-mib 512 \
  --max-import-seconds 180 \
  --max-http-seconds 5 \
  --serve-port 8879
```

The server's `browser_ready` line means all automated checks are finished. Open
the printed URL to inspect that actual index. Stop the isolated server with Ctrl+C.
Results, the source manifest, and HTTP logs are saved under the selected workspace.
The corpus and database are intentionally excluded from Git.

To repeat the browser check, note the UTC time immediately before clicking LOAD
for the generated source. After the UI reports completion, run:

```sh
python3 scripts/verify_browser_upload.py \
  --workspace data/benchmarks/reproduce-2gib \
  --port 8879 \
  --upload-started-utc "<the UTC timestamp you recorded, in ISO 8601 format>"
```

This independently checks the uploaded copy, stored records, tail-record search,
retained original, and bounded polling from the actual local HTTP log.

For a smaller smoke test, use `--size-mib 64` and omit `--serve-port`. To stress a
large individual conversation, use `--size-mib 128 --nodes-per-conversation 11000`
with a different workspace. The target size is a minimum; generation always
finishes the current conversation.

The GitHub Actions workflow runs the regression suite on Linux/Python 3.10 and
3.14, macOS/Python 3.14, and Windows/Python 3.14. Its Linux/Python 3.14 job also runs
the real 64 MiB import/HTTP smoke test. A green workflow is separate evidence from
the workstation measurements above; check the actual run result before claiming
that a platform passed.

## Research and remaining limits

- **Largest individual value:** the parser still materializes a whole conversation
  or top-level value. The single-conversation result shows that memory does not
  stay at the approximately 234 MiB measured for many small conversations. A fully
  streaming per-node importer would require further work. Python documents
  [`raw_decode`](https://docs.python.org/3.14/library/json.html#json.JSONDecoder.raw_decode)
  as decoding a document from a string, not providing incremental object events.
- **Storage expansion:** the final 2 GiB corpus produced a 6.53 GiB database; the
  largest observed WAL was 6.57 GiB. Source copies, database, and WAL can coexist.
  Allow substantial additional free space, especially for browser uploads.
  SQLite documents that a large transaction can keep a
  [large WAL until commit/checkpoint](https://sqlite.org/wal.html#avoiding_excessively_large_wal_files).
  Modern SQLite supports large transactions; the application's behavior needs
  measurement rather than assuming SQLite cannot handle gigabytes.
- **Repeated text storage:** raw records, extracted text, and the regular FTS table
  retain overlapping content. SQLite's
  [external-content FTS tables](https://sqlite.org/fts5.html#external_content_tables)
  offer a future storage option, with explicit consistency and migration work.
- **Scope of the claim:** the measured large workload is plain JSON with text-heavy
  conversation mappings. Gzip, chronological wrappers, branching, and malformed
  formats have correctness tests, but this is not a multi-gigabyte performance
  certification of every format, attachment type, or machine. The 20 GiB picker
  cap is a size guard, not evidence that a 20 GiB import was tested.
- **Persistence and interruption:** sources remain read-only and file imports are
  atomic. There is no resumable-import checkpoint yet; failed large files must be
  retried. Raw windows still read the current file, as documented in the README;
  the viewer is not an immutable forensic archive.

These measurements support a specific, reviewable claim: the tested version
imported and navigated this 2 GiB corpus within the stated workstation budgets,
with verifiable counts, provenance checks, tests, and retained failure reports.
