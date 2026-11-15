# Engineering review: local investigation infrastructure

**Deployment target: one trusted user on a local filesystem**, using the loopback
server and supported JSON export shapes. This implementation self-review records
the fixes, regression results, workload measurements, and recovery procedures.
Production deployment requirements are listed at the end of this report.

## Review basis

The review used [OWASP ASVS 5.0](https://asvs.owasp.org/downloads/) to organize web
and file-handling checks, [NIST SSDF 1.1](https://csrc.nist.gov/pubs/sp/800/218/final)
to organize verification and release evidence, and SQLite's
[corruption](https://www.sqlite.org/howtocorrupt.html) and
[backup](https://www.sqlite.org/backup.html) documentation for durability work.
The review applies selected controls from these references to the local deployment
target. [OWASP's assessment guidance](https://github.com/OWASP/ASVS/blob/v5.0.0/5.0/en/0x04-Assessment_and_Certification.md)
describes the process for a separate full ASVS assessment.

Threats considered: malformed/truncated/duplicate JSON, compression expansion,
source mutation/replacement, unsupported reference types, large conversation and
message payloads, incomplete windows, ambiguous IDs, derived-index corruption,
process interruption, competing writers, lost tool attempts, backup/restore,
cross-site browser requests, clickjacking, slow connections, request flooding,
ambiguous HTTP framing, sensitive query logging, and mutable CI dependencies.

## Findings and fixes

| Finding | Consequence | Implemented control and evidence |
| --- | --- | --- |
| Source hash was computed by reopening the path after parsing | Changed bytes could receive the wrong provenance claim | Hash the exact binary stream being decoded, including compressed bytes. Compare open-file and path identity/size/nanosecond timestamps before commit. Mutation and replacement tests require rollback. |
| A whole conversation was decoded at once | A single large chat could consume gigabytes of process memory | Incremental conversation decoding and a shared 8 MiB mapping-spool threshold; spill to private temporary files. Legal metadata-after-mapping order is retained. |
| Nested shadowed conversation mappings could serialize incorrectly during the new spool implementation | Conversation metadata could lose nested mapping contents | Explicit metadata materialization with a size budget; a regression compares retained metadata against the original fixture. |
| Decoding had no explicit value, nesting, expansion, or elapsed-time budgets | Deep/compressed/oversized inputs could exhaust resources | Input, decompressed-character, value, metadata, key-count, depth, and time guards; the raw compressed-byte reader also checks the deadline. Fail the file transaction explicitly. |
| Non-string children could be converted to invented string IDs | The graph could imply a relationship not declared by an identifier | Validate relationship/control-field types and lengths. Preserve valid raw records; reject unsupported types with the field named. |
| Graph queries decoded full raw messages | Node-count caps did not bound payload memory | Cache small event summaries during ingestion/migration; graph SQL fetches bounded previews. Invalidation requires repair and produces an explicit retryable error, never a hidden raw-message scan. |
| Ledger pages fetched full message payloads for previews | A small page could allocate a large corpus | Stream page rows, bound previews and parsed passage fields, label large-record passage limits, and restrict full-detail/legacy-graph byte budgets. |
| Raw search/seek work was unbounded and the cache conflated Unicode queries | Expensive gzip seeks or misleading cached matches | Scan byte/time limits, explicit partial-scan reasons, gzip seek cap, byte-accurate cache keys, file signatures, and cache locking. |
| HTTP threads and database reads were unbounded | Slow/repeated requests could exhaust resources | Admission before worker creation: 16 connections, 4 read queries; connection timeouts, SQL deadlines, bounded responses, and retryable busy errors. Health/progress bypass database read admission. |
| Cross-site reads and framing were insufficiently constrained | Browser-driven resource abuse, embedding, and ambiguous body handling | Host/Origin/Fetch-Site checks, frame denial, strict content lengths, no transfer-encoding ambiguity, partial-body rejection, and security headers on ordinary error responses. |
| Request logs retained search text and terminal control characters | Private query disclosure or log spoofing | Redact query strings, sanitize controls, and bound log messages. |
| Import status existed only in memory | Restart could conceal incomplete reconciliation | Durable import-run states and startup interruption recovery; partial datasets remain visibly unverified. The failed file rolls back; committed earlier files are retained and labeled. |
| CLI/server writers had no shared lease | Competing imports or migration could race | OS-backed nonblocking writer lease, released automatically on process death. Real second-process tests exercise exclusion and crash recovery. |
| Notebook attempts committed together with outcomes | A process interruption could erase the attempt | Commit the attempt before reading; recover unmatched attempts once, preserving request IDs and an explicit unrecorded-outcome label. Tool execution and startup recovery share the writer lease. |
| Live file-copy backup had no supported verification workflow | WAL state or notebook entries could be lost | SQLite backup API, exclusive new destination, private temporary snapshot, integrity check, checksum, and atomic publication. Restore test includes evidence pins and committed WAL data. |
| Reused integer occurrence keys lacked an index namespace in downloads | Evidence from different indexes could be confused | Persistent index UUID in graph evidence schema version 2; backups retain that identity. Source and original record IDs remain separate. |
| CI used mutable action tags | A reviewed tag could later reference different code | Pin both official actions to verified full commit IDs and disable persisted checkout credentials. See [GitHub guidance](https://docs.github.com/en/actions/reference/security/secure-use). |

## Validation evidence

Native browser checks on the release synthetic corpus exercised basin navigation,
literal metadata bridges, connected occurrences, and the focus workspace. These
manual checks supplement the automated suite.

The regression suite includes deterministic decoder differential vectors against
the standard JSON decoder, tiny-chunk Unicode/escape boundaries, malformed syntax,
an independent breadth-first graph oracle, real subprocess crashes, a second
process competing for the lease, network framing/admission/timeout tests, source
mutation, spool failure, cache invalidation, corruption detection, and backup
restoration. All fixtures are invented. No private exports or indexes belong in
the repository or CI artifacts.

The first cloud verifier run exposed a Windows portability failure in the source
change guard: Python's path `stat` and descriptor `fstat` can give different
meanings to `st_ctime_ns` on Windows. The guard now compares each API with its own
initial snapshot and separately checks file identity. A regression models the
differing timestamp namespaces; mutation/replacement tests also accept Windows'
OS-level denial of replacing an open file. See the
[CPython issue](https://github.com/python/cpython/issues/157671). The failed
[cloud run](https://github.com/starbrite-j/traceglass/actions/runs/42346980819)
is retained as part of the review evidence. A second Windows run exposed a
busy-socket shutdown issue: closing before consuming request headers could reset
the connection before delivering 503. The rejection path now drains a bounded
header and permits a bounded graceful close; the 503 test remains unchanged.
The [second failed run](https://github.com/starbrite-j/traceglass/actions/runs/42347327129)
is also retained. A final real CLI startup check then caught incorrect subcommand
registration that helper/API tests had missed. Direct help, verification, backup,
and real server-process startup tests now cover the command entry points; port 0
also reports its actual bound port. The first hosted macOS startup smoke exceeded
its initial 15-second readiness wait; the smoke now has a bounded 45-second startup
window and captures a child traceback if startup stalls. The
[timeout run](https://github.com/starbrite-j/traceglass/actions/runs/42348022971)
remains available. Inspection also found that Python's default HTTP server bind
performs reverse DNS before readiness. Numeric-loopback startup now bypasses that
unnecessary lookup, with a regression that makes any reverse-DNS call fail. This
removes a startup dependency; the first timeout did not capture enough evidence
to establish its cause.

The final local suite passed **114 tests**, including **400 decoder differential
vectors** and **50 generated graph-oracle cases**. Final workload measurements:

| Workload | Import / reconciliation | Process peak RSS through import | Result |
| --- | ---: | ---: | --- |
| 2,147,490,402 bytes; 165,344 records; 10,334 conversations | 130.23 s | 170.92 MiB | All acceptance checks passed |
| 80,000 connected branching records; 12 conversations | 16.18 s | 123.88 MiB through checks | Both bridge types, errors, thinking, and separate occurrences verified |
| 158,003,538-byte single conversation; 11,000 records | 8.40 s | 147.00 MiB | All 11,000 records checked for index consistency; source hash and 3 original-node samples verified |

The final 2 GiB run passed all 26 monitoring rounds and its 180-second import,
512 MiB process-memory, five-second HTTP, and three-second live-probe budgets.
Basin overview median was 0.515 s; connected neighborhood median was
0.0021 s (three requests each). Overall process peak RSS through checks was
244.05 MiB.

The performance files below pin the build immediately before the Windows-only
stat/rejection corrections, CLI registration fix, and numeric-loopback startup correction. The normal decoding, storage, and graph algorithms
are unchanged by those corrections. The complete 2 GiB source was replayed after
the timestamp correction, verifying its consumed-byte hash, all record counts,
and all 18 original-node samples. The replay checks source decoding; import timings
come from the pinned workload runs. See the [portable source replay](validation/verifier-portable-source-replay.json).

Current workload measurements and their exact code/source hashes are in:

- [Release 2 GiB workload](validation/verifier-release-2gib.json)
- [Release 80,000-record connected workload](validation/verifier-release-80k.json)
- [Release single-conversation workload](validation/verifier-release-conversation.json)

The workload programs generate real files, use actual imports and SQLite tables,
and call actual loopback HTTP endpoints. The single-conversation program generates
incrementally so source generation does not hide the importer's memory use.
Measurements include process high-water RSS; browser and filesystem-cache memory
are excluded. Workloads may run concurrently on the measured workstation, and
source generation warms caches. Each workload records one full acceptance run,
with the request sample counts stated above.

Earlier [connected graph](CONNECTED_LINEAGE_VALIDATION.md) and
[ingestion](LARGE_DATA_VALIDATION.md) reports retain their pinned historical
measurements and failed iterations. Their timings and limits do not describe
every later build.

## Enforced boundaries

| Area | Current bound / behavior |
| --- | --- |
| Source input | 20 GiB binary source; 20 GiB decompressed characters; regular files only |
| Decoded unit | 64 MiB characters; 128 nesting budget; 64 MiB aggregate record metadata; one million mapping keys; 4,096-character identifiers/control fields |
| Source processing | 1,800-second cooperative deadline, checked in decoded and raw binary reading |
| Mapping payload memory | Shared 8 MiB serialized-size spill threshold, plus the current decoded node, parser buffers, key index, metadata, and SQLite allocations |
| Network | Loopback only; 16 workers; 4 admitted read queries; 15-second normal connection timeout; 60-second upload timeout |
| HTTP JSON | 1,000,000-byte request body; investigation requests 100,000 bytes; 16 MiB encoded response budget |
| Database | WAL, foreign keys, `synchronous=FULL` for writes; 5-second writer lock wait; 10-second read SQL execution deadline |
| Detail / old graph | 8 MiB materialization budgets; explicit error directs large requests to bounded JSON windows or connected Lineage |
| Raw search | 256-character query; 5,000 matches; 4 GiB / 5-second scan budget; partial results explicitly labeled |
| Raw windows | At most 256 KiB; gzip offset at most 64 MiB; extract to inspect later gzip windows |
| Export comparison selector | Latest 100 datasets in the graph response; older comparison IDs remain addressable by API. Pagination for long-lived archives needs further UI work. |
| Connected graph | Existing node/hop/fanout/work bounds remain; see [relationship contract](CONNECTED_LINEAGE_VALIDATION.md#bounds-and-limits) |
| Private local files | Unix state directory mode 0700; spool/backup files private. Windows inherits filesystem ACLs; deployment ACL review remains required. |

Budgets reject unsupported work explicitly. Previews and graph windows identify
their bounds. Giant individual values can still consume substantial memory before
rejection; hard process-level containment is a separate deployment requirement.

## Use verification and backup

These commands operate on the normal local workspace. Verification opens the
database read-only and does not initialize, migrate, or repair it:

```sh
python3 app.py verify --sources
python3 app.py backup --out /absolute/private/path/new-snapshot.sqlite3
```

Verification checks SQLite integrity, foreign keys, record/content consistency,
event summaries, reference-index membership, full-text row/content membership,
incomplete imported datasets, and optional complete source hashes. Source-record
replay, FTS internal checks, and signed manifests are separate operations. A failed historical
attempt with no retained source is reported in history rather than treated as a
corrupt current corpus. Imported datasets without durable run history are labeled
`legacy_unverified` in the UI.

A backup includes the index, import history, annotations, and notebook. Keep source
archives separately. Restore into a **new, stopped workspace** with the snapshot
as `data/traceglass.sqlite3`, without unrelated old WAL/SHM files. Preserve the
original workspace, then run verification against the restored copy. Moving or
copying only a running database file is not the supported backup procedure.

Reproduce the local acceptance runs with fresh workspace paths:

```sh
python3 -m unittest discover -s tests -v
python3 scripts/benchmark_relationships.py --workspace data/review-connected --records 80000
python3 scripts/benchmark_streaming_conversation.py --workspace data/review-conversation --records 11000 --chars 12000
python3 scripts/benchmark_large_import.py --workspace data/review-large --size-mib 2048 --max-rss-mib 512 --max-import-seconds 180 --max-http-seconds 5
python3 scripts/verify_source_replay.py --workspace data/review-large --out data/replay-check.json
```

## Production deployment requirements

| Area | Required deployment evidence |
| --- | --- |
| Independent assessment | Review by people independent of implementation, a completed applicable control matrix, scoped penetration testing, and closure of findings. |
| Hostile-data containment | An isolated importer process/container with hard CPU/RAM/disk quotas, platform-specific escape testing, decompression/fuzz campaigns, and termination/recovery under adversarial load. |
| Identity and tenancy | Authentication, authorization, tenant isolation, session controls, and network ingress design for the intended deployment. The current server serves one trusted local user. |
| Confidentiality and custody | Encryption/key management, immutable original archive storage, signed manifests/audit records, retention/deletion policy, and access auditing. |
| Operational recovery | Restore drills on production-sized backups, off-device backups, measured RPO/RTO, real storage-full/I/O-fault/power-loss tests, and documented incident ownership. Process exits and injected spool failures cover only part of this. |
| Availability and capacity | Sustained/concurrent workloads on deployment hardware, realistic corpus distributions, repeated latency/resource measurements, alerting, graceful shutdown, and capacity planning. |
| Schema completeness | Versioned adapters and golden reference corpora for each lab's agent/tool logs, explicit trace/span/tool-call identity, and independently verified expected event/order counts. |
| Evidence interpretation | Preserve the source field behind each relationship and event label. Evaluate semantic clustering separately if introduced. |
| Supply chain / release | Signed release artifacts, runtime/SBOM and vulnerability management for Python/SQLite/OS, reproducible deployment configuration, and rollback/change ownership. |
