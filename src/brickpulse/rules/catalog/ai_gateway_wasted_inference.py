"""Rule: AI-gateway requests failing and wasting inference (operational).

EXPERIMENTAL (Labs tab). The "AI Gateway efficiency" opportunity: a serving
endpoint fronted by the AI Gateway that keeps returning ERRORS is a broken client
integration — the requests reach the gateway, often invoke the model (5xx) and burn
input tokens, and return nothing usable. Like a repeatedly-failing SQL query (rule A)
one layer up, a client hammering an endpoint with the same failing call is directly
avoidable work.

**Operational, $0.** AI-gateway / foundation-model traffic bills per token under a
model SKU that system.billing.usage does not split cleanly by endpoint_id (a gateway
endpoint's per-endpoint serving cost comes back ~$0), so this rule reports the failed
requests and the input tokens they wasted — not a booked dollar figure. Experimental
(Labs, see ``Rule.experimental``) — out of the headline entirely.
"""

from __future__ import annotations

from brickpulse.rules.contract import (
    EffortBand,
    Rule,
    RuleContext,
    RuleFinding,
    RulePriority,
)
from brickpulse.rules.evidence import CONFIDENCE_HIGH, evidence_block, viz_ai_gateway_errors
from brickpulse.rules.history import history_note, insufficient_history
from brickpulse.rules.registry import register

_BASIS = (
    "system.ai_gateway.usage (status_code >= 400 marks a failed request; input_tokens on "
    "failures = wasted inference), {n}-day lookback. Failures are DIRECTLY counted, not "
    "inferred. The failure signal has high confidence. No dollar figure is shown: gateway / "
    "foundation-model traffic bills per token under a model SKU that system.billing.usage "
    "does not split by endpoint, so this is an operational finding, not a booked or "
    "addressable DBU saving. Experimental (Labs): validate the failing caller before acting."
)


def _status_phrase(code: int) -> str:
    """A short human gloss for the most-common failing HTTP status, so the 'why' tells
    the reader what kind of failure dominates (auth, bad request, overload)."""
    if code in (401, 403):
        return f"{code} (authentication/permission)"
    if code == 400:
        return "400 (malformed request)"
    if code == 404:
        return "404 (model or route not found)"
    if code == 429:
        return "429 (rate-limited — the endpoint is over its throughput)"
    if code >= 500:
        return f"{code} (server error — the model call itself failed)"
    return str(code)


@register
class AiGatewayWastedInferenceRule(Rule):
    id = "ai_gateway_wasted_inference"
    experimental = True
    # Operational: the failure signal is measured, but per-endpoint token cost is not
    # reliably attributable from system tables, so nothing is booked or addressed in $.
    default_savings_status = "advisory"
    version = "0.1.0"
    title = "AI-gateway requests failing and wasting inference"
    resource_type = "serving_endpoint"
    category = "serving"
    contract_pricing = "none"  # $0 advisory: no dollar figure to re-price
    sql_file = "ai_gateway_wasted_inference.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        # A single bad hour is not the recurring pattern this rule targets — require
        # enough distinct days first (finops_rules.history).
        if insufficient_history(row, ctx):
            return None

        t = ctx.thresholds
        requests = int(row.get("requests") or 0)
        failed = int(row.get("failed_requests") or 0)
        if failed < int(t.get("min_failed_requests", 100)):
            return None
        error_rate = float(row.get("error_rate") or 0.0)
        if error_rate < float(t.get("min_error_rate", 0.05)):
            return None  # a few errors are normal; only a habit is a finding

        server_errors = int(row.get("server_errors") or 0)
        failed_tokens = int(row.get("failed_input_tokens") or 0)
        top_code = int(row.get("top_status_code") or 0)
        top_count = int(row.get("top_status_count") or 0)
        name = row.get("endpoint_name") or str(row["endpoint_id"])

        # Priority on how much of the traffic fails AND how much wasted inference it
        # represents (server errors that burned tokens are the strongest case).
        if error_rate >= 0.5 or failed_tokens >= 1_000_000:
            priority = RulePriority.HIGH
        elif error_rate >= 0.2 or failed_tokens >= 100_000 or server_errors >= 100:
            priority = RulePriority.MEDIUM
        else:
            priority = RulePriority.LOW

        top_clause = ""
        if top_code and top_count > 1:
            top_clause = (
                f" The most common failure is HTTP {_status_phrase(top_code)}, "
                f"{top_count:,} times."
            )
        token_clause = ""
        if server_errors > 0:
            token_clause = (
                f" {server_errors:,} were 5xx server errors"
                + (
                    f", where the model was typically invoked and ~{failed_tokens:,} input "
                    f"tokens were billed for no usable output."
                    if failed_tokens
                    else ", where the model call itself failed."
                )
            )
        why = (
            f"Serving endpoint '{name}' returned an error on {failed:,} of its "
            f"{requests:,} AI-gateway requests ({error_rate * 100:.0f}%) over the last "
            f"{ctx.lookback_days} days.{top_clause}{token_clause} The measured error "
            f"pattern warrants investigation, but the usage table does not establish "
            f"whether the cause is the caller, capacity, permissions, or the model."
        )
        how = (
            "Filter system.ai_gateway.usage (or the endpoint's Serving logs) to this "
            "endpoint and status_code >= 400, group by the failing caller/route, and fix "
            "or throttle it (a bad auth token, a malformed schema, a retry storm, or an "
            "over-subscribed model that needs more provisioned throughput). No dollar "
            "saving is booked: gateway token cost isn't split per endpoint in the billing "
            "tables. The operational win is fewer failed requests and wasted inference."
        )

        viz = viz_ai_gateway_errors(
            days=row.get("days") or [],
            lookback_days=ctx.lookback_days,
            error_rate=error_rate,
            failed_requests=failed,
            total_requests=requests,
            server_errors=server_errors,
            failed_input_tokens=failed_tokens,
            top_status_code=top_code or None,
            top_status_count=top_count,
        )

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="serving_endpoint",
            resource_id=str(row["endpoint_id"]),
            resource_name=name,
            # Operational finding: gateway token cost is not reliably per-endpoint, so no
            # priceable spend. The failed-request / wasted-token counts are the metric,
            # carried in the evidence + why.
            monthly_waste_usd=0.0,
            monthly_savings_usd=0.0,
            savings_status="advisory",
            monthly_savings_low_usd=0.0,
            effort=EffortBand.MEDIUM,  # find the caller behind it, then fix/throttle
            priority=priority,
            why=why,
            how=how,
            evidence=evidence_block(
                confidence=CONFIDENCE_HIGH,
                data_basis=_BASIS.format(n=ctx.lookback_days) + history_note(row, ctx),
                viz=viz,
                requests=requests,
                failed_requests=failed,
                server_errors=server_errors,
                failed_input_tokens=failed_tokens,
                error_rate=row.get("error_rate"),
                top_status_code=top_code or None,
                top_status_count=top_count,
                observed_days=row.get("observed_days"),
                operational_only=True,
            ),
        )
