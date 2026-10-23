# Adding a cost-saving rule

A rule is a self-contained plugin. Adding one never requires editing a shared
list: the registry discovers every rule in `src/brickpulse/rules/catalog/`.

## The 3 pieces
1. `src/brickpulse/rules/catalog/<rule_id>.py` — the rule class
2. `src/brickpulse/rules/sql/<rule_id>.sql.j2` — candidate-finding SQL
3. `src/brickpulse/rules/config/thresholds.yaml` — a `<rule_id>:` config block

Fastest path: **copy `autotermination.py` + `autotermination.sql.j2`** and adapt.

## Step 1 — write the candidate SQL (`sql/<rule_id>.sql.j2`)
Return the resources that *might* be wasteful, plus the raw metrics `score()`
needs. Rules:
- **Do not filter by `workspace_id`** — scoping is injected later by the data
  layer. Your final `SELECT` **must** expose a `workspace_id` column.
- Use the current SCD2 row when reading config tables:
  `QUALIFY ROW_NUMBER() OVER (PARTITION BY workspace_id, <resource_id> ORDER BY change_time DESC) = 1`.
- Attribute **real dollars** through the SHARED pricing macro — never a
  hand-written join. Start the template with
  `{% import '_macros.sql.j2' as m with context %}` and use:
  ```sql
  SUM(u.usage_quantity * {{ m.price_rate('lp') }}) AS cost_30d_usd,
  SUM({{ m.unpriced_dbus() }})                     AS unpriced_dbus
  FROM {{ catalog }}.billing.usage u
  {{ m.price_join('lp') }}
  ```
  The macro keys the join on all four `list_prices` columns and reads
  `pricing.effective_list.default`. A copy-pasted join gets both wrong: keying on
  `sku_name` alone fans out (and inflates dollars) in any multi-cloud or
  multi-currency account, and `pricing.default` ignores promotional pricing.
  See the header of `sql/_macros.sql.j2`.
- Reference thresholds as Jinja vars: `{{ max_acceptable_minutes }}`, plus
  `{{ catalog }}` and `{{ lookback_days }}` which are always provided.
- **Emit an `observed_days` column** — distinct calendar days the resource was
  seen *active*, on whichever source is your rule's primary evidence
  (`COUNT(DISTINCT CAST(start_time AS DATE))` off `node_timeline` /
  `query.history`, `COUNT(DISTINCT usage_date)` off `billing.usage`, distinct run
  days off the timelines). This feeds the minimum-observation guard in Step 2.
- SQL comments (`-- …`) are kept: the rendered SQL is stored in
  `finops_rule_catalog.detection_sql`, so write them for the people who will read
  the rule's logic there.

## Step 2 — write the rule class (`catalog/<rule_id>.py`)
```python
from brickpulse.rules.contract import EffortBand, Rule, RuleContext, RuleFinding, RulePriority
from brickpulse.rules.coverage import coverage_evidence, coverage_note, underpriced
from brickpulse.rules.history import history_evidence, history_note, insufficient_history
from brickpulse.rules.pricing import to_monthly
from brickpulse.rules.registry import register

@register
class MyRule(Rule):
    id = "my_rule"                 # must match the SQL and config file names
    version = "1.0.0"
    title = "Human-readable action"
    resource_type = "cluster"      # cluster|warehouse|job|serving_endpoint|pipeline
    category = "compute"           # compute|sql|jobs|serving|pipelines|governance
    sql_file = "my_rule.sql.j2"    # optional; defaults to "<id>.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        if insufficient_history(row, ctx):
            return None                    # too new to judge -> suppress FIRST
        if underpriced(row):
            return None                    # too little of its spend could be priced
        monthly = to_monthly(row.get("cost_30d_usd") or 0, ctx.lookback_days)
        if monthly <= 0:
            return None                    # nothing to save -> suppress
        if not _actually_applies(row, ctx.thresholds):
            return None                    # investigation-guided guard
        savings = monthly * _fraction(row)
        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type=self.resource_type,
            resource_id=str(row["resource_id"]),
            resource_name=row.get("resource_name") or str(row["resource_id"]),
            monthly_waste_usd=round(monthly, 2),
            monthly_savings_usd=round(savings, 2),
            effort=EffortBand.LOW,         # trivial | low | medium | high
            priority=RulePriority.HIGH,    # HIGH | MEDIUM | LOW
            why="Why this wastes money, with the numbers.",
            how="The concrete action to take.",
            evidence={"metric": row.get("metric"), **history_evidence(row, ctx)},
        )
```
`effort` drives the Next-Best-Action score (`monthly_savings_usd / effort_score`),
so be honest: a config toggle is `TRIVIAL`, a migration is `HIGH`.

