"""Rule: retire a job that produces data nobody reads (Gap G2).

A scheduled job that repeatedly writes a table which never appears as a read in
``system.access.table_lineage`` is spending compute to produce dead data. This
was a documented blind spot (Gap G2): the app had no lineage/consumption rule.

Confidence is LOW *by construction*: lineage read-capture is incomplete (reads
through paths, external tools, or un-instrumented surfaces may not register), so
"zero reads observed" is a strong proxy, not proof. The rule therefore (a) guards
hard in SQL — every output table unread, a focused producer (few tables),
recurring writes — and (b) never books: it shows the producing cost as *max
addressable* pending verification, rather than a fraction-of-cost guess.

Savings model:

* **No haircut.** The old rule credited a 50-70% "recoverable fraction" of the
  producing cost — an invented number. It now shows the FULL producing cost as the
  max-addressable upper bound and books **$0** (advisory): a lineage-inferred "dead
  data" claim is only realized once a human confirms the output really is unused.
* **Lineage-availability sentinel.** "Zero reads on this table" is meaningful only
  if lineage is capturing reads in the table's schema at all. When the output
  table's schema shows NO captured reads of any table, that is *no coverage*, not
  *dead data*, and the finding is suppressed — distinguishing the two cases the old
  rule conflated.
"""

from __future__ import annotations

from brickpulse.rules.change_tracking import TrackedSetting
from brickpulse.rules.contract import (
    EffortBand,
    Rule,
    RuleContext,
    RuleFinding,
    RulePriority,
)
from brickpulse.rules.evidence import CONFIDENCE_LOW, evidence_block, viz_write_no_read
from brickpulse.rules.history import history_evidence, history_note, insufficient_history
from brickpulse.rules.pricing import to_monthly
from brickpulse.rules.registry import register

_BASIS = (
    "system.access.table_lineage (writes with zero corresponding reads, and the "
    "output schema confirmed to have captured reads — the availability sentinel) + "
    "billing.usage x list_prices, {n}-day lookback. Lineage read-capture is "
    "incomplete, so 'never read' is a proxy: the full producing cost is shown as "
    "MAX ADDRESSABLE and books nothing (advisory) until a human confirms the output "
    "is genuinely unused."
)


