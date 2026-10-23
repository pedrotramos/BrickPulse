# Data model — the recommendation contract

One recommendation is described by a single schema with **three mirrors that must
stay in sync**. Changes are **additive only** (append columns; never rename/repurpose).

| Mirror | File |
|--------|------|
| Producer (rule output) | `src/brickpulse/rules/contract.py` → `RuleFinding` |
| API model (source of truth) | `src/brickpulse/server/models/recommendation.py` |
| Delta table (precompute) | `src/brickpulse/pipelines/ddl/finops_recommendations.sql` |

## Columns
| Column | Type | Meaning |
|--------|------|---------|
| `recommendation_id` | STRING | `sha256(rule_id\|workspace_id\|resource_id\|snapshot_date)[:32]` — idempotent key |
| `snapshot_date` | DATE | day the recommendation was computed |
| `rule_id` / `rule_version` | STRING | which rule + its logic version (lets you re-score history) |
| `rule_title` | STRING | denormalized for display |
| `category` | STRING | `compute` \| `sql` \| `jobs` \| `serving` \| `pipelines` \| `governance` |
| `workspace_id` / `workspace_name` | STRING | scoping key + label |
| `workspace_url` | STRING | base URL of the resource's own workspace, from `system.access.workspaces_latest`; the UI deep-links to this host (the account spans many workspaces, each on its own host) |
| `resource_type` | STRING | `cluster` \| `warehouse` \| `job` \| `serving_endpoint` \| `pipeline` |
| `resource_id` / `resource_name` | STRING | the exact resource |
| `resource_owner` / `resource_creator` | STRING | who owns / created the resource, resolved at precompute time from `billing.usage.identity_metadata` (`owned_by`/`run_as` → owner, `created_by` → creator). **Nullable** — some resource types carry no identity on their usage rows; an unknown owner never suppresses the finding. See `src/brickpulse/server/data/owner_sql.py`. |
| `why` | STRING | why it wastes money (human explanation) |
| `how` | STRING | the action to take |
| `monthly_waste_usd` | DECIMAL(12,2) | the resource's own billed spend / month (see "Total workspace spend" below — this is NOT an account-level waste figure) |
| `monthly_savings_usd` | DECIMAL(12,2) | **point** estimate of $ saved if applied (kept for compatibility) |
| `monthly_savings_low_usd` | DECIMAL(12,2) | conservative lower bound; the **only** value the booked headline totals, and only for `savings_status='bookable'` rows. `<= monthly_savings_usd`. Defaults to the point estimate until a rule widens the band |
| `monthly_savings_high_usd` | DECIMAL(12,2) | upper scenario; shown for range context, **never** booked. `>= monthly_savings_usd` |
| `savings_status` | STRING | `bookable` (its `low` is booked into the headline lower bound) \| `estimated` (shown & rankable, contributes 0 to the booked total) \| `advisory` (no dollars booked). Currently `bookable` = `cluster_autotermination` + `idle_serving_endpoints` (zero-traffic tier only) + `interactive_to_job` (job-DEDICATED clusters only, per finding); most others are `estimated` (right-size / auto-stop family, plus `interactive_to_job`'s shared-cluster default tier, `idle_zombie_clusters` while a cluster is still running idle, and `failing_jobs` REPAIR findings) or `advisory` (photon, spot, (un)used jobs, serverless, governance, plus `idle_zombie_clusters` for a STOPPED cluster whose forward saving is $0, and `failing_jobs` RETIRE findings for a job that never succeeded — booked only on an owner/obsolescence decision); `idle_apps` is disabled entirely |
| `effort_band` / `effort_score` | STRING / INT | trivial/low/medium/high and its weight (1/2/3/5) |
| `priority` | STRING | HIGH \| MEDIUM \| LOW |
| `nba_score` | DOUBLE | Next-Best-Action rank = `savings / effort_score × priority_mult` |
| `evidence` | STRING (JSON) | raw metrics that triggered the rule |
| `computed_at` | TIMESTAMP | run timestamp |
| `price_basis` | STRING | `contract` (has a contract-rate figure) \| `list` (contract data existed, but < `contract_min_coverage` of this resource's usage had a contract price — shown at list with a marker) \| NULL (no contract data this run). See "Contract rates + cloud VM cost" below |
| `contract_price_coverage` | DOUBLE | share (0–1) of the resource's usage priced by `system.billing.account_prices` (and list) |
| `monthly_savings_contract_usd` / `_low_usd` / `_high_usd` | DECIMAL(12,2) | the savings point / band at contract rates; NULL below the coverage gate |
| `cloud_cost_30d_usd` | DECIMAL(12,2) | the resource's classic cloud VM cost (`system.billing.cloud_infra_cost`), NULL without data |
| `monthly_cloud_savings_usd` | DECIMAL(12,2) | the cloud VM saving of the action — a separate layer, **never** added to the Databricks dollars |
| `finding_key` | STRING | `sha256(rule_id\|workspace_id\|resource_type\|resource_id)[:32]`, lowercase hex. Snapshot-independent (unlike `recommendation_id`), one per rule × resource, so a link built from it still works tomorrow. See "Finding key" below |

## Finding key
`finding_key` (`src/brickpulse/server/finding_key.py`) identifies one rule's finding on one
resource across snapshots. `recommendation_id` changes every day (it includes
`snapshot_date`); `finding_key` doesn't, which is what lets the "Saved so far" tracker
follow a finding from day to day, and what makes a shared link stay valid:

* **Deep links:** `/resources/<group_id>?focus=<finding_key>`; `focus` also accepts a
  `recommendation_id`.
* **Old snapshots** without the column still load: the model derives `finding_key`
  from the four parts.

## Contract rates + cloud VM cost
Configurable with `pricing_mode: auto | list | account` (`thresholds.yaml`, overridden
by pipeline setting `brickpulse.pricing_mode`). `auto` probes `billing.account_prices`
for rates overlapping the analysis window and uses it when accessible; `list` never
reads that table; `account` requires accessible matching rates and fails the update
otherwise. `billing.cloud_infra_cost` is an independent optional layer. The app's
`BRICKPULSE_PRICING_MODE` should match the pipeline setting; the deployment notebook
sets both. Without contract rates, rule SQL remains list-only.

* **List stays the complete, ranking basis.** Rules compute list savings exactly as
  before; NBA order never changes with the basis.
* **Contract figures are derived in one place.** The SQL macros (`contract_cost_cols`,
  `contract_price_join`) emit two scale-invariant ratios per resource —
  `contract_ratio` (contract $ / list $ over doubly-priced usage) and
  `contract_price_coverage` — and the engine scales point/low/high by the ratio
  (`Rule.contract_pricing = "ratio"`). Price-gap rules (`interactive_to_job`) price both
  SKUs at contract rates themselves (`"counterpart"`); $0 rules opt out (`"none"`).
  `enable_serverless` is counterpart but prices from configured tier rates, so it has
  no contract figure yet (shown at list with the marker).
* **Coverage gate:** a contract figure only when coverage ≥ `contract_min_coverage`
  (0.95, `thresholds.yaml`). A total at "Your rates" is therefore BLENDED (contract
  where available, list otherwise) and is always shown with its split and a coverage chip.
* **Cloud VM** (`Rule.cloud_savings`): `proportional` = cloud cost × (list savings /
  list cost); `full` = the whole classic VM (moving to serverless). Separate KPI only.
* The global `pricing_mode: effective` is retired (it would contradict per-finding bases).
  Even in `account` mode, a finding below the coverage gate remains explicitly at
  list price. This is not invoice reconciliation or a guaranteed realized saving.
* Aggregates: `AccountOverview.priced` / `WorkspaceDetail.priced` (`PricedTotals`),
  `RecommendationGroup.monthly_savings_contract_usd`, `/api/pricing` (`PricingSummary`,
  reports whether contract rates are available; `default_basis` = contract only at
  ≥ 95 % coverage). The current UI keeps list-price KPIs as the comparable baseline
  and shows contract-rate estimates separately in Overview and resource detail.
* **Not yet validated on real contract data.** Treat contract figures as a preview.

## Minimum observation window — "too new to judge"
Every savings figure is a *sample* extrapolated to a month, which is only
meaningful once the sample is representative. A cluster created yesterday
afternoon that idled for six hours is statistically indistinguishable from one
that has idled for a month, so recommending "terminate this zombie" on that
evidence is sampling noise, not a finding.

Each rule's SQL therefore emits **`observed_days`** — distinct calendar days the
resource was seen *active*, measured on that rule's own primary evidence source
(node-timeline samples for clusters, `query.history` for warehouses, run/write
events for jobs) — and every `score()` calls
`finops_rules.history.insufficient_history(row, ctx)` **first**, before any
pricing, suppressing rows below the bar.

- Threshold: **`min_observation_days`** (default **5**) at the *top level* of
  `config/thresholds.yaml`. Engine-wide, not per-rule: "too new to judge" is a
  property of the resource and its data, not of the criterion being applied.
- **Distinct days, not a contiguous streak** — a weekday-only resource observed
  Mon–Fri has five days of solid evidence; requiring an unbroken run would
  suppress exactly the well-understood resources we most want to report on.
- Clamped to `lookback_days` at runtime, since a 3-day window could never yield 5
  observed days. Live mode's window is floored at the minimum for the same reason.
- A **missing** `observed_days` (older snapshot, or a rule predating the guard)
  reads as *unknown → don't block*, so the column can never silently empty the app.
- The sample size is surfaced, not just enforced: `observed_days` /
  `min_observation_days` land in `evidence`, and `data_basis` states them in prose.
- `unused_scheduled_jobs` is deliberately exempt — it fires on the *absence* of
  runs, so its `min_days_since_last_run` (30) already *is* a history requirement.

## Savings, effort, and NBA
- **Savings $**: computed per-rule in `score()` from `usage_quantity × list_prices`
  attributed to the resource, normalized to a 30-day month (`pricing.to_monthly`).
- **Effort score**: `EffortBand` → numeric via `config/effort.yaml` (trivial 1 …
  high 5), so effort is comparable across rules.
- **NBA score** (`services/nba_ranker.compute_nba`): `savings / effort_score`,
  times a priority multiplier (HIGH 1.25 / MEDIUM 1.0 / LOW 0.8). Stored so live
  and precompute rank identically; multipliers/effort weights are config, so the
  tradeoff is re-tunable without recompute.

### Resource groups and the Recommendations page (read-time, no stored shape)
`GET /api/recommendation-groups` returns one `RecommendationGroup` per resource
(options savings-sorted; group savings composed per spend pool, never a sum).
Optional params: `effort` and `status` (csv) filter
**findings before grouping**, so the group's figures are recomputed on the matching
options; `q` is a case-insensitive search over resource name/id, workspace name,
and owner; `view` is a quick-view preset (`all`, `quick_wins` =
trivial/low effort AND bookable/estimated, `booked`, `high_priority`, `advisory`)
intersected with the explicit filters (unknown views are ignored); `sort` accepts
`nba` | `savings` | `effort` (easiest band, then savings). `priority`, `rule_ids`
and `min_savings` stay group-level. `GET /api/recommendation-groups/summary` takes
the same params and returns `{resources, potential_usd, quick_wins: {count,
value_usd}, advisory_only, views: {all, quick_wins, booked, high_priority,
advisory}}` (`models/group.RecommendationGroupsSummary`); `potential_usd` is the
"Savings potential" formula (`aggregation._addressable_savings_total`) and each
view count applies every current filter except the view. The UI's strip computes
the same totals client-side (basis-aware) and uses the endpoint for the chip counts.

## The `evidence` JSON — "show the data"
`evidence` is a free-form JSON blob, so it extends **without a schema migration**.
Beyond the raw trigger metrics, rules populate these standardized keys
(builders in `src/brickpulse/rules/evidence.py`):

| Key | Meaning |
|-----|---------|
| `confidence` | `high` \| `medium` \| `low` — how *directly* platform data backs the estimate, driven by which tables were available (not by dollar size). E.g. cluster right-size reads per-minute CPU (`high`); warehouse right-size has no CPU signal and is inferred from query concurrency (`low`). Rendered as a pill on the detail page. |
| `data_basis` | One honest sentence: which system tables backed it, the lookback, and where we rounded down. Shown verbatim under the chart. |
| `data_warning` | *Optional.* The rule's own statement that the evidence may not show what it seems to — e.g. `idle_serving_endpoints` on an endpoint that has **never** logged a request, where zero traffic can't be told apart from usage tracking being off. Rendered as a warning callout ABOVE the action on the detail card plus an "Unverified" tag on list rows. A rule setting it should also lower confidence and not book the saving. |
| `viz` | A compact, self-describing chart payload the UI switches on via `viz.kind`. |
| `phasing` | *Optional.* The step plan of a capacity-reducing finding (see "Phased downsizing" below). The UI renders it generically. |
| `savings_curve` | *Optional.* `[{"value": minutes, "monthly_usd": …}, …]`: the finding's own saving at each candidate setting, so the "Saved so far" tracker can value a **partial** move (see "Savings tracker" below). Emitted by the two auto-stop rules at 10, 15, 20, 30, 45, 60, 90, 120 min (`savings_curve_minutes` in `thresholds.yaml`), keeping only values from the proposal up to (not including) today's setting. Sorted ascending; the first point is the proposal and equals `monthly_savings_usd`; every later point is clamped to [0, the previous point], so the curve is monotone and never claims more than the estimate (`savings.savings_curve`). `warehouse_autostop` prices the exact-interval counterfactual at each value in the **same** SQL pass as the target (`curve_minutes` / `curve_reclaimable_minutes`, no extra scan) and emits a curve only on the interval-priced path. `warehouse_autostop_serverless` derives it from its gap histogram (net reclaim, restart penalty deducted; above 60 min the open-ended bucket is keyed at its mean gap, so 90/120 are approximate). Readers must tolerate its absence (older snapshots, the hourly fallback): the tracker then values linearly. Not rendered by the UI. |

`viz.kind` values today:
- **`hour_heatmap`** — day×hour grid of resource state (`0` stopped / `1` warm+serving / `2` warm+idle); used for warehouse auto-stop. Carries `reclaimable_hours`.
- **`burst_compare`** — query-load series + `warm_floor`; classic-vs-serverless comparison. Carries `idle_pct`, `cold_start_s`.
- **`cpu_area`** — per-minute CPU series + `p95`; cluster idle / right-size (unused headroom shaded).
- **`cluster_daily`** — per-day active vs idle hours across the lookback; cluster idle / auto-termination.
- **`job_runs`** / **`write_no_read`** / **`wh_rightsize`** — the G1/G2/G3 rule charts.
- **`autostop_tradeoff`** — the serverless auto-stop *decision*: the warehouse's idle-gap histogram, every candidate auto-stop value with what it reclaims vs the extra restarts/day it introduces (and whether it was rejected), and the hourly query rhythm. This is what makes the recommended value auditable instead of asserted.
- **`cluster_resources`** — multi-metric cluster utilization (CPU, memory, swap, I/O wait, network) on one axis, plus `cluster_kind` / `node_type` / `worker_count`. CPU alone cannot distinguish "oversized" from "memory-bound" or "I/O-starved".

A finding may carry **more than one chart**: `viz` is the primary one and the
optional `viz_extra` array holds additional charts rendered after it (the cluster
rules use this to show both "when did it run" and "what was it doing"). `viz_extra`
is additive — every existing consumer of `viz` is unaffected.

The demo source (`data/demo_source.py`) and the live rule `score()` build these
through the **same** builders, so the charts render identically offline and in
production. Keep payloads small — they live inside the `evidence` column.

### Phased downsizing — `evidence.phasing`
A rule that lowers capacity users can feel (a warehouse's size or min clusters, a
cluster's workers, a pool's warm instances) recommends ONE bounded step now and the
rest as a plan (`src/brickpulse/rules/phasing.py`; policy in docs/ADDING_A_RULE.md "Rules that
reduce capacity"). The finding **leads with the full saving**:

| Field | Meaning |
|-------|---------|
| `monthly_savings_usd` = `monthly_savings_high_usd` | the saving at what the data supports (`data_target`) |
| `monthly_savings_low_usd` | the first step's saving alone |

```jsonc
// evidence.phasing — present only on capacity-reducing findings
{
  "setting": "Minimum clusters",       // the setting's name, as shown in the UI
  "current": 30,                       // number, or a ladder size string ("X_LARGE")
  "recommended_step": 10,              // == plan[0]; what `how` says to set now
  "data_target": 1,                    // what the data supports
  "plan": [10, 4, 2, 1],               // steps AFTER current; last == data_target
  "observe_days": 7,
  "watch": "the queued-query share and p95 query latency",
  "step_savings_usd": 9800.0,          // == monthly_savings_low_usd
  "target_savings_usd": 14200.0        // == monthly_savings_usd == monthly_savings_high_usd
}
```
`plan.length == 1` is a single safe step (data target == step, collapsed band). Every
reader treats every field as optional. Rules emitting it today:
`warehouse_min_clusters` (3x per step, step priced in SQL as `step_excess_cluster_min`),
`warehouse_rightsize` (one ladder rung per step; measured-slot tier only — the inferred
tier measures no target and its low end is $0), `rightsize_underutilized` (halving,
`phasing_max_step_ratio: 2`) and `instance_pool_idle` ($0). Timeouts (auto-stop,
auto-termination) are not phased. Defaults: the top-level `phasing:` block of
`thresholds.yaml` (`max_step_ratio: 3`, `observe_days: 7`), surfaced as
`RuleContext.phasing`.

## The output table
Written by the **Spark Declarative Pipeline** (`src/brickpulse/pipelines/pipeline.py`), which owns
all of its output tables. The app reads the latest `snapshot_date`.

**It is a MATERIALIZED VIEW, not a Delta table** — that is what `@dlt.table`
creates, and it has two consequences worth knowing before you touch this:

* **No day-over-day history.** An MV is fully recomputed on each pipeline update,
  so the table holds *only the current* `snapshot_date`. A trend chart over
  snapshots would need a real Delta sink (an `append`-mode `@dlt.table`, or a
  downstream table fed from the MV). The "Saved so far" tracker keeps its own
  history in plain Delta tables (see below).
* **Delta-only writes fail against it.** Don't write to these tables yourself.

`snapshot_date` is the read key and the DDL files in `src/brickpulse/pipelines/ddl/` document the
contract, but nothing creates the tables from them: the deploy notebook leaves that
to the pipeline, since a pre-created table makes it fail to materialize.

## Total workspace spend — `finops_workspace_spend`
The recommendation table only knows about resources a rule flagged, so it cannot
answer *"what does this workspace cost?"* — the figure a platform admin anchors on.
A sibling table (DDL: `src/brickpulse/pipelines/ddl/finops_workspace_spend.sql`) carries the
whole billed spend per workspace over the lookback window, plus a `by_sku` JSON
breakdown and the serverless/classic split (which the About Serverless view uses).

| Column | Meaning |
|--------|---------|
| `snapshot_date` / `workspace_id` | partition + scoping key |
| `total_spend_usd` / `monthly_spend_usd` | window spend, and the same normalized to 30 days so it is comparable with the rules' monthly savings |
| `total_dbus` | usage quantity behind that spend |
| `by_sku` | JSON `[{sku_group, spend_usd, dbus}]`, biggest first |
| `serverless_spend_usd` / `classic_spend_usd` | how much of the estate is already serverless |
| `total_spend_contract_usd` | BLENDED spend at contract rates where priced, list otherwise (NULL without contract data) |
| `contract_priced_share` | list-$ share of `total_spend_usd` that had a contract price — always shown with the blended figure |
| `total_cloud_cost_usd` | classic cloud VM cost (`billing.cloud_infra_cost`); never added into the DBU spend |

### The breakdown groups on `billing_origin_product`, not `sku_name`
`by_sku` is named for history; the grouping key is the **product**, and that matters:
**Databricks Apps bill under `PREMIUM_ALL_PURPOSE_SERVERLESS_COMPUTE_<REGION>`** — a
SKU whose name contains no hint of "app". So do Vector Search, Lakebase and Model
Serving variants. Grouping on SKU-name substrings would pool all of them into a
catch-all `'Serverless (other)'` bucket, which on a large account can be the
**single largest line item**. A breakdown whose biggest row is "other" explains
nothing.

`_SKU_GROUP_SQL` therefore keys on `billing_origin_product` and falls back to
`initcap(billing_origin_product)` for a product Databricks adds later, so a new
surface appears as itself the first time it bills instead of vanishing into "Other".
`sku_name` is still used for the serverless/classic distinction **within** a product
(a serverless and a classic SQL warehouse share the product `SQL`), and for the
`serverless_spend_usd` / `classic_spend_usd` split, which is about compute type
rather than product.

**Read path** (`data/spend_source.py`): the snapshot table first; a **live
`system.billing.usage` aggregate** as a fallback when the table is missing or empty
(a fresh deployment before its first precompute run, or live/dev mode); and if both
fail, `unavailable: true` so the UI **omits** the figure rather than rendering a
confident `$0`. The SQL is defined once in `data/spend_sql.py` and used by both the
pipeline (`pipeline.py::_build_spend`) and the fallback, so they agree by construction.

`monthly_waste_usd` on a recommendation is the **resource's own billed spend** — the
UI labels it "This resource's spend / month". The old account-level "wasteful spend"
KPI (a sum of per-rule waste estimates) has been removed: it read like a bill it
never reconciled with. Total spend replaces it as the denominator for savings.

## Hidden rules
`Settings.hidden_rules` (env `FINOPS_HIDDEN_RULES`, default
`unused_scheduled_jobs,enable_photon_dbr`) lists rules that are **computed and
stored but hidden from the entire GUI** — no list, group, chart, savings total or
NBA ranking. The filter is applied in `RecommendationSource.fetch`, the single
boundary every read path crosses; `RecommendationSource.load` bypasses it, and the
About Serverless view deliberately uses `load` because the old-DBR findings are its
evidence. This is a presentation switch, not `enabled: false` in `thresholds.yaml`:
the pipeline keeps collecting the data.

## Genie costs — `finops_genie_usage`

Genie spend is reported in its own tab (`/genie-costs`) and is **deliberately
outside the recommendation contract**: it produces no finding, no savings figure,
no effort/priority and no NBA score, and it contributes to no total anywhere else
in the app. It is analysis only. Attribution is to **people and spaces**, not to
compute resources, so it fits neither the recommendation shape (one row per
finding) nor the spend shape (one row per workspace).

### What the billing data actually looks like
Three facts drive the whole design (all verified live against `system.billing.usage`):

1. **The free tier is a separate SKU, not a discount.** `GENIE_FREE_USAGE` has **no
   row at all** in `billing.list_prices`, so it prices to exactly `$0`. The
   free-vs-paid split is therefore *read from billing*, never inferred from a spend
   threshold — which is why the tab can state "N% never went paid" as a fact.
2. **Paid Genie does not mention "genie" in its SKU name.** It bills as
   `PREMIUM_SERVERLESS_REAL_TIME_INFERENCE_<REGION>` — the inference SKU. A
   `sku_name LIKE '%GENIE%'` filter silently returns *free usage only*, making paid
   spend look like zero. **Always filter on `billing_origin_product = 'GENIE'`.**
3. **`usage_metadata.genie.agent_id` is populated for `GENIE_AGENTS` only.** Genie
   Code and Genie One emit no space id, so per-space cost legitimately covers a
   subset of spend. `genie_space_sql` filters to rows that have one rather than
   inventing an "unknown space" that would own most of the money.

Other columns used: `usage_metadata.genie.surface`
(`GENIE_CODE`/`GENIE_AGENTS`/`GENIE_ONE`), `.channel` (`UI`/`API`), and
`identity_metadata.run_as` for the principal.

### Budgets are not available
`usage_metadata.budget_policy_id` is **NULL on every Genie usage row**, and account
budget policies are not readable from a workspace-scoped app. Set budgets therefore
**cannot be shown from system tables**. The tab renders an explicit "not available"
panel (`GenieCosts.budgets_note`) instead of an empty widget that looks broken.

### Table shape — one table, four aggregates
DDL: `src/brickpulse/pipelines/ddl/finops_genie_usage.sql`. Written by the **SDP pipeline**
(`src/brickpulse/pipelines/pipeline.py::_build_genie`), a sibling sink of `finops_recommendations`.
Each row is `(snapshot_date, kind, workspace_id,
payload)` where `kind` ∈ `user | surface | space | daily` and `payload` is the
aggregate row as JSON. One table keeps the analysis-only surface cheap to evolve (a
fifth aggregate needs no migration); `workspace_id` stays a real column because
scoping must never depend on parsing JSON.

The four queries live once in `src/brickpulse/server/data/genie_sql.py` and are used by both
the pipeline and the live fallback — the same one-definition rule the rules follow.

### Read path
`data/genie_source.py`: snapshot → concurrent live queries → honest `unavailable`,
mirroring `spend_source.py`. Two things to know before changing it:

- **The live window is truncated** to `live_lookback_days` (like
  `LiveRecommendationSource`) because the path scans `billing.usage` while a page
  load waits. `GenieCosts.lookback_days` reports the window *actually queried*, and
  the monthly figure divides by it — do not report the configured window for a
  truncated read.
- **The time budget only works because the pool is shut down with `wait=False`.**
  Collecting results inside a `with ThreadPoolExecutor` block joins every worker on
  exit, so a straggler holds the response open no matter what the budget says; that
  turned a 45s budget into a >5-minute request on a real account. The budget is also
  set *above* the connection's 40s socket timeout, since that already bounds each
  concurrent query — a lower budget can only cancel queries that were about to
  succeed. `missing_sections` names any aggregate that did not arrive so the UI can
  explain an empty section instead of implying "no usage".
- **The snapshot read must select the workspace columns explicitly.**
  `workspace_id`/`_name`/`_url` are real columns and the pipeline *removes*
  `workspace_id` from the JSON payload (scoping must never parse JSON). A read of
  just `(kind, payload)` therefore loses every workspace attribution while each
  individual figure still looks right (the tab would show *"across 0 workspaces"*
  with unnamed spaces). The row's columns are merged back over the parsed payload in
  `_from_snapshot`.

### Per-workspace scope
Genie respects workspace scope in both directions:

* **A workspace admin** sees only their own workspaces on `GET /api/genie` — the
  scope is server-derived and the snapshot query filters on the real `workspace_id`
  column.
* **A platform admin can drill in** via `GET /api/genie/{workspace_id}`
  (route `/workspaces/:workspaceId/genie-costs`), mirroring
  `GET /api/spend/{workspace_id}`. All four aggregates are already grouped by
  `workspace_id` in SQL, so this is the same rollup over a narrower scope, not a
  second code path.

The path param is client-supplied, so it goes through `Scope.narrowed_to`, which
applies it as a filter *within* the caller's scope and can only ever remove access:
a workspace admin naming a workspace they don't own gets an empty scope (rendered as
`IN (NULL)`, and short-circuited before any query runs), never that workspace's
data. A non-numeric id raises `ScopeError` → **400**, since ids are inlined into SQL
after a digit check. The source caches per scope, so the platform view and each
drill-in can never serve each other's rows.

## Databricks Apps — `finops_apps_usage`
An **inventory, not a rule set**: what apps exist, what they cost, how they are
configured. Written by the SDP pipeline (`pipeline.py::_build_apps`), read by
`data/apps_source.py`, rendered at `/apps`. DDL:
`src/brickpulse/pipelines/ddl/finops_apps_usage.sql`.

### Why there are deliberately NO app recommendations
`system.access.audit` with `service_name='apps'` contains **lifecycle actions only** —
`createApp`, `deployApp`, `startApp`, `stopApp`, `updateApp`, `deleteApp`,
`changeAppsAcl`, `installTemplateApp` (verified live 2026-08-06). **There is no app
request or page-view log anywhere in system tables.** So the question every
idle-resource rule must answer — *is anyone actually using this?* — is unanswerable
for apps, and an "idle app" or "rightsize this app" finding would be a guess wearing
a dollar figure.

Contrast `idle_serving_endpoints`, which exists **because**
`system.serving.endpoint_usage` is a genuine per-request log. That rule also shows the
shape the future apps rule must take: it requires an `entity_matched` flag so that
absence of traffic data reads as *unknown*, never as *idle* — without it, a LEFT JOIN
made busy endpoints look like zero-traffic ones and would have recommended deleting
them.

So the tab shows facts and states the limitation (`analysis_note`, rendered verbatim
in a panel) rather than omitting the surface or inventing advice. The model carries no
`monthly_savings_usd`, `priority`, `nba_score` or `effort_*` field at all — if one ever
appears, some UI will render it as advice we cannot justify.

**Where the rule would go when a usage log lands:** `apps_inventory_sql` already
computes `days_billed`, `compute_size` and `last_deploy_time`, which is most of a
rightsizing/staleness candidate query. Add the traffic join, gate on it like the
serving rule, and put the rule in `src/brickpulse/rules/catalog/`.

### Two billing facts that shape the SQL
1. **Apps bill under a generic serverless SKU** —
   `PREMIUM_ALL_PURPOSE_SERVERLESS_COMPUTE_<REGION>`, nothing in the name says "app".
   Apps cannot be selected by SKU name; filter on `billing_origin_product = 'APPS'`.
   (Same fact that made apps invisible in the spend breakdown above.)
2. **Apps are always-on while running, and mostly one size.** Typically about half
   of the apps bill every day of a 30-day window, and nearly all run `MEDIUM`.
   There is no scale-to-zero, so `days_billed` is a **lifecycle** signal (was it ever
   stopped?), never an idleness one — the UI labels a full-window count "always on"
   as a neutral fact.

### Table shape and read path
Same `(kind, workspace_id, payload)` shape as `finops_genie_usage`, with
`kind ∈ inventory | size | daily`. Both analysis-only sinks now share one pipeline
helper (`pipeline.py::_build_kinded_sink`) so they cannot drift on the two details
that are easy to get wrong: `workspace_id` is lifted **out** of the payload into a
real column (scoping must never parse JSON — so the read path has to select it back
and merge it), and workspace name/url are resolved from `workspaces_latest`.

Read path mirrors Genie: snapshot → three concurrent live queries under one 45s budget
→ honest `unavailable`. The inventory query joins the audit log and is much the
slowest, so it is the one most likely to miss the budget; `missing_sections` names it
so the UI explains an empty table instead of implying there are no apps.

Per-workspace scope works exactly as for Genie: `GET /api/apps/{workspace_id}`
(route `/workspaces/:workspaceId/apps`) via `Scope.narrowed_to`.

### The window is exactly N days
`date_sub(current_date(), N)` is inclusive at **both** ends, so the naive filter spans
N+1 dates and an always-on app reports `days_billed = 31` for a 30-day window. The
apps SQL also excludes today (itself partial, which would understate the last day), so
"billed 30 of the last 30 days" is literally true.

## Cost anomalies — `finops_cost_anomalies`
Analysis-only tab (`/cost-anomalies`, per-workspace `/workspaces/:workspaceId/cost-anomalies`
via `GET /api/anomalies/{workspace_id}` + `Scope.narrowed_to`). **An anomaly is not a
saving**: a spike can be a backfill or a launch, so `CostAnomaly` carries no
`monthly_savings_usd` / priority / NBA field and nothing
here feeds a savings total. The method (grain, baseline, robust z, weekday rhythm,
types) is documented in the `src/brickpulse/server/data/anomaly_sql.py` module docstring; in short:

- Daily **list-priced** cost at two grains: `sku` (workspace × `billing_origin_product` ×
  `sku_name`, covers every dollar) and `resource` (job before cluster, because job
  clusters get a new id per run). Totals come from the `sku` grain only — the two
  grains overlap.
- Baseline = the 28 days before the last 7, densified with $0 days; flagged when the
  robust z > 4 **and** the excess clears `max($200, 3 % of workspace median daily
  spend)`, against both the overall and the same-weekday median.
- Types: `new_spender` (no baseline), `step_change` (≥ 3 consecutive flagged days),
  `spike` (the rest). Today is excluded (partial).
- "Who" = `identity_metadata.run_as` / `owned_by` of the largest record on the peak day.

Same `(kind, workspace_id, payload)` table shape as Genie/Apps, written by
`pipeline.py::_build_anomalies` through `_build_kinded_sink`. Read path: snapshot →
live fallback → honest `unavailable`. **The live fallback needs 35 days of billing and
takes 1.5–3 min on a large account**, well over the 45 s budget, so in practice the
tab depends on the snapshot; before the first pipeline run it says so rather than
showing "no anomalies". The table name defaults to `finops_cost_anomalies` next to
`finops_table` (override: `FINOPS_ANOMALY_TABLE`).

## Savings tracker — "Saved so far" history (four Delta tables)
What changed on resources the app flagged, in the direction we recommended, valued
from our own estimate. It shows that
the changes happened; it can't prove the app caused them. This section is the
storage contract. The API and the tile that read it are under "Saved so far: API and
tile" below.

**Who writes it.** Not the SDP pipeline: its outputs are materialized views holding
only the current snapshot, and a full refresh would reset a streaming table. The
daily job "[FinOps] Daily refresh (<catalog>.<schema>)" has a second task, `track_changes`
(`src/brickpulse/pipelines/change_tracker.py`, `depends_on: run_pipeline`), that owns four **plain
Delta tables** next to `finops_recommendations`. It creates them if absent; the DDL
files in `src/brickpulse/pipelines/ddl/` are the documented contract and match the code schema
(`change_tracker.TABLE_COLUMNS`). Idempotent per snapshot
day: a re-run replaces that day's ledger rows and events and rebuilds the daily table.

| Table | Grain | Written |
|---|---|---|
| `finops_finding_ledger` | finding × snapshot day (main set only) | `DELETE` day + `INSERT`; never pruned |
| `finops_workspace_retirement` | a `listing` row per workspace ever listed + a `retirement` row per period gone | overwritten each run (small) |
| `finops_change_events` | one row per change of a finding's tracked state | the day's rows replaced; `counted` re-derived each run |
| `finops_savings_daily` | (day, workspace, spend scope) where something was booked | rebuilt in full each run |

Table names default to siblings of `finops_table`; override with
`FINOPS_FINDING_LEDGER_TABLE`, `FINOPS_CHANGE_EVENTS_TABLE`,
`FINOPS_WORKSPACE_RETIREMENT_TABLE`, `FINOPS_SAVINGS_DAILY_TABLE`. The deploy notebook
grants the app's service principal SELECT on them; they exist only after the tracker's
first run, so re-run its section 5 if they were reported as not created yet.

**What is tracked.** Every finding in the main set (no sidecar category, no Labs rule)
that was non-advisory in at least one snapshot. Its **baseline** is its first ledger
row: `evidence_core` keeps the tracked-setting keys, `phasing`, `savings_curve`, the
idle gap, and `_observed` (the config value the tracker read that day, used when the
evidence doesn't carry the "from" value). `first_seen` = that row's `snapshot_date`;
`tracking_since` = `MIN(snapshot_date)` of the ledger.

**Per-rule setting.** Each rule declares `tracked_setting = TrackedSetting(...)` in its
own file (`finops_rules/change_tracking.py`). A rule without one still gets deletion
credit. Current declarations:

| Rule | Setting (direction → proposal) | Valuation |
|---|---|---|
| `warehouse_autostop` | `auto_stop_minutes` ↓ → `target_autostop_minutes` | `savings_curve` (interval-priced findings), else linear |
| `warehouse_autostop_serverless` | `auto_stop_minutes` ↓ → `proposed_autostop_minutes` | `savings_curve` (from the gap histogram), else linear |
| `warehouse_rightsize` | `warehouse_size` ↓ → `proposed_size` (phasing data target when present) | phasing curve, else ladder |
| `warehouse_min_clusters` | `min_clusters` ↓ → 1 (phasing data target when present) | phasing curve, else linear |
| `enable_serverless` | `warehouse_type` = `SERVERLESS` | binary |
| `cluster_autotermination` | `auto_termination_minutes` ↓ → proposed | linear (off = longest idle gap, else 1,440) |
| `idle_zombie_clusters` | any shorter `auto_termination_minutes` | full credit |
| `rightsize_underutilized` | max workers (`COALESCE(max_autoscale_workers, worker_count)`) ↓ → `target_workers` | phasing curve, else linear |
| `unused_scheduled_jobs`, `unused_data_jobs`, `job_no_new_input` | job `paused` = true | binary (the first two are advisory today, so nothing is tracked for them yet) |
| `idle_serving_endpoints` | `min_concurrency` → 0 (scale to zero) | binary |
| every other non-advisory rule | deletion only | 1.0 |

**Valuation.** `valued_monthly_usd = fraction × estimate_before_usd`. The estimate is
the finding's `monthly_savings_usd` (point, list price) in the last non-advisory
snapshot on or before the change day (`reference_snapshot_date`). The fraction is in
[0, 1]: deletion, pause, or reaching/passing the proposal = 1. A partial move uses the
finding's savings curve (`evidence.savings_curve`, or the three points of
`evidence.phasing`, interpolated piecewise-linearly: `curve(after) / curve(proposal)`),
else the size ladder `(1 − 2^−k_done) / (1 − 2^−k_proposed)`, else linear
`(before − after) / (before − proposal)`. Later changes re-value the SAME finding from
its original baseline against the same reference snapshot (240 → 60 → 30 is one
finding whose fraction rises twice). A move away (or back to the baseline) is an
`away` event worth 0.

**What doesn't count** (`counted = false`, `not_counted_reason`):
- `before_first_seen`: the change predates the finding's first snapshot.
- `advisory`: no non-advisory snapshot before the change.
- `workspace_retired`: the workspace was retired on the change day.
- `workspace_status_unknown`: a **deletion** in a workspace never listed in
  `workspaces_latest`. Setting changes there still count.

**Deletion and absence.** A deletion needs `delete_time` on the resource's latest SCD
row. A resource missing from a config query is unknown and yields no event. A config
query that fails skips that resource type for the day.

**Retirement** (owner decisions, nothing spend-based): a workspace is retired when
`workspaces_latest.status` is `BANNED` (reason `banned`), or when it was listed on an
earlier run and is no longer listed (reason `no_longer_listed`).

`workspaces_latest` has **no deleted or cancelled status** live. Verified 2026-10-06:
`status` only takes `RUNNING` and `BANNED` (documented values: NOT_PROVISIONED |
PROVISIONING | RUNNING | FAILED | BANNED). So a deleted workspace is detected only by
"no longer listed": it drops out of the table.

`retired_on` = the day after the workspace was last seen active. It is revived (`revived_on`) when it is listed active again, and changes after
that count again. A failed or empty listing changes nothing. Changes booked earlier
stop accruing on `retired_on`.

**Daily booking.** For each day from `tracking_since` to yesterday, every counted change
in effect that day (from its own `changed_at` day, back-dated if detected late, until
the finding's next event) is grouped by spend scope and deduped with the headline's
`book_scope` (imported from `finops.services.aggregation`, not copied).
`run_rate_monthly_usd` = that scope's booked value. `saved_usd` = run rate / 30 (the
app's 30-day month, `pricing.DAYS_PER_MONTH`). Hidden rules
(`FINOPS_HIDDEN_RULES`) are excluded. Saved in a window = `SUM(saved_usd) WHERE day
BETWEEN …`.

**Config sources** (one query per resource type, the latest SCD row per id, deleted
rows included): `compute.warehouses`, `compute.clusters`, `lakeflow.jobs` (`paused`),
`serving.served_entities` (`min_concurrency`, `endpoint_delete_time`),
`compute.instance_pools`, `lakeflow.pipelines`, `alert.alerts`, all verified live
2026-10-06 (`change_tracker.CONFIG_QUERIES`). Apps and dashboards have no config
table, so their findings are not tracked.

### "Saved so far": API and tile
`GET /api/savings-realized` (with the global `?workspace_id=` view filter, via
`get_view_scope`) and `GET /api/savings-realized/{workspace_id}` (narrowed with
`Scope.narrowed_to`: a workspace you don't own gives an empty result and runs no query;
a non-numeric id is a 400, as on the spend and Genie drill-ins). Optional `from` / `to`
(ISO dates, inclusive) select any window. The default is `tracking_since` through
yesterday, i.e. cumulative since deployment. Source: `data/realized_source.py`. The math
is `services/aggregation.realized_totals`.

**No live fallback.** The figure is history, so it is `unavailable` (and the tile is
hidden) in live mode, and when `finops_savings_daily` can't be read (before the first tracker run, or no grant).
When the table exists but nothing is booked yet, the tile shows `$0 · since deployment on
<tracking_since>`, which makes it clear the tracking is running.

| Field | Source |
|---|---|
| `saved_usd` | `SUM(saved_usd)` of `finops_savings_daily` rows in the window. The tracker already deduped each pool per day (`book_scope`), so the app never re-adds |
| `run_rate_usd` | Sum of `run_rate_monthly_usd` on `min(window_end, data_through)`. `data_through` is the table's last day, taken BEFORE the scope filter (`MAX(day) OVER ()`), so a workspace whose bookings ended reads 0 |
| `tracking_since` | `MIN(snapshot_date)` of `finops_finding_ledger`. If the ledger is unreadable, the first booked day |
| `by_month` | `saved_usd` per calendar month, every month of the window (zero months included) |
| `by_kind` | Findings booked in the window, each by its latest event on or before `window_end`: `full` (reached/beyond), `partial` (toward), `deleted` (deleted/paused), `away` (moved back since: booked earlier, now 0, not in `change_count`). An unknown `change_kind` keeps its own key. `monthly_usd` is each kind's share of `run_rate_usd`: every deduped pool row on the run-rate day is split across the kinds in effect in it, in proportion to their own valuations, so the kinds sum to the run-rate. A finding no longer in effect (moved back, workspace retired) counts but adds $0 |
| `change_count` | The booked findings, minus `away` |
| `not_counted` | Uncounted credit events (latest per finding, not superseded by a later counted one), per `not_counted_reason`: `{count, workspace_count, monthly_usd}` |
| `partial_example` | The biggest `partial` finding's `setting` / `baseline_value` / `after_value` / `proposed_value` / `fraction` (the tooltip's "e.g. auto-stop 240 → 30 min") |

**Exclusions.** Events of hidden and experimental rules are dropped. A daily row is
dropped only when every rule on it is excluded. A mixed row is kept whole, because the
pool's deduped figure can't be split back into rules. The tracker already drops hidden
rules, so this only matters when the app's settings differ from the pipeline's.

**Back-compat.** Every model field has a default, every column is read with
`.get` from `SELECT *`, arrays may arrive as lists or JSON strings, and an unreadable
`finops_change_events` or ledger only empties the breakdown or the date, never the
dollars. The UI labels an unknown kind or reason with its raw key.

**Tile.** It comes FIRST on the Overview and on every Workspace page, with the
per-kind rows, "Not counted: …" and "By month: …" in its tooltip. The copy says "Saved
so far", never "saved by the app".
