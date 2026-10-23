# Configuration

**English** · [Español](../es/configuration.md) · [Português](../pt-BR/configuration.md)

Most installs only set the options exposed by `scripts/deploy.py`. Everything below is optional.

## Bundle variables

These are declared in `databricks.yml`. `scripts/deploy.py` writes them to
`.databricks/bundle/<target>/variable-overrides.json`. With the bundle CLI, pass them as `--var name=value`.

| Variable | Default | Description |
| --- | --- | --- |
| `catalog` | — (required) | Existing Unity Catalog catalog for the BrickPulse schema |
| `warehouse_id` | — (required) | SQL warehouse the app queries through |
| `schema` | `brickpulse` (`brickpulse_dev` on `dev`) | Schema for the BrickPulse tables |
| `app_name` | `brickpulse` (`brickpulse-dev` on `dev`) | Lowercase letters, digits and hyphens, at most 30 characters |
| `lookback_days` | `30` | Days of system-table history each refresh reads (1–90) |
| `pricing_mode` | `auto` | `auto`, `list` or `account`; see below |
| `disabled_rules` | `none` | Comma-separated rule ids to switch off. Set by the deploy's source check |
| `apps_tab` | `false` | Show the Apps inventory tab |
| `genie_costs_tab` | `false` | Show the Genie Costs tab |

## Pricing mode

`pricing_mode` is a per-install choice, not a per-user preference:

| Mode | Behavior |
| --- | --- |
| `auto` (default) | Uses `system.billing.account_prices` when it is readable, and list prices otherwise |
| `list` | Never reads `account_prices`. Every estimate uses list prices |
| `account` | Requires readable contract rates covering the analysis window. The pipeline update fails otherwise |

Contract estimates count only when the SKU, cloud, unit, currency and effective dates all match. For each
resource, if less than 95% of its usage is covered by contract rates, its estimate stays at list price, and
the contract total shows that fallback. The main dashboard always ranks on list prices so results stay
comparable. Contract estimates are shown separately and are not invoice amounts.

To change the mode, redeploy with the new value (`--pricing-mode`) and refresh. The pipeline and the app
must use the same value.

## Optional tabs

| Feature | How to turn on | Notes |
| --- | --- | --- |
| Apps inventory | `--apps-tab` | Lists Databricks Apps and their costs. Lifecycle events alone do **not** prove an app is idle, so no idle-app savings are claimed from them |
| Genie costs | `--genie-costs-tab` | Shows Genie space usage costs |
| Ask Genie | `BRICKPULSE_GENIE_ENABLED=true` plus a space | Off by default. Read [security](security.md#genie) before turning it on |

The deploy remembers each choice. Use `--no-apps-tab` or `--no-genie-costs-tab` to turn a tab off again.

## Environment variables

The bundle sets these on the app (see `resources/brickpulse.app.yml`). You only need to set them yourself
when running the backend outside the bundle.

| Variable | Default | Description |
| --- | --- | --- |
| `BRICKPULSE_COMPUTE_MODE` | `live` (`precompute` in the bundle) | `precompute` reads the pipeline tables; `live` runs the rule SQL on request; `demo` uses synthetic data |
| `BRICKPULSE_AUTH_MODE` | `obo` (`sp` in the bundle) | `sp` = app service principal with an explicit user scope map; `obo` = development persona only |
| `BRICKPULSE_USER_SCOPE_JSON` | empty (denies everyone in `sp` mode) | Who can use the app, and with what scope. See [security](security.md) |
| `BRICKPULSE_TABLE` | `main.finops.finops_recommendations` | Recommendations table. The other `finops_*` tables default to siblings of it |
| `BRICKPULSE_CATALOG` | `system` | Catalog holding the system tables |
| `BRICKPULSE_LOOKBACK_DAYS` | `30` | Lookback window |
| `BRICKPULSE_LIVE_LOOKBACK_DAYS` | `3` | Shorter window used in `live` mode |
| `BRICKPULSE_PRICING_MODE` | `auto` | Must match the pipeline |
| `BRICKPULSE_DISABLED_RULES` | empty | Comma-separated rule ids to switch off |
| `BRICKPULSE_HIDDEN_RULES` | `unused_scheduled_jobs,enable_photon_dbr` | Rules evaluated but hidden from the main lists |
| `BRICKPULSE_EXPERIMENTAL_RULES` | from the rule registry | Rules shown under Labs |
| `BRICKPULSE_PRECOMPUTE_CACHE_TTL` | `300` | Seconds the app caches precomputed reads |
| `BRICKPULSE_PRECOMPUTE_CACHE_MAX_ROWS` | `10000` | Maximum aggregated rows held in that cache |
| `BRICKPULSE_APPS_TAB_ENABLED` | `false` | Apps inventory tab |
| `BRICKPULSE_GENIE_COSTS_TAB_ENABLED` | `false` | Genie Costs tab |
| `BRICKPULSE_GENIE_ENABLED` | `false` | Ask Genie panel |
| `BRICKPULSE_GENIE_SPACE_ID` / `BRICKPULSE_GENIE_SPACE_URL` | empty | Genie space for Ask Genie |
| `BRICKPULSE_GENIE_MODE` | `iframe` | `iframe` embeds the space; `api` uses the Conversation API proxy |
| `DATABRICKS_WAREHOUSE_ID` | — | SQL warehouse (injected from the app resource) |
| `DATABRICKS_APP_PORT` | `8000` | HTTP port |
| `LOG_LEVEL` | `INFO` | Python log level |

Every `BRICKPULSE_*` variable also accepts the legacy `FINOPS_*` spelling. When both are set, `BRICKPULSE_*`
wins. A local `.env` file is loaded if present. It is git-ignored, so never commit it.

## Rule thresholds

What counts as waste lives in
[`src/brickpulse/rules/config/thresholds.yaml`](../../src/brickpulse/rules/config/thresholds.yaml). It has
one block per rule id plus a few engine-wide settings:

- `recency_days` (default 7): drops findings on resources with no billed usage in the last N days.
- `min_observation_days` (default 5): the minimum number of distinct active days before any rule may
  recommend an action.
- `enabled: false` inside a rule block switches that rule off.

Thresholds ship inside the wheel. After you edit them, redeploy and refresh (`scripts/deploy.py ... --refresh`).
Effort estimates live next to the thresholds in `effort.yaml`.

## Languages and currency

The UI language follows the browser, then the user's explicit choice, then English. Dates and numbers are
localized. Amounts are always USD, with no currency conversion. Rule summaries are translated. The full
technical evidence stays in its original English, so dynamic caveats remain auditable.
