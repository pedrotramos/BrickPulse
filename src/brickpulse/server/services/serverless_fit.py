"""Decide, per resource, whether serverless is the right move — and say why.

The savings engine silently drops a resource when serverless wouldn't help it:
``enable_serverless`` returns ``None`` for a steadily-busy warehouse, and no
finding is produced. That silence is the problem this fills. Given the findings a
resource already has, :func:`evaluate` returns a :class:`ServerlessFit` verdict
the detail page shows next to the recommendations.

Why this can be done from findings alone (no new query):

* ``enable_serverless`` runs on *every* eligible classic warehouse with material
  spend. So on a classic warehouse, the PRESENCE of that finding => recommended;
  its ABSENCE (while other warehouse rules fired) => serverless was considered and
  didn't clear the idle-warm-pool bar, i.e. "not a fit".
* ``warehouse_autostop_serverless`` / ``serverless_cost_optimized`` fire only on
  resources that are ALREADY serverless — so their presence pins the verdict to
  "already serverless" (with a cost-optimized projection for jobs).
* ``warehouse_type`` is carried in warehouse evidence, so a warehouse that only
  matched size/scale-out rules can still be classified classic vs serverless.

The invariant, mirroring the savings engine: a projected dollar figure appears
ONLY when a real finding measured one. "Not a fit" and "can't estimate" carry no
number — inventing one for a workload we ruled out is exactly the overstatement
the engine exists to prevent.
"""

from __future__ import annotations

from ..models import Recommendation
from ..models.group import group_id
from ..models.serverless_fit import ServerlessFit

#: Rules that only ever fire on already-serverless resources.
_SERVERLESS_ONLY = {"warehouse_autostop_serverless", "serverless_cost_optimized"}

#: The rule that recommends moving a classic warehouse to serverless.
_TO_SERVERLESS = "enable_serverless"


def _is_serverless_warehouse(options: list[Recommendation]) -> bool | None:
    """Classic vs serverless from evidence, or None if no rule carried the type."""
    for r in options:
        if r.rule_id in _SERVERLESS_ONLY:
            return True
        wt = (r.evidence or {}).get("warehouse_type")
        if isinstance(wt, str) and wt:
            return wt.upper() == "SERVERLESS"
    return None


def _find(options: list[Recommendation], rule_id: str) -> Recommendation | None:
    for r in options:
        if r.rule_id == rule_id:
            return r
    return None


def evaluate(
    resource_type: str,
    options: list[Recommendation],
) -> ServerlessFit | None:
    """Serverless-fit verdict for one resource, or None where we say nothing.

    ``options`` are the resource's findings (a group's options). Returns None for
    resource types serverless doesn't apply to (clusters map to serverless via a
    different lever, covered by their own rules; endpoints/pipelines aren't in
    scope here), so the card simply doesn't render."""
    if not options:
        return None
    if resource_type == "warehouse":
        return _warehouse_fit(options)
    if resource_type == "job":
        return _job_fit(options)
    if resource_type == "cluster":
        return _cluster_fit(options)
    return None


def _warehouse_fit(options: list[Recommendation]) -> ServerlessFit:
    to_serverless = _find(options, _TO_SERVERLESS)
    if to_serverless is not None:
        idle = (to_serverless.evidence or {}).get("idle_hours_pct")
        idle_txt = f" It was warm but idle {idle:.0f}% of its billed hours" if isinstance(idle, (int, float)) else ""
        return ServerlessFit(
            verdict="recommended",
            headline="Serverless is recommended",
            reason=(
                "This classic warehouse holds a cluster warm between query bursts."
                f"{idle_txt}, which serverless bills to zero. The projected saving "
                "below is measured from that idle warm-pool time — see the "
                "'Move to Serverless' option for the evidence."
            ),
            projected_monthly_savings_usd=round(to_serverless.monthly_savings_usd, 2),
            group_id=group_id(
                to_serverless.workspace_id, "warehouse", to_serverless.resource_id
            ),
        )

    is_serverless = _is_serverless_warehouse(options)
    if is_serverless is True:
        auto = _find(options, "warehouse_autostop_serverless")
        extra = (
            " Its auto-stop can still be tightened — see the option below."
            if auto is not None
            else ""
        )
        return ServerlessFit(
            verdict="already_serverless",
            headline="Already serverless",
            reason=(
                "This warehouse already runs on serverless SQL, so it scales to "
                "zero between bursts with no cluster to size or idle timeout to "
                f"tune.{extra}"
            ),
        )

    if is_serverless is False:
        # enable_serverless runs on every eligible classic warehouse, so its
        # absence here means it was evaluated and the idle warm-pool wasn't worth
        # the higher serverless rate — a real "not a fit", not an oversight.
        return ServerlessFit(
            verdict="not_a_fit",
            headline="Serverless wouldn't save here",
            reason=(
                "We checked this classic warehouse for serverless: it's busy "
                "steadily enough that there's little idle warm-pool time to reclaim, "
                "so serverless — which bills a higher per-DBU rate — would likely "
                "cost the same or more. The size and concurrency levers above are "
                "the better fit for this warehouse."
            ),
        )

    # No rule carried the warehouse type (rare — only size/scale-out fired on a
    # warehouse whose SQL didn't surface it). Don't guess.
    return ServerlessFit(
        verdict="cannot_estimate",
        headline="Serverless fit: not enough signal",
        reason=(
            "We couldn't confirm from billing data whether this warehouse is "
            "classic or serverless, so we're not estimating a serverless projection "
            "rather than guessing one."
        ),
    )


