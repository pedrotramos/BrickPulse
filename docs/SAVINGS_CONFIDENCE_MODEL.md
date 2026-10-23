# Savings confidence & range — how to read the numbers

This is the model behind every dollar figure the app shows: the **confidence
tier** (Booked / High / Medium / Low) and the **savings range** (`$low – $high/mo`).
It exists because the two are easy to conflate, and anyone who conflates them
will misread the headline. Read this before changing `services/aggregation.py`
or `src/brickpulse/rules/catalog/*`.

## The one thing to internalise: these are TWO different axes

| | Question it answers | Where it's set | Shape |
|---|---|---|---|
| **Confidence tier** | "How much do I trust the *method* behind this point estimate?" | A label chosen **per rule** (sometimes per finding), from how the number was derived. | Qualitative: Booked / High / Medium / Low. **Not** a numeric error bar. |
| **Savings range** | "What's the plausible *dollar* bracket around the point?" | `monthly_savings_low_usd` / `monthly_savings_high_usd` on each finding. | Numeric `[low, high]` around the point estimate. |

They are **not** derived from each other, and a narrow range does **not** imply
high confidence. A rule can be *Medium* confidence (the method rests on an
un-measurable assumption) yet report a *tight* dollar range (the modelled figure
itself has little dollar spread) — and vice-versa. The tier is the honest
"how much do you trust this" signal; the range is the honest "how big could the
number be" signal. Both are shown; neither substitutes for the other.

### Why the range often looks degenerate

`Recommendation.__post_init__` (`src/brickpulse/server/models/recommendation.py`) defaults:

```python
if self.monthly_savings_low_usd is None:  self.monthly_savings_low_usd = self.monthly_savings_usd
if self.monthly_savings_high_usd is None: self.monthly_savings_high_usd = self.monthly_savings_usd
```

So **any rule that doesn't explicitly widen its band reports `low == point == high`** —
a zero-width range. A headline can therefore show a very narrow range while most
of it is `Medium confidence`: rules that set no band collapse to a point. The
near-zero spread is an artefact of the default, **not** a statement of
certainty. Rules that model a genuine band (see below) are what make the range
meaningful.

## What each tier means, and how much uncertainty it carries

### Booked (`savings_status == "bookable"`)
The dollar is realised **by the action itself**, on a **directly-measured** cost.
Examples: `idle_serving_endpoints` (a zero-traffic endpoint's own billed cost),
`cluster_autotermination` (idle hours × the measured rate), `warehouse_autostop`.
- **Uncertainty on the mechanism ≈ 0.** The only risk is that future usage
  differs from the measured window.
- Band: `low == point` by design. Only the conservative `low` of a bookable
  finding is banked into the headline lower bound.

### High confidence
The **signal and the dollars are directly measured**, but one modelling caveat
keeps it out of "Booked." Usually the caveat is **conditional realisation** —
the number is right, but whether you *capture* it depends on an action or a
judgement:
- `interactive_to_job` on a *shared* cluster: the all-purpose-vs-jobs price gap is
  measured exactly, but moving the jobs off may not shrink a cluster that's also
  used interactively → `low = $0, high = point` (all-or-nothing, not ±%).
- `warehouse_scale_out`, `sql_repeated_failures`, `continuous_pipeline`: the metric
  is counted directly; the $ is addressable, not booked.
- **Uncertainty:** the figure is dependable; the risk lives in whether the action
  is taken, not in the arithmetic.

### Medium confidence
The point estimate depends on **an input Databricks billing does not expose**, or
on **an assumption about workload behaviour**:
- `enable_serverless` / `warehouse_autostop_serverless`: parts of the figure rest
  on cost that classic/pro billing doesn't split out per resource (a cloud-VM cost;
  a churn/restart trade-off). `enable_serverless` encodes this in its band
  (`low` = the DBU-only floor billing can prove, `high` = incl. the estimated VM).
- `warehouse_rightsize` (with measured slots): slot occupancy *is* measured and
  gates the recommendation, but a larger size also **speeds each individual query**
  — an effect slot occupancy can't see — and a 30-day window can miss a periodic
  heavy batch. That un-measurable latency effect is exactly why it's never booked.
- **Uncertainty: roughly ±30–50%**, driven by the estimated input or the
  behavioural assumption. A rule should widen its band to reflect this rather than
  leave it collapsed.

### Low confidence
**No positive measurement of the key driver** — inferred from a weak or absent
proxy:
- `warehouse_rightsize` when no Spark task data is available (size inferred from
  query behaviour alone), `enable_photon_dbr` (speedup from a generic factor),
  `unused_data_jobs`.
- **Uncertainty: unbounded / directional** — could be off by a large factor. Treat
  it as "worth investigating," not "worth banking." A Low-confidence rule should
  report a wide band (typically `low = $0`), which is what makes the range on the
  UI visibly wide.

## How the tiers roll up into the headline (aggregation.py)

- `SAVINGS_TIER_ORDER = ("booked", "high", "medium", "low")` — `booked` is its own
  tier (a bookable finding is booked regardless of its data-confidence label);
  everything else falls into its `evidence.confidence` tier.
- `book_scope_breakdown` attributes each **spend scope's composed amount** to a
  tier, so the tiers **sum exactly** to `total_monthly_savings_addressable_usd`
  (no dollar is counted twice when two options target the same scope).
- `_savings_range` sums each scope's `low` / `high` (non-advisory only) to give the
  headline band. The booked headline (`_booked_savings_total`) still counts only
  bookable `low`, so widening a band on an *estimated* rule never inflates the
  booked floor.

## The rule author's contract for the band

When a rule widens its band, make `low` and `high` **mean something a reader can
act on**, and say what they mean in `evidence.savings_range_basis` (one sentence,
surfaced verbatim on the resource's Data Quality panel and the option's range
tooltip). Follow the existing convention: **the point estimate is usually the
optimistic/high end, and `low` is the conservative/measurable floor** (e.g.
`enable_serverless`, `interactive_to_job`). A rule whose deeper opportunity is the
upside (e.g. `warehouse_rightsize`'s multi-step floor) may instead put the point at
the low end and the reachable ceiling at `high`; the `savings_range_basis` sentence
disambiguates which it is. Leave the band collapsed only when the figure genuinely
has little dollar uncertainty (a measured, low-churn, single-step change) — and
even then the confidence tier still carries any method caveat.

## Checklist when changing a rule's figures

- A change that widens a band on an **estimated/advisory** rule must not change the
  **booked** headline — verify `_booked_savings_total` still counts only bookable
  `low`.
- A rule that sets a band should set `evidence.savings_range_basis` too, or the UI
  shows a range with no explanation of what its ends mean.
- Tier attribution must still sum to the addressable total
  (`aggregation.book_scope_breakdown`). Check this if you add a tier or change how
  options compose.
- Don't invent a synthetic percentage confidence. The tiers are qualitative on
  purpose; a fake "87% confident" reads as precision the data doesn't support.
