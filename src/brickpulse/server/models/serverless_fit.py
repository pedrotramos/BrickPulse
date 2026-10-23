"""Per-resource "should this go serverless?" verdict, shown on the detail page.

Today a rule that decides serverless is *not* the right move for a resource just
returns ``None`` — the reasoning vanishes and the admin is left wondering whether
serverless was even considered. This model makes that decision explicit for the
resource types we can honestly judge (SQL warehouses and jobs): it says whether
serverless is recommended, already in use, not a fit, or genuinely unknowable,
and carries a projected saving *only* when a real finding measured one.

It is a pure read-time rollup over the findings a resource already has (like
:class:`RecommendationGroup` itself) — no new rule, no new query. The signal is
which rules fired: ``enable_serverless`` runs on every eligible classic
warehouse, so its ABSENCE on a classic warehouse is itself the "not a fit"
signal; ``serverless_cost_optimized`` / ``warehouse_autostop_serverless`` fire
only on resources that are already serverless.

The one hard rule mirrors the engine's core invariant: never show a dollar
projection we didn't actually measure. A "not a fit" or "can't estimate" verdict
carries no number, because inventing one for a workload we ruled out would be the
same overstatement the savings engine exists to avoid.
"""

from __future__ import annotations

from pydantic import BaseModel


class ServerlessFit(BaseModel):
    """Whether serverless is the right move for one resource, and why."""

    # recommended     — a measured serverless finding applies (projection is real)
    # already_serverless — the resource already runs serverless
    # not_a_fit       — serverless was applicable but wouldn't help (no number)
    # cannot_estimate — no signal to judge from billing data alone (no number)
    verdict: str
    headline: str  # one-line verdict for the card header
    reason: str  # the measured/honest justification, shown verbatim
    # Set ONLY for `recommended`, and only from a finding that actually computed
    # it. None everywhere else — we never fabricate a projection for a workload we
    # ruled out (same discipline as the savings engine).
    projected_monthly_savings_usd: float | None = None
    # Deep-link to the resource's serverless option (the finding that backs a
    # `recommended` verdict), so the card can point at the actionable rec.
    group_id: str | None = None