def _cluster_fit(options: list[Recommendation]) -> ServerlessFit:
    """Serverless-fit verdict for an all-purpose cluster.

    Fills the gap behind "why isn't serverless recommended for this cluster?".
    Serverless is not a toggle on an all-purpose cluster the way it is on a SQL
    warehouse — the equivalent levers are (a) moving scheduled work to serverless
    JOBS, or (b) auto-stop for interactive, human-attached compute. We never
    fabricate a dollar figure (engine invariant): the measured savings for these
    clusters already live on their own findings (interactive_to_job / auto-stop /
    zombie), so this card explains the serverless angle without a projection."""
    to_job = _find(options, "interactive_to_job")
    if to_job is not None:
        # Recurring jobs on an all-purpose cluster: serverless jobs are the natural
        # target. We price the (classic) jobs-compute move on that finding, but not
        # a serverless-rate figure — mirror the job path's honesty.
        return ServerlessFit(
            verdict="cannot_estimate",
            headline="Serverless jobs are a candidate — not yet estimated",
            reason=(
                "This is an all-purpose (interactive) cluster, so serverless isn't a "
                "toggle here the way it is on a SQL warehouse. Its recurring jobs are "
                "a strong fit for serverless jobs, which start per run and stop when "
                "the run ends — but we haven't modeled a serverless-rate dollar figure "
                "for this workload, so we won't show one. The 'move scheduled work off "
                "all-purpose' option above prices the measured move we can stand behind."
            ),
        )

    # An idle / abandoned interactive cluster (auto-termination, zombie, rightsize).
    # The serverless equivalent is auto-stop, not a warehouse-style type switch.
    return ServerlessFit(
        verdict="cannot_estimate",
        headline="For a cluster, auto-stop is the serverless-style lever",
        reason=(
            "Serverless is a SQL-warehouse setting, not an all-purpose cluster one, so "
            "there's no 'switch to serverless' toggle to recommend here. The equivalent "
            "for interactive compute is auto-stop — so it scales to zero when idle "
            "instead of billing around the clock — or, for scheduled work, moving it to "
            "serverless jobs. The measured saving is on the auto-stop / termination "
            "option above; we don't invent a separate serverless projection."
        ),
    )


def _job_fit(options: list[Recommendation]) -> ServerlessFit:
    cost_opt = _find(options, "serverless_cost_optimized")
    if cost_opt is not None:
        return ServerlessFit(
            verdict="recommended",
            headline="Already serverless — and can be cheaper",
            reason=(
                "This job already runs serverless. It's on the performance-optimized "
                "target; switching to Standard (cost-optimized) mode uses fewer DBUs "
                "at the same rate. The projected saving below is measured — see the "
                "cost-optimized option for the evidence."
            ),
            projected_monthly_savings_usd=round(cost_opt.monthly_savings_usd, 2),
            group_id=group_id(cost_opt.workspace_id, "job", cost_opt.resource_id),
        )

    to_job = _find(options, "interactive_to_job")
    if to_job is not None:
        # It runs on an all-purpose cluster today. Serverless jobs are the natural
        # target, but we haven't modeled the serverless-rate projection for this
        # path — the finding prices the (classic) jobs-compute move. Be honest.
        return ServerlessFit(
            verdict="cannot_estimate",
            headline="Serverless jobs are a candidate — not yet estimated",
            reason=(
                "This work runs on an always-on all-purpose cluster. Serverless jobs "
                "start per run and stop when the run ends, so they're a strong "
                "candidate — but we haven't modeled a serverless-rate dollar figure "
                "for this workload, so we won't show one. The 'run as a job' option "
                "above prices the move to (classic) jobs compute, which is the "
                "measured saving we can stand behind."
            ),
        )

    # A job flagged only for reliability/lifecycle reasons (failing, unused): its
    # compute class isn't what we can judge serverless fit from with these signals.
    return ServerlessFit(
        verdict="cannot_estimate",
        headline="Serverless fit: not enough signal",
        reason=(
            "The findings for this job are about reliability or lifecycle, not its "
            "compute class, so there's no serverless idle or performance-target "
            "signal to project a saving from. We won't estimate one rather than "
            "guess."
        ),
    )
