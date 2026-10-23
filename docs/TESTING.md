# Testing

Every pull request runs these checks in CI (`.github/workflows/ci.yml`). Run them locally before you push:

```sh
.venv/bin/python -m ruff check .          # lint
.venv/bin/python -m pytest -q             # Python suite
npm --prefix frontend run typecheck       # TypeScript
npm run build                             # production build into src/brickpulse/server/static
npm --prefix frontend test                # Playwright end-to-end (needs Chromium, see below)
```

`npm test` at the repository root runs pytest and Playwright together.

## Python suite (`tests/`)

| File | What it protects |
| --- | --- |
| `test_regression.py` | The **frozen financial baseline**: findings, totals, ids, grouping and ordering on synthetic data must match `tests/fixtures/financial_baseline.json` exactly. It also checks that all 42 rule templates render (with and without the optional price sources), that scoring is pure and that estimates are bounded, and that pagination and detail views read a single snapshot |
| `test_breakdowns.py` | Savings breakdowns: overlapping exclusive options count once, additive options go to their own rules, advisory findings stay out, demo breakdowns add up to the headline |
| `test_pricing_mode.py` | `auto` / `list` / `account` modes: `list` never reads `account_prices`, `auto` uses it when available, `account` fails closed |
| `test_precompute.py` | Precompute source: optional-column discovery, cache keys per host, warehouse and scope, persisted JSON |
| `test_cache.py` | Bounded cache: TTL, weighted LRU, single flight, retry after failure |
| `test_i18n_api.py` | Language negotiation, parity of the three catalogs, localized API surfaces, fail-closed `sp` scoping, scope narrowing only |
| `test_deploy.py` | `scripts/deploy.py`: install, update and refresh flows, per-table grants, source checks, `--bind-existing`, remembered options |
| `test_deployment.py` | Bundle invariants: preflight tables match the rule SQL, owner-only app ACL, no default target or workspace values, the pipeline and job run the bundle wheel |
| `test_branding.py` | The app and favicon use the exact symbol in `docs/brand/` |

### Updating the financial baseline

The baseline is meant to change rarely and on purpose. If your change is *supposed* to change the numbers
(a new rule, a scoring fix), regenerate it and explain the diff in your PR:

```sh
PYTHONPATH=src BRICKPULSE_BASELINE_DATE=2026-10-21 \
  .venv/bin/python scripts/capture_financial_baseline.py brickpulse.server tests/fixtures/financial_baseline.json
```

Never regenerate it just to make a failing test pass.

## Frontend

- `typecheck` runs `tsc --noEmit`.
- `build` runs the type check, then Vite. The build writes into `src/brickpulse/server/static`. Commit the
  rebuilt output with any frontend change, because the deployed app serves those synced files.
- `frontend/tests/smoke.spec.ts` (Playwright) runs 14 scenarios on a desktop and a mobile viewport (28
  tests): the localized overview in all three languages, breakdowns that add up to the headline, loading
  states, titled and localized evidence charts, and workspace scope surviving navigation. It starts `npm run dev`, which runs the real FastAPI app in demo mode
  through `.venv/bin/python`, so the Python setup has to come first. Install the browser once with
  `(cd frontend && npx playwright install chromium)`.

## What the tests do not cover

All local tests run on synthetic data. **None of them run Spark or query real system tables.** The rule SQL
is rendered and checked structurally, but it is only executed for real in a workspace. After deploying a
change to rules, SQL or the pipeline, check it in a `dev` target:

- the pipeline and the tracker's first run complete;
- the app is `RUNNING` and its ACL is limited to the deploying user;
- identity, workspace scope and real data look right;
- languages and data freshness work;
- unavailable sources and switched-off rules match what `scripts/deploy.py` printed.