@register
class UnusedDataJobsRule(Rule):
    id = "unused_data_jobs"
    # Savings model: lineage-based, needs explicit human verification
    # before booking. Advisory — shows the FULL producing cost as max-addressable and
    # books $0 (no recoverable-fraction haircut).
    default_savings_status = "advisory"
    version = "2.0.0"
    title = "Retire a job that produces unread data"
    resource_type = "job"
    # "Saved so far" (finops_rules.change_tracking): the job was paused (deletion is
    # generic). The "from" value is the tracker's own observation of ``paused``.
    # NB: this rule is advisory today, so nothing it flags is tracked until it books.
    tracked_setting = TrackedSetting(
        resource="job", column="paused", proposed_value=True, direction="equals",
        valuation="binary", reached_kind="paused", label="Schedule paused",
    )
    category = "jobs"
    sql_file = "unused_data_jobs.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        # Too little history to judge. "Nobody has read it" only means something
        # once the data has existed long enough for a reader to appear — a table
        # first written yesterday is unread because it is NEW, not because it is
        # dead. See finops_rules.history.
        if insufficient_history(row, ctx):
            return None

        monthly_cost = to_monthly(row.get("cost_30d_usd") or 0.0, ctx.lookback_days)
        if monthly_cost <= 0:
            return None  # no billed producing cost => nothing to reclaim

        # lineage-availability sentinel: "zero reads on this table" is only a
        # dead-data signal if lineage is capturing reads in the table's schema at all.
        # min_schema_reads == 0 means the output schema shows NO captured reads of any
        # table — that is "no coverage", not "dead", so we cannot make the claim.
        min_schema_reads = int(row.get("min_schema_reads") or 0)
        if min_schema_reads <= 0:
            return None

        # no recoverable-fraction haircut. A lineage-inferred "unused output"
        # is only realized once a human confirms it, so we show the FULL producing cost
        # as the MAX ADDRESSABLE amount and book $0 (advisory). low == 0, high == point.
        addressable = monthly_cost
        low, high, status = 0.0, addressable, "advisory"
        savings = round(addressable, 2)

        name = row.get("job_name") or str(row["job_id"])
        n_tables = int(row.get("n_tables") or 0)
        total_writes = int(row.get("total_writes") or 0)
        sample = row.get("sample_table")
        table_desc = (
            f"table '{sample}'" if n_tables == 1 else f"{n_tables} tables (e.g. '{sample}')"
        )
        why = (
            f"Job '{name}' wrote {table_desc} {total_writes} times over the last "
            f"{ctx.lookback_days} days, but lineage shows those outputs were never "
            f"read by any query, dashboard, pipeline, or model — and reads ARE being "
            f"captured in that schema, so the zero-read signal is real. It is spending "
            f"~${monthly_cost:,.0f}/mo to produce data nobody appears to consume — the "
            f"full addressable amount if it's confirmed dead. Because lineage can miss "
            f"reads via paths or external tools, this is shown for verification, not "
            f"booked as savings."
        )
        how = (
            "Confirm the output really is unused (check downstream dashboards, "
            "external readers, and ad-hoc queries not captured by lineage). If "
            "confirmed, pause or delete the job (Workflows > the job > pause/delete) — "
            "the ~${:,.0f}/mo becomes real savings only once that's verified.".format(
                monthly_cost
            )
        )

        days = _normalize_days(row.get("days"))
        viz = viz_write_no_read(
            days=days,
            lookback_days=ctx.lookback_days,
            total_writes=total_writes,
            total_reads=0,
            last_write=str(row.get("last_write")) if row.get("last_write") else None,
        )

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="job",
            resource_id=str(row["job_id"]),
            resource_name=name,
            monthly_waste_usd=round(monthly_cost, 2),
            monthly_savings_usd=savings,
            monthly_savings_low_usd=round(low, 2),
            monthly_savings_high_usd=round(high, 2),
            # Advisory: a lineage-inferred unused output books $0 until a human
            # verifies it; the producing cost is the max-addressable upper bound.
            savings_status=status,
            effort=EffortBand.LOW,
            priority=RulePriority.LOW,
            why=why,
            how=how,
            evidence=evidence_block(
                confidence=CONFIDENCE_LOW,
                data_basis=_BASIS.format(n=ctx.lookback_days)
                + history_note(row, ctx),
                viz=viz,
                **history_evidence(row, ctx),
                output_tables=n_tables,
                sample_table=sample,
                # sentinel: captured reads in the output table's schema. > 0
                # confirms lineage is instrumented there, so 'zero reads' is a real
                # signal rather than 'no coverage'.
                schema_read_events=int(row.get("min_schema_reads") or 0),
                total_writes=total_writes,
                total_reads=0,
                last_write=row.get("last_write"),
                cost_30d_usd=row.get("cost_30d_usd"),
            ),
        )


def _normalize_days(days) -> list[dict]:
    """Coerce the SQL ``days`` array (structs with fields d/w/r) into plain
    dicts the viz builder expects; tolerant of None / dict / struct / tuple."""
    out: list[dict] = []
    for item in days or []:
        if isinstance(item, dict):
            d, w, r = item.get("d"), item.get("w"), item.get("r")
        else:
            d = getattr(item, "d", None)
            w = getattr(item, "w", None)
            r = getattr(item, "r", None)
            if d is None and isinstance(item, (list, tuple)) and len(item) == 3:
                d, w, r = item
        if d is None:
            continue
        out.append({"d": str(d), "w": int(w or 0), "r": int(r or 0)})
    return out
