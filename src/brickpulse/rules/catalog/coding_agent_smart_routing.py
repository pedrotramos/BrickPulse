"""Rule: coding agents on frontier models without Unity AI Gateway Smart Routing.

EXPERIMENTAL (Labs tab). Smart Routing picks the cheapest capable ``system.ai.*``
model once per coding-agent session (Claude Code, Codex, Omnigent). The workspace
admin enables the "Enhanced Unity Gateway" preview and developers launch through
``ug claude`` / ``ug codex``. This rule finds the workspace's coding-agent sessions
that the router never saw, and prices them.

**Why not token mean / standard deviation per request.** Measured on
On a test account: short requests (< 2k input, < 300 output tokens) are ~0% of
frontier-model spend. Frontier spend is 100k–400k-token agent contexts, and the
spread of per-request tokens tracks context growth inside a session, not how hard
the task is. The router decides per SESSION, so the rule works on sessions.

**The estimate is the workspace's own routing effect.** The router's classifier
calls are logged (``invocation_metadata.source = 'ROUTER'``), so routed and
unrouted sessions can be told apart. Cost per turn is compared by session size
(short vs long), because the router saves most on short sessions and long sessions
hold most of the spend. Each bucket's measured reduction (capped at
``max_savings_rate`` — routed sessions are self-selected) is applied to the
unrouted spend in that bucket. Too few routed sessions → ``default_savings_rate``.

**Advisory, $0 booked** (Labs convention). ``monthly_waste_usd`` = unrouted spend
on premium models (addressable); the estimate is in the copy and the evidence.
"""

from __future__ import annotations

from brickpulse.rules.contract import (
    EffortBand,
    Rule,
    RuleContext,
    RuleFinding,
    RulePriority,
)
from brickpulse.rules.evidence import (
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    evidence_block,
    viz_daily_stack,
    viz_ranked_bars,
)
from brickpulse.rules.history import history_note, insufficient_history
from brickpulse.rules.pricing import to_monthly
from brickpulse.rules.registry import register

_BASIS = (
    "system.ai_gateway.usage (coding-agent requests: harness user agent on the "
    "Anthropic Messages / OpenAI Responses APIs against system.ai.* models; router "
    "classifier calls marked invocation_metadata.source = 'ROUTER') × "
    "system.billing.usage priced at list, attributed per gateway endpoint and model "
    "through usage_metadata.ai_gateway. {n}-day lookback. A session is one user + one "
    "tool split at a {gap}-minute gap (no session id is logged); it counts as routed "
    "when that user made a router call during it. Per-model dollars are exact; the "
    "split between sessions of one model uses a token weight (uncached input + 0.1 × "
    "cache reads + 5 × output). The saving is an estimate from this workspace's own "
    "routed sessions, which users chose to route, so it is not booked."
)


def _rate(usd, turns) -> float | None:
    usd, turns = float(usd or 0), float(turns or 0)
    return usd / turns if turns > 0 else None


def _bucket_reduction(row: dict, bucket: str, t: dict) -> tuple[float, bool]:
    """(reduction, measured) for one session-size bucket: 1 - routed $/turn over
    unrouted $/turn when enough routed sessions exist, else the default rate."""
    default = float(t.get("default_savings_rate", 0.15))
    cap = float(t.get("max_savings_rate", 0.6))
    if int(row.get(f"{bucket}_routed_sessions") or 0) < int(t.get("min_routed_sessions", 20)):
        return default, False
    unrouted = _rate(row.get(f"{bucket}_unrouted_usd"), row.get(f"{bucket}_unrouted_turns"))
    routed = _rate(row.get(f"{bucket}_routed_usd"), row.get(f"{bucket}_routed_turns"))
    if not unrouted or routed is None:
        return default, False
    return max(0.0, min(cap, 1.0 - routed / unrouted)), True


def _turn_msgs(row_msg: str, max_turns, turns) -> dict:
    """Message ids for one cost-per-turn bar, so its label renders localized."""
    return {"msg": row_msg, "params": {"turns": max_turns},
            "sub_msg": "turns", "sub_params": {"count": int(turns or 0)}}


def _fmt_tokens(n) -> str:
    n = float(n or 0)
    return f"{n / 1000:.0f}k" if n >= 1000 else f"{n:.0f}"


