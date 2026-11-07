# Connected history: rules and measured validation

The Lineage view now traces a graph of **record occurrences across conversations**.
Siblings and branches without descendants remain connected through their recorded
parents. Reused IDs retain separate occurrences, including conflicting copies.
An overview groups each conversation copy into a navigable basin; selecting a
bridge opens the actual records that supplied it.

This is a local investigation foundation with reproducible checks. It is not a
certification of frontier-lab deployment readiness or evidence that a particular
chat affected runtime memory. No semantic or causal edges are invented from text,
timestamps, or the positions of points on the canvas.

## Relationship contract

| Evidence | Appearance | Resolution and meaning |
| --- | --- | --- |
| `node.parent` | Slate-blue solid arrow | Literal mapping-key match; arrow parent to child |
| `node.children[i]` | Teal dotted arrow | Literal mapping-key match; each declared child retained independently |
| `message.metadata.parent_id` | Pink dashed arrow | Literal mapping-key or message-ID match; the matched namespace is reported |
| `message.id` reused | Amber dotted line without arrow | Undirected identity relationship; separate occurrences remain separate |
| Explicit failed/error/timeout status, error field, or nonzero tool exit code | Red diamond | The exact exported evidence field is listed |
| Error-like tool output or interruption | Amber review label | Text hint/interruption is separate from a reported execution failure |
| Exported `thoughts`, `reasoning_recap`, or `analysis` channel | Violet square | Exported content only; absent thinking is not reconstructed |
| No declared continuation in selected datasets | Ring | A stopped branch is not automatically a failure |

Every record has an occurrence key (`record:<SQLite pk>`) plus its original source,
conversation, node, and message IDs. References retain field, ordinal, literal ID,
owner, direction, candidate count, ambiguity, and cross-source/cross-chat flags.
Shared-ID edges compare canonical content hashes and mark content disagreements.
Missing/null/invalid parent fields remain distinct. Multiple possible targets are
shown as candidates, never silently replaced by a preferred parent.

The neighborhood starts in the focus record's dataset. **COMPARE EXPORT** adds
exactly the selected second dataset. The evidence download includes selected
scope, original imported source checksums/paths, loaded nodes/edges, unresolved
references, unloaded-edge counts, and work bounds. A raw-source window still reads
the current source file; imported checksums are not rechecked on every request.

