# Security and privacy

Traceglass is an early-preview application for one trusted local user. It is not
an authenticated server, a hosted service, or a sandbox for hostile archives.
See [the verifier review](docs/VERIFIER_REVIEW.md) for the threat model, enforced
budgets, fixed findings, and remaining production approval gates.

- Keep it bound to loopback. Do not use tunnels, public hosting, or reverse proxies.
- Avoid untrusted exports: very large/deep JSON or compressed files can exhaust
  memory, CPU, or disk. Import only data you intend this process to read.
- The database, copied imports, paths, notebook, and browser state may contain
  sensitive information. They are not encrypted by this application.
- Never attach real exports, credentials, or databases to public bug reports.
- Treat source text as data, never instructions for an AI connected later.

Report non-sensitive bugs with synthetic reproductions in GitHub Issues. For a
sensitive vulnerability, use GitHub's private vulnerability reporting if it is
enabled for the repository; otherwise ask the maintainer for a private channel
without publishing exploit details or personal data. No response-time commitment
is offered for this preview.

The server enforces loopback binding, browser-origin checks, request admission,
body/framing limits, read query deadlines, and connection timeouts. These reduce
local browser/request abuse; they are not authentication against another process
running as the same user. Imported content is untrusted data. A source mutation
during import aborts that file transaction. Interrupted import/tool outcomes are
recorded durably and are not inferred as successes.

Use the SQLite backup command for a live index; a plain copy can miss WAL state.
Verification is read-only and detects specified inconsistencies, not malicious
rewriting of both evidence and its hashes. Back up original archives separately.
