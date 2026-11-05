"""Scoring guards for the Labs coverage rules: lakebase_scale_to_zero and
serverless_untagged_spend. Rows mirror the shape of each rule's SQL output."""

import pytest

from brickpulse.rules import RuleContext, get_rule, load_thresholds
from brickpulse.rules.contract import RulePriority


def _ctx(rule_id):
    return RuleContext(thresholds=load_thresholds()[rule_id], lookback_days=30)


def _lakebase_row(**overrides):
    # 4 CU floor billed every hour for 7 days; a client in 10 of the 168 hours.
    row = {
        "workspace_id": "123", "endpoint_id": "ep-a", "project_id": "p", "branch_id": "b",
        "project_name": "orders", "branch_name": "dev", "endpoint_name": "primary",
        "compute_mode": "primary", "floor_cu": 4.0, "peak_cu": 4.0,
        "floor_dbu_per_hour": 0.639, "billed_hours": 168, "telemetry_hours": 168,
        "idle_hours": 158, "client_hours": 10, "observed_days": 7,
        "cost_window_usd": 28.0, "dbus_30d": 107.0, "unpriced_dbus": 0.0,
        "reclaimable_idle_hours": 150, "reclaimable_cost_usd": 24.5,
        "longest_idle_run_hours": 40, "window_days": 7, "daily_activity": [],
    }
    row.update(overrides)
    return row


class TestLakebaseScaleToZero:
    rule = get_rule("lakebase_scale_to_zero")

    def test_is_labs_and_estimated(self):
        assert self.rule.experimental
        assert self.rule.default_savings_status == "estimated"

    def test_partial_idle_is_priced_on_the_rows_own_window(self):
        f = self.rule.score(_lakebase_row(), _ctx(self.rule.id))
        assert f is not None
        assert f.resource_type == "lakebase_endpoint"
        # 7-day window extrapolated to a month, not the engine's 30-day lookback.
        assert f.monthly_waste_usd == pytest.approx(28.0 * 30 / 7, rel=0.01)
        assert f.monthly_savings_usd == pytest.approx(24.5 * 30 / 7, rel=0.01)
        assert 0 < f.monthly_savings_usd <= f.monthly_waste_usd
        assert f.evidence["tier"] == "idle_stretches"
        assert f.evidence["confidence"] == "high"

    def test_fully_idle_is_high_priority(self):
        f = self.rule.score(_lakebase_row(client_hours=0, idle_hours=168), _ctx(self.rule.id))
        assert f.evidence["tier"] == "fully_idle"
        assert f.priority == RulePriority.HIGH
        assert "delete the branch" in f.how

    def test_default_branch_suggests_the_project_not_the_branch(self):
        f = self.rule.score(
            _lakebase_row(client_hours=0, idle_hours=168, branch_name="production"),
            _ctx(self.rule.id),
        )
        assert "delete the project" in f.how
        assert "delete the branch" not in f.how

    @pytest.mark.parametrize("overrides", [
        {"compute_mode": "replica"},                   # replica support not verified
        {"floor_cu": 48.0},                            # max >= min > 32 CU: not allowed
        {"telemetry_hours": 100},                      # 60% of billed hours observed
        {"observed_days": 2},                          # too little history
        {"unpriced_dbus": 107.0},                      # nothing could be priced
        {"reclaimable_cost_usd": 1.0},                 # below the monthly floor
    ])
    def test_guards_suppress(self, overrides):
        assert self.rule.score(_lakebase_row(**overrides), _ctx(self.rule.id)) is None

    def test_floor_between_16_and_32_cu_warns(self):
        f = self.rule.score(_lakebase_row(floor_cu=24.0), _ctx(self.rule.id))
        assert f is not None
        assert "32 CU" in f.evidence["data_warning"]

    def test_floor_up_to_16_cu_needs_no_warning(self):
        f = self.rule.score(_lakebase_row(floor_cu=16.0), _ctx(self.rule.id))
        assert "data_warning" not in f.evidence

    def test_saving_scales_with_the_regional_price_only(self):
        # Same endpoint, same hours, priced at US East vs Brazil rates: the saving
        # follows the cost ratio and nothing else about the finding changes.
        us = self.rule.score(_lakebase_row(), _ctx(self.rule.id))
        ratio = 0.535 / 0.26
        br = self.rule.score(
            _lakebase_row(cost_window_usd=28.0 * ratio, reclaimable_cost_usd=24.5 * ratio),
            _ctx(self.rule.id),
        )
        assert br.monthly_savings_usd == pytest.approx(us.monthly_savings_usd * ratio, rel=0.001)
        assert br.evidence["floor_cu"] == us.evidence["floor_cu"]
        assert br.evidence["tier"] == us.evidence["tier"]

    def test_daily_activity_accepts_json_text(self):
        f = self.rule.score(
            _lakebase_row(daily_activity='[{"d":"2026-11-01","billed_hours":24,"client_hours":2}]'),
            _ctx(self.rule.id),
        )
        assert f.evidence["daily_activity"] == [
            {"d": "2026-11-01", "billed_hours": 24, "client_hours": 2}
        ]


