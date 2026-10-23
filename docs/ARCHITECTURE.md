# BrickPulse architecture

The current architecture, performance decisions, localization boundaries and operational limits
are documented in [BRICKPULSE_ARCHITECTURE.md](BRICKPULSE_ARCHITECTURE.md).

The shared Python package contains `server` (FastAPI, identity, data sources and services),
`rules` (SQL templates and pure scoring) and `pipelines` (daily snapshots and change tracking).
The React application lives in `frontend/src`, organized by feature. Its production build is
served by FastAPI from `src/brickpulse/server/static`, with no Node runtime required.

Production runs as the application's service principal with an explicit, fail-closed user
scope map. Development OBO/personas are not production authorization. See
[AUTH_AND_SCOPING.md](AUTH_AND_SCOPING.md) before changing identity or data scoping.

Physical `finops_*` table names and finding/group identifiers remain stable. See
[DATA_MODEL.md](DATA_MODEL.md), [SAVINGS_CONFIDENCE_MODEL.md](SAVINGS_CONFIDENCE_MODEL.md)
and [ADDING_A_RULE.md](ADDING_A_RULE.md) for the preserved financial contracts.

The legacy pipeline illustration (`finops-pipeline.png` / `.svg`) describes the original
processing model, not the current source-directory layout or authentication guarantees.
