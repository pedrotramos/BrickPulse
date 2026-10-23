# Architecture

This page covers how BrickPulse is put together, the decisions behind it and its operational limits. For
the financial contracts, see [DATA_MODEL.md](DATA_MODEL.md), [SAVINGS_CONFIDENCE_MODEL.md](SAVINGS_CONFIDENCE_MODEL.md)
and [ADDING_A_RULE.md](ADDING_A_RULE.md). Read [security](en/security.md) before you change identity or data
scoping.

## Components

```text
system tables ──► precompute pipeline (Lakeflow, serverless) ──► finops_* tables ──► FastAPI ──► React UI
                  rules: SQL templates + pure scoring            change tracker ─┘     (Databricks App)
```

There is a single Python package, `brickpulse`, with three domains:

- **`rules`**: the rule contract, Jinja SQL templates, threshold/effort configuration and pure scoring.
  Every rule is a plugin that the registry discovers.
- **`pipelines`**: the daily precompute pipeline and the change tracker that estimates savings from
  observed changes.
- **`server`**: FastAPI routes, identity, data sources, services, presentation and i18n.

The pipeline and the app share the same wheel. The React app (`frontend/src`) is organized by feature. It
uses TanStack Query for request state, cancellation and caching, React Router for navigation context and
i18next for languages. FastAPI serves both the API and the compiled SPA from `src/brickpulse/server/static`
in the same process, so production needs no Node runtime.

Materialized views keep batch semantics, and private intermediates avoid publishing helper tables. There
are no destructive MV/ST migrations and no forced full refreshes.

Production runs as the app's service principal with an explicit, fail-closed user scope map. The
development OBO mode and its personas are not production authorization.

## Finance and contracts

- USD is the canonical unit. Language never changes scoring, ranking, deduplication or amounts.
- Physical `finops_*` table names and finding/group identifiers are stable contracts. Schema changes are
  additive only.
- A frozen financial baseline (`tests/fixtures/financial_baseline.json`) pins findings, totals, ids,
  grouping and ordering on synthetic data. It doesn't validate the rule SQL against real system tables.
- Hidden rules, the governance sidecar and Labs (experimental) rules keep their classification and
  financial composition.
- The Apps inventory never produces "idle app" savings from lifecycle events alone.
- Savings history is an estimate based on observed changes over an explicit period. It is not a
  reconciled invoice.

## Internationalization

- **Resolution order:** the browser language, then an explicit persisted choice, then English as fallback.
- The API negotiates a weighted `Accept-Language` header and sends `Content-Language` and `Vary`.
- Localization works on a copy of the serialized JSON. It never mutates cached objects or persisted
  snapshots.
- Each rule has a title, a "why" and a summarized guidance in all three languages. Versioned message
  references and numeric parameters travel in the evidence JSON, so legacy rows stay readable.
- Dynamic instructions and technical evidence are shown in a section labeled as the original English
  text. That content is **not** translated sentence by sentence, and the UI doesn't claim it is.
- Evidence charts are generic adapters over the existing evidence contracts. They show selectable series
  or numeric comparisons, with access to the raw data.

## Performance decisions

- The groups page reads rows and summary from the same snapshot and leaves bulky evidence out of the
  listing.
- The resource detail page reuses one read for the main, governance and experimental partitions.
- A weighted LRU cache with TTL rejects oversized entries, evicts expired ones, coalesces concurrent misses
  (single flight) and never stores failures.
- Column-discovery caching expires and retries after a failure.
- The precompute cache defaults to a 300 s TTL and 10,000 aggregated rows. Tune it with
  `BRICKPULSE_PRECOMPUTE_CACHE_*` for large fleets.
- Secondary modules and charts are code-split, and fonts are self-hosted.
- Main tables paginate and filter on the server. Secondary analytical tables receive full aggregates and
  paginate 25 rows at a time in the client.
- Large technical evidence renders the first 100 array elements at first. The API keeps the full content.

## Operational limits

- No latency or cost benchmark on a real fleet exists yet, and nothing runs Spark locally.
- Coverage is limited by system-table grants and by the metastore region. Missing data must be told apart
  from zero, so check Data health and the last run before making decisions.
- The cache is per process, not distributed. Group pagination reduces payloads, but grouping still runs
  on the snapshot loaded in the server.
- `requirements.lock` captures the maintainers' local environment (macOS, Python 3.13). It is not a
  universal Linux lock. `requirements.txt` pins the direct runtime dependencies, and Databricks resolves
  compatible transitive dependencies for its runtime.
- Don't let two deployment mechanisms control the same resource. To move an older install under the
  bundle, bind it (`scripts/deploy.py --bind-existing`).

The pipeline illustration (`finops-pipeline.png` / `.svg`) shows the processing model. It doesn't reflect
the current source layout or the authentication guarantees.
