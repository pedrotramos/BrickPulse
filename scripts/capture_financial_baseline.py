"""Generate a regression artifact from either the original or current package.

Run with the original package on PYTHONPATH to establish the frozen fixture.
No dates, prose or localization references are part of the financial baseline.
"""
import importlib
from datetime import date
import json
import os
import sys

prefix = sys.argv[1]
fixed_day = os.environ.get("BRICKPULSE_BASELINE_DATE")
if fixed_day:
    baseline_day = date.fromisoformat(fixed_day)

    class FrozenDate(date):
        @classmethod
        def today(cls):
            return baseline_day

    # The synthetic rows use relative dates. Pin their clock when comparing a
    # frozen financial fixture, while leaving the app's normal demo clock live.
    for module_name in ("data.demo_source", "data.demo_rule_rows"):
        try:
            module = importlib.import_module(f"{prefix}.{module_name}")
        except ImportError:
            continue  # The original package may not have demo_rule_rows.
        module.date = FrozenDate

settings = importlib.import_module(f"{prefix}.config").Settings(compute_mode="demo")
source_type = importlib.import_module(f"{prefix}.data.demo_source").DemoRecommendationSource
scope = importlib.import_module(f"{prefix}.identity").Scope(all_workspaces=True)
service_type = importlib.import_module(f"{prefix}.services.recommendation_service").RecommendationService
aggregation = importlib.import_module(f"{prefix}.services.aggregation")
source = source_type(settings, None)
service = service_type(source)
fields = ["rule_id", "resource_id", "workspace_id", "monthly_waste_usd", "monthly_savings_usd", "monthly_savings_low_usd", "monthly_savings_high_usd", "savings_status", "savings_composition", "affected_spend_scope", "effort_score", "nba_score", "priority"]
result = {
    "findings": sorted([{key: getattr(rec, key) for key in fields} for rec in source.load(scope)], key=lambda x: (x["workspace_id"], x["rule_id"], x["resource_id"])),
    "overview": aggregation.account_overview(source.fetch(scope)).model_dump(mode="json"),
    "groups": sorted([{key: getattr(group, key) for key in ("group_id", "option_count", "monthly_savings_usd", "monthly_waste_usd", "options_savings_sum_usd", "nba_score")} for group in service.list_grouped(scope)], key=lambda x: x["group_id"]),
    "summary": service.grouped_summary(scope).model_dump(mode="json"),
}
if len(sys.argv) > 2:
    from pathlib import Path
    destination = Path(sys.argv[2])
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
else:
    print(json.dumps(result, sort_keys=True))