The basin overview aggregates cross-only literal references (no matching target
in the owner's basin) and shared message IDs. It suppresses high-fanout ID cliques
and reports those omissions. It is a navigation summary, not the complete graph.
Click a basin/bridge to inspect indexed references against the full selected
dataset. Line filters change drawing only; parent-path highlighting leaves other
loaded records, including siblings, visible. The event list sorts by exported
display timestamp with occurrence key as a tie-breaker; time creates no edges.

## Bounds and limits

- Neighborhood: default 300 occurrences / 4 hops; maximum 3,000 / 12 hops. Traversal
  follows incoming and outgoing references plus shared IDs. A clipped window is
  labeled. Known outside-window edges are distinct from absent targets.
- Resolver: at most 32 target candidates per reference/shared-ID expansion,
  128 incoming and 128 outgoing references per occurrence, 20,000 reference
  resolutions, and 15,000 discovered edges. Bounds appear in the response.
- Overview: 300 basins/page, 250,000 thin catalog rows, 500,000 scanned references,
  200,000 identity-pair operations, 2,000 displayed bridges. Work limits and
  high-fanout groups are explicit; counts/weights may be partial when bounded.
- Each graph HTTP response uses one SQLite read snapshot. A SQLite progress
  handler interrupts SQL work after a 10-second execution deadline and returns
  HTTP 503. This is not an end-to-end Python/browser time or memory guarantee.
- The worker uses a bounded spatial-neighbor pass, not an all-pairs force loop.
  Canvas performance still depends on hardware and the loaded window.
- New reference indexes migrate existing databases and follow import/update/delete
  transactions. Structured exported thoughts, recap content, and browsing
  result/summary text become searchable; repair preserves raw JSON, occurrence
  keys, and canonical content hashes. Unsupported fields remain available in raw
  detail. The read-only `scripts/audit_import.py` report lists extraction gaps.
- Import still holds the largest individual JSON value/conversation in memory.
  Graph flag extraction reads full raw messages for loaded records. A huge single
  message/conversation can use substantial memory despite bounded node counts.
- `/api/basins` and `/api/neighborhood` implement the connected view. The retained
  `/api/atlas`, `/api/constellation`, `/api/lineage`, and `/api/parent-trace` endpoints
  preserve their earlier scopes for compatibility.
- Arbitrary agent-log schemas need explicit adapters. This local loopback server
  has no multi-user authentication/isolation or distributed ingestion. The graph
  does not modify ChatGPT memory or delete chats from an account.

## Measured workloads

Measured November 6, 2026 (UTC timestamps cross into November 7) on Apple M2 Max,
12 CPU cores, 96 GiB installed RAM, macOS 15.7.7, Python 3.14.7, SQLite 3.53.4.
These are actual generated files, imports, SQLite queries, and loopback HTTP calls.
They are not mocked responses. Source generation/import warms filesystem caches.
One run per variant is not a statistical performance study.

| Workload | Import + reconciliation | Python process peak RSS | Graph HTTP result |
| --- | ---: | ---: | --- |
| 80,000 branching records / 12 conversations / 38,180,881 bytes | 13.02 s | 171.44 MiB through checks | Overview: 0.536 s, 12 basins / 18 bridges |
| Same branching dataset, depth 4 / limit 300 | — | Included above | 0.0189 s; 83 occurrences / 182 edges; clipped |
| Same branching dataset, depth 6 / limit 900 | — | Included above | 0.0735 s; 366 occurrences / 790 edges; clipped |
| 2,147,490,402 bytes / 165,344 records / 10,334 conversations | 125.68 s | 225.75 MiB through import; 309.63 MiB through checks | Overview median 0.492 s; neighborhood median 0.00242 s |

The two workloads exercise different shapes. The 2 GiB source has many small
conversations and varied text lengths; it checks bounded overview pagination and
ordinary local neighborhoods. The 80,000-record source deliberately interconnects
branching conversations using metadata parents and shared message IDs, including
failed tools and exported thoughts. The benchmark asserts both bridge kinds,
separate occurrence identities, cross-chat traversal, thinking, errors, and bounds.
It does not claim to exhaust all 80,000 nodes in one browser frame.

The 2 GiB acceptance budgets were 180 seconds for import/reconciliation, 512 MiB
process RSS, five seconds per post-import HTTP response, and three seconds per
live probe. All 25 monitoring rounds passed. Correctness includes exact index/FTS
counts, 18 raw-record sample hashes, unchanged source SHA-256, SQLite `quick_check`,
FTS integrity, first/last ledger pages, text/ID/metadata searches, raw windows,
original-file search, both graph APIs, and reopening the index. Its database was
7,061,532,672 bytes; allow substantial additional disk space beyond source size.
RSS excludes browser memory and filesystem cache.

Full measurements pin the application, relationship resolver, benchmark programs,
and generated source using SHA-256:

- [80,000-record result](validation/connected-80k.json)
- [2 GiB result](validation/connected-2gib.json)
- [Initial 80,000-record failure](validation/connected-80k-baseline.json): overview
  timed out after 30 seconds. Replacing the large correlated join with covering
  index scans and bounded aggregation removed that failure. It remains recorded.

The 81-test regression suite covers siblings with no descendants, cross-chat
metadata parents, shared-ID conflicts, distinct mapping/message namespaces,
ambiguous roots, cycles, missing targets, fanout clipping, error hints versus
reported failures, reference rollback, old-index migration, raw/hash preservation,
and a consistent read snapshot while another connection commits. CI runs on
Linux/Python 3.10 and 3.14, macOS/Python 3.14, and Windows/Python 3.14. Linux 3.14
also runs real 64 MiB ingestion and the 80,000-record connected workload.

Native browser checks on the isolated invented dataset exercised basin → metadata
bridge → connected occurrences, depth expansion to 300 records, explicit export
comparison, conflicting shared-ID evidence, error labels, and original raw JSON
above the focus view. A one-record invented comparison dataset was added only
after the benchmark measurements; it is not part of the 80,000-record timings.

## Reproduce

Use fresh workspace directories; benchmarks refuse to overwrite prior results.
The measurement scripts use the Unix `resource` module; ordinary application
tests also run on Windows. Generated datasets/indexes remain in ignored `data/`.

```sh
python3 -m unittest discover -s tests -v
python3 scripts/benchmark_relationships.py --workspace data/connected-check --records 80000
python3 scripts/benchmark_large_import.py --workspace data/large-connected-check --size-mib 2048 --max-rss-mib 512 --max-import-seconds 180 --max-http-seconds 5
```

Add `--serve-port 8882` to the relationship benchmark to keep its isolated demo
available in a browser after checks pass. A separate CLI audit can report fields
that recognition or search extraction did not include without changing a source:

```sh
python3 scripts/audit_import.py /absolute/path/to/conversations.json --out /absolute/path/to/new-audit
```

Audit reports can include private content/identifiers and stay local. No personal
exports, working databases, or private lineage findings are included in this PR.
