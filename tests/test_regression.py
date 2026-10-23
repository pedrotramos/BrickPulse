import json
import os
from pathlib import Path
import random
import subprocess
import sys

import pytest

from brickpulse.rules import RuleContext, get_rule, list_rules, load_thresholds
from brickpulse.server.data.demo_rule_rows import GENERATORS, scored_demo_finding
from brickpulse.server.identity import Scope
from brickpulse.server.services.recommendation_service import RecommendationService


def test_original_financial_contract():
    output = subprocess.check_output(
        [sys.executable, "scripts/capture_financial_baseline.py", "brickpulse.server"],
        env={**os.environ, "PYTHONPATH": "src", "BRICKPULSE_BASELINE_DATE": "2026-10-21"}, text=True,
    )
    assert json.loads(output) == json.loads(Path("tests/fixtures/financial_baseline.json").read_text())


@pytest.mark.parametrize("rule", list_rules(), ids=lambda r: r.id)
def test_all_rule_templates_render(rule):
    thresholds = load_thresholds()
    for contract_prices, cloud_costs in [(False, False), (True, True)]:
        sql = rule.render_sql(RuleContext(thresholds=thresholds.get(rule.id, {}),
            contract_prices=contract_prices, cloud_costs=cloud_costs))
        assert sql.strip()
        assert "{{" not in sql and "{%" not in sql
        assert rule.id in thresholds


@pytest.mark.parametrize("rule_id", GENERATORS)
def test_real_scoring_is_pure_and_estimates_are_bounded(rule_id):
    rule = get_rule(rule_id)
    for seed in range(3):
        finding = scored_demo_finding(rule, workspace_id="123", resource_id="456",
            resource_name="Example", rng=random.Random(seed), thresholds=load_thresholds())
        assert finding is not None
        assert finding.monthly_waste_usd >= 0
        assert 0 <= finding.monthly_savings_usd <= finding.monthly_waste_usd + 0.01
        assert finding.evidence


def test_pagination_uses_one_snapshot_and_preserves_filters(source, monkeypatch):
    service = RecommendationService(source)
    scope = Scope(all_workspaces=True)
    expected = service.list_grouped(scope, sort="savings", priority="HIGH")
    calls = []
    load = source.load
    monkeypatch.setattr(source, "load", lambda s: (calls.append(s), load(s))[1])
    page = service.grouped_page(scope, page=2, page_size=7, sort="savings", priority="HIGH")
    assert len(calls) == 1
    assert page["total"] == len(expected)
    assert [r["group_id"] for r in page["items"]] == [r.group_id for r in expected[7:14]]
    assert all("options" not in r and "evidence" not in r for r in page["items"])
    assert page["summary"]["resources"] == len(expected)


def test_detail_reuses_one_load(source, monkeypatch):
    scope = Scope(all_workspaces=True)
    rec = source.fetch(scope)[0]
    calls = []
    load = source.load
    monkeypatch.setattr(source, "load", lambda s: (calls.append(s), load(s))[1])
    assert RecommendationService(source).get(scope, rec.finding_key).finding_key == rec.finding_key
    assert len(calls) == 1