def _tags_row(**overrides):
    row = {
        "workspace_id": "123", "resource_id": "app-1", "resource_type": "app",
        "product": "APPS", "app_name": "sales-assistant", "cost_30d_usd": 300.0,
        "dbus_30d": 4000.0, "unpriced_dbus": 0.0, "observed_days": 30,
        "identity_hint": "someone@example.com",
        "missing_tags": ["cost_center", "team"], "required_tag_count": 5,
    }
    row.update(overrides)
    return row


class TestServerlessUntaggedSpend:
    rule = get_rule("serverless_untagged_spend")

    def test_is_labs_governance_and_free(self):
        f = self.rule.score(_tags_row(), _ctx(self.rule.id))
        assert self.rule.experimental and self.rule.category == "governance"
        assert f.monthly_savings_usd == 0.0
        assert f.monthly_waste_usd == pytest.approx(300.0)
        assert f.evidence["missing_tags"] == ["cost_center", "team"]
        assert f.resource_name == "sales-assistant"

    def test_missing_tags_as_json_text(self):
        f = self.rule.score(_tags_row(missing_tags='["team"]'), _ctx(self.rule.id))
        assert f.evidence["missing_tags"] == ["team"]

    @pytest.mark.parametrize("resource_type", [
        "app", "pipeline", "lakebase_endpoint", "vector_search_endpoint", "serving_endpoint",
    ])
    def test_each_product_is_named_and_actionable(self, resource_type):
        f = self.rule.score(_tags_row(resource_type=resource_type, app_name=None), _ctx(self.rule.id))
        assert f.resource_type == resource_type
        assert f.resource_name == "app-1"
        assert "usage policy" in f.how

    @pytest.mark.parametrize("overrides", [
        {"missing_tags": []},                 # fully tagged
        {"cost_30d_usd": 10.0},               # below the floor
        {"resource_type": "cluster"},         # untagged_spend's territory
        {"observed_days": 1},                 # too new to nag about
    ])
    def test_guards_suppress(self, overrides):
        assert self.rule.score(_tags_row(**overrides), _ctx(self.rule.id)) is None

    def test_shares_the_tag_aliases_with_untagged_spend(self):
        t = load_thresholds()
        assert t["serverless_untagged_spend"]["required_tags"] == t["untagged_spend"]["required_tags"]

    def test_sql_is_disjoint_from_untagged_spend(self):
        sql = self.rule.render_sql(_ctx(self.rule.id))
        for col in ("cluster_id", "warehouse_id", "job_id"):
            assert f"u.usage_metadata.{col} IS NULL" in sql
        assert "LIKE 'databricks-%'" in sql
