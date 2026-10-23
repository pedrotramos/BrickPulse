from types import SimpleNamespace

from brickpulse.server.identity import Scope
from brickpulse.server.services import aggregation


def _rec(rule_id, savings, *, resource="wh-1", waste=1000.0, composition="exclusive",
         status="estimated", category="sql", scope=None):
    # The composition functions are duck-typed (see aggregation.scope_key).
    return SimpleNamespace(
        rule_id=rule_id, category=category, workspace_id="ws", resource_type="warehouse",
        resource_id=resource, monthly_savings_usd=savings, monthly_waste_usd=waste,
        savings_composition=composition, savings_status=status, affected_spend_scope=scope,
    )


def _slices(recs, key):
    return {s.key: s.monthly_savings_usd for s in aggregation._savings_breakdown(recs, key)}


def test_overlapping_exclusive_options_book_once_to_the_best_rule():
    recs = [_rec("autostop", 300.0), _rec("rightsize", 500.0), _rec("serverless", 450.0)]
    assert _slices(recs, lambda r: r.rule_id) == {"rightsize": 500.0}
    assert aggregation._addressable_savings_total(recs) == 500.0


def test_additive_options_are_credited_to_their_own_rules_within_the_pool():
    recs = [
        _rec("autostop", 700.0, composition="additive"),
        _rec("rightsize", 600.0, composition="additive"),
    ]
    # Highest first, the second takes only what is left of the 1000 pool.
    assert _slices(recs, lambda r: r.rule_id) == {"autostop": 700.0, "rightsize": 300.0}


def test_advisory_findings_never_appear_in_a_breakdown():
    recs = [_rec("advice", 900.0, status="advisory"), _rec("rightsize", 200.0, resource="wh-2")]
    assert _slices(recs, lambda r: r.rule_id) == {"rightsize": 200.0}


def test_demo_breakdowns_sum_to_the_headline(source):
    overview = aggregation.account_overview(source.fetch(Scope(all_workspaces=True)))
    total = overview.total_monthly_savings_addressable_usd
    for slices in (overview.savings_by_category, overview.savings_by_rule):
        assert slices, "demo data should populate every breakdown"
        assert abs(sum(s.monthly_savings_usd for s in slices) - total) < 0.05
        amounts = [s.monthly_savings_usd for s in slices]
        assert amounts == sorted(amounts, reverse=True)
        assert all(s.resource_count > 0 for s in slices)
