# Contributing

Small, reproducible improvements are welcome. Open an issue describing the observed
behavior, expected behavior, Python/OS/browser versions, and a **synthetic** example.
Do not submit private conversation exports or real account identifiers.

Before proposing a change:

1. Keep original sources read-only and retain conflicting evidence.
2. Add a regression test using invented records and a temporary database.
3. Run `python3 -m unittest discover -s tests -v`.
4. For UI changes, walk the affected flow in a browser on an isolated demo index.
5. Document limits honestly. A structural marker is not a claim about runtime cause.

Avoid new external services or dependencies unless the change requires them and
the privacy implications are explicit. Keep personal JSON import working without
API keys or editing code. Contributions are provided under the repository's MIT
license.