@register
class CodingAgentSmartRoutingRule(Rule):
    id = "coding_agent_smart_routing"
    experimental = True
    default_savings_status = "advisory"
    version = "0.1.0"
    title = "Route coding agents through Smart Routing"
    resource_type = "ai_gateway"
    category = "serving"
    contract_pricing = "none"  # advisory: no booked dollar figure to re-price
    sql_file = "coding_agent_smart_routing.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        if insufficient_history(row, ctx):
            return None

        t = ctx.thresholds
        n = ctx.lookback_days
        sessions = int(row.get("sessions") or 0)
        routed_sessions = int(row.get("routed_sessions") or 0)
        unrouted_sessions = sessions - routed_sessions
        if unrouted_sessions < int(t.get("min_unrouted_sessions", 10)):
            return None
        addressable = to_monthly(row.get("unrouted_premium_cost_usd") or 0, n)
        if addressable < float(t.get("min_addressable_usd", 100)):
            return None

        short_red, short_measured = _bucket_reduction(row, "short", t)
        long_red, long_measured = _bucket_reduction(row, "long", t)
        estimate = to_monthly(
            float(row.get("short_unrouted_usd") or 0) * short_red
            + float(row.get("long_unrouted_usd") or 0) * long_red,
            n,
        )
        unrouted_monthly = to_monthly(row.get("unrouted_cost_usd") or 0, n)
        blended = estimate / unrouted_monthly if unrouted_monthly > 0 else 0.0
        measured = short_measured or long_measured

        users = int(row.get("agent_users") or 0)
        routed_users = int(row.get("routed_users") or 0)
        custom = to_monthly(row.get("custom_endpoint_cost_usd") or 0, n)
        max_turns = int(t.get("short_session_max_turns", 40))

        if estimate >= 1000:
            priority = RulePriority.HIGH
        elif estimate >= 200:
            priority = RulePriority.MEDIUM
        else:
            priority = RulePriority.LOW

        adoption = (
            f"{routed_users} of {users} developers already route some sessions "
            f"({routed_sessions:,} of {sessions:,} sessions)."
            if routed_users
            else (
                "The one developer using coding agents here doesn't route yet."
                if users == 1
                else f"None of the {users} developers using coding agents here routes yet."
            )
        )
        if measured:
            basis_clause = (
                f"In this workspace, routed sessions of up to {max_turns} turns cost "
                f"{short_red * 100:.0f}% less per turn than unrouted ones, and longer "
                f"sessions {long_red * 100:.0f}% less."
            )
        else:
            basis_clause = (
                f"Too few routed sessions here to measure the effect, so a conservative "
                f"{short_red * 100:.0f}% is assumed (Databricks publishes 30%+ on its "
                f"coding benchmarks)."
            )
        why = (
            f"Coding agents (Claude Code, Codex) in this workspace spent "
            f"~${addressable:,.0f}/mo on frontier models in sessions Smart Routing never "
            f"saw. {adoption} {basis_clause} Applied to the unrouted sessions, that is "
            f"~${estimate:,.0f}/mo ({blended * 100:.0f}% of their spend). "
            + (
                "This is an estimate: routed sessions were chosen by the users who "
                "routed them, so they may have been the easier tasks."
                if measured
                else "This is an estimate, not measured in this workspace."
            )
        )
        how = (
            "An account admin enables the Enhanced Unity Gateway preview (account console "
            "> Previews). Developers then launch with `ug claude` or `ug codex` (or add "
            "--enable-smart-routing); an explicit --model still wins. Each user needs "
            "EXECUTE on every system.ai model the router may pick."
            + (
                f" ~${custom:,.0f}/mo of coding-agent traffic goes to custom gateway "
                f"endpoints the router cannot choose; point those tools at system.ai "
                f"models first."
                if custom >= 1
                else ""
            )
        )

        viz = viz_daily_stack(
            title="Coding-agent spend per day",
            unit="$",
            series=[
                ("up", "Unrouted · frontier models", "warn"),
                ("uo", "Unrouted · other models", "info"),
                ("r", "Routed sessions", "savings"),
            ],
            rows=row.get("days") or [],
            lookback_days=n,
            note="Amber is what Smart Routing would choose a model for.",
            msg="agent_daily_spend",
        )
        cents = lambda usd, turns: 100 * (_rate(usd, turns) or 0)  # noqa: E731
        per_turn = viz_ranked_bars(
            title="Cost per turn, routed vs not",
            unit="¢/turn",
            rows=[
                {"label": f"Unrouted · ≤ {max_turns} turns",
                 "value": cents(row.get("short_unrouted_usd"), row.get("short_unrouted_turns")),
                 "sub": f"{int(row.get('short_unrouted_turns') or 0):,} turns", "color": "warn",
                 **_turn_msgs("unrouted_short", max_turns, row.get("short_unrouted_turns"))},
                {"label": f"Routed · ≤ {max_turns} turns",
                 "value": cents(row.get("short_routed_usd"), row.get("short_routed_turns")),
                 "sub": f"{int(row.get('short_routed_turns') or 0):,} turns", "color": "savings",
                 **_turn_msgs("routed_short", max_turns, row.get("short_routed_turns"))},
                {"label": f"Unrouted · > {max_turns} turns",
                 "value": cents(row.get("long_unrouted_usd"), row.get("long_unrouted_turns")),
                 "sub": f"{int(row.get('long_unrouted_turns') or 0):,} turns", "color": "warn",
                 **_turn_msgs("unrouted_long", max_turns, row.get("long_unrouted_turns"))},
                {"label": f"Routed · > {max_turns} turns",
                 "value": cents(row.get("long_routed_usd"), row.get("long_routed_turns")),
                 "sub": f"{int(row.get('long_routed_turns') or 0):,} turns", "color": "savings",
                 **_turn_msgs("routed_long", max_turns, row.get("long_routed_turns"))},
            ],
            keep_order=True,
            msg="agent_cost_per_turn",
        ) if routed_sessions else None  # nothing to compare against yet
        by_model = viz_ranked_bars(
            title="Unrouted spend by model",
            unit="$/mo",
            rows=[
                {"label": m.get("model"), "value": to_monthly(m.get("usd") or 0, n),
                 "sub": (f"{m.get('users') or 0} users · avg {_fmt_tokens(m.get('avg_in'))} in / "
                         f"{_fmt_tokens(m.get('avg_out'))} out tokens"),
                 "sub_msg": "model_usage",
                 "sub_params": {"users": int(m.get("users") or 0),
                                "tokens_in": _fmt_tokens(m.get("avg_in")),
                                "tokens_out": _fmt_tokens(m.get("avg_out"))},
                 "color": "violet"}
                for m in (row.get("top_models") or [])
            ],
            note="Big, steady contexts are agent sessions: token counts alone don't say "
                 "how hard the task was, which is why the router decides per session.",
            msg="agent_spend_by_model",
        )

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type=self.resource_type,
            resource_id="coding-agents",
            resource_name="Coding agents (Claude Code / Codex)",
            monthly_waste_usd=round(addressable, 2),
            monthly_savings_usd=0.0,
            savings_status="advisory",
            monthly_savings_low_usd=0.0,
            effort=EffortBand.LOW,  # one admin toggle, then a different launch command
            priority=priority,
            why=why,
            how=how,
            evidence=evidence_block(
                confidence=CONFIDENCE_MEDIUM if measured else CONFIDENCE_LOW,
                data_basis=_BASIS.format(n=n, gap=t.get("session_gap_minutes", 30))
                + history_note(row, ctx),
                viz=viz,
                viz_extra=[per_turn, by_model],
                estimated_monthly_savings_usd=round(estimate, 2),
                estimated_savings_rate=round(blended, 3),
                savings_rate_measured=measured,
                short_session_reduction=round(short_red, 3),
                long_session_reduction=round(long_red, 3),
                unrouted_monthly_usd=round(unrouted_monthly, 2),
                custom_endpoint_monthly_usd=round(custom, 2),
                sessions=sessions,
                routed_sessions=routed_sessions,
                agent_users=users,
                routed_users=routed_users,
                requests=row.get("requests"),
                unpriced_requests=row.get("unpriced_requests"),
                top_models=row.get("top_models") or [],
                observed_days=row.get("observed_days"),
            ),
        )