**`insufficient_history` goes first, before any pricing.** Every savings number is
a sample extrapolated to a month, and a resource observed for a day or two cannot
support one — see the "Minimum observation window" section of
[DATA_MODEL.md](DATA_MODEL.md). Append `history_note(row, ctx)` to your
`data_basis` so the sample size is stated in the UI, not just enforced. If your
rule genuinely keys on the *absence* of activity (like `unused_scheduled_jobs`),
skip the guard.

The source of `score()` is stored in `finops_rule_catalog.scoring_logic`, so keep
its comments meaningful too.

## Step 3 — add the config block (`config/thresholds.yaml`)
```yaml
my_rule:
  enabled: true
  max_acceptable_minutes: 60   # whatever your SQL/score reference
```
These are runtime-tunable — changing a threshold changes what counts as
wasteful without touching code.

## Step 4 — check the SQL against a real warehouse
Render the SQL and run it **once** before trusting the rule. A wrong column name
only fails on a live query:
```python
# run from the folder root, with jinja2 and pyyaml installed
import sys; sys.path[:0] = ["src"]
from brickpulse.rules import load_thresholds
from brickpulse.rules.contract import RuleContext
from brickpulse.rules.registry import discover, get_rule
discover()
rule = get_rule("my_rule")
print(rule.render_sql(RuleContext(thresholds=load_thresholds()["my_rule"])))
```
Then run `SELECT COUNT(*) FROM (<rendered sql>\n)` in the SQL editor. Keep the
newline before the closing paren — several templates end in a `--` comment which
would otherwise swallow it.

## Step 5 — ship it
- **App:** it imports the rules from `src/brickpulse/rules/`, so it picks up the new
  rule on its next deployment.
- **Pipeline:** it installs the rules from the wheel the bundle builds
  (`scripts/build_wheel.py`, run by `databricks bundle deploy`).
- Deploy with `scripts/deploy.py --profile PROFILE --target prod --check-sources --refresh`:
  it re-checks the system tables the enabled rules read (derived from their rendered SQL),
  updates the pipeline, runs it so the new rule's rows exist, grants the app read on any
  new table and redeploys the app.
- Add the rule's localized synopsis to the three API catalogs in
  `src/brickpulse/server/i18n/`; keep dynamic original instructions intact.

Then confirm it shows up: the **Rule Catalog** page (and `GET /api/rules`) lists
every registered rule with its current thresholds and enabled state.

## Rules that reduce capacity
Any rule that lowers **capacity users can feel** (a warehouse's size or min clusters,
a cluster's workers, a pool's warm instances, serving min units) must not jump
straight to what the data supports (30 → 1). Helpers: `src/brickpulse/rules/phasing.py`.
1. **Say what the data supports** — the `data_target`.
2. **Recommend one bounded step now.** Each step is at most `max_step_ratio` times
   smaller (default **3×**: `count_plan(30, 1)` = `[10, 4, 2, 1]`; t-shirt sizes:
   `ladder_plan(current, target, savings.WAREHOUSE_SIZE_LADDER)`), then observe for
   `observe_days` (default **7**) and repeat. Both defaults live in the top-level
   `phasing:` block of `thresholds.yaml` and reach your rule as `RuleContext.phasing`;
   read them with `phasing.max_step_ratio(ctx)` / `phasing.observe_days(ctx)`. A rule
   may override the ratio in its own block (`phasing_max_step_ratio`, e.g.
   `rightsize_underutilized` halves).
3. **Lead with the full saving:** `monthly_savings_usd` = `monthly_savings_high_usd`
   = the saving at the data target; `monthly_savings_low_usd` = the first step's.
   Explain both in `evidence.savings_range_basis` when they differ. NBA, the headline
   and the totals then see the whole opportunity, and the UI says what the first step
   alone is worth.
4. **Emit `evidence.phasing`** via `phasing_evidence(setting, current, plan, ...)`
   (contract in [DATA_MODEL.md](DATA_MODEL.md), "Phased downsizing"). The UI renders
   the step plan from it, with no per-rule UI code. Any `proposed_*` evidence key
   means the step to take NOW.
5. **Say in `how` what to watch between steps** (queued share, p95 latency, spill,
   job duration): `phase_sentence(plan=..., observe_days=..., watch=...)`, and
   `phase_target_clause(...)` for the `why`.

If your SQL prices the step (as `warehouse_min_clusters` does), compute it in the
same pass and have `score()` check its step against `count_plan(...)[0]`; on a
mismatch trust the SQL and add a `data_warning`. **Timeouts are excluded**: auto-stop
and auto-termination stay aggressive with no step rule (the admin judges the
cold-start trade-off; `warehouse_autostop_serverless` already bounds restarts/day).

## A NEW `resource_type`
The prebuilt UI knows `cluster`, `warehouse`, `job`, `serving_endpoint` and
`pipeline`. Any other value still shows, but with a generic icon and **no deep link**
to the resource in its workspace.

## It just works in both modes
Because a rule is only SQL + `score()`, live mode and the pipeline both run it the
same way. You don't write anything mode-specific.
