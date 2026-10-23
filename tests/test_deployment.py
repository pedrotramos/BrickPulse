import ast
import json
from pathlib import Path
import re
import runpy

import yaml

from brickpulse.rules import RuleContext, list_rules, load_thresholds
from brickpulse.rules.sources import qualified_tables, rule_dependencies, short_name


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = runpy.run_path(str(ROOT / "scripts/deploy.py"), run_name="deploy")


def resource(name):
    return yaml.safe_load((ROOT / "resources" / name).read_text())["resources"]


def test_preflight_tables_match_rendered_rule_sql():
    thresholds = load_thresholds()
    expected = {}
    for rule in list_rules():
        if thresholds.get(rule.id, {}).get("enabled") is False:
            continue
        for table in rule_dependencies(rule, RuleContext(thresholds=thresholds.get(rule.id, {}))):
            expected.setdefault(table, []).append(rule.id)
    stored = DEPLOY["rule_tables"]()
    assert {k: sorted(v) for k, v in stored.items()} == {k: sorted(v) for k, v in expected.items()}


def test_preflight_covers_non_rule_pipeline_sources():
    from brickpulse.rules.resolver import TableResolver
    from brickpulse.server.data.anomaly_sql import anomalies_sql, anomaly_daily_sql
    from brickpulse.server.data.apps_sql import apps_inventory_sql, apps_size_summary_sql, apps_daily_sql
    from brickpulse.server.data.genie_sql import genie_user_sql, genie_surface_sql, genie_space_sql, genie_daily_sql
    from brickpulse.server.data.spend_sql import workspace_spend_sql

    queries = [workspace_spend_sql(TableResolver.nested("system"))]
    queries += [fn() for fn in (anomalies_sql, anomaly_daily_sql, apps_inventory_sql,
        apps_size_summary_sql, apps_daily_sql, genie_user_sql, genie_surface_sql, genie_space_sql, genie_daily_sql)]
    needed = {short_name(table) for sql in queries for table in qualified_tables(sql)}
    assert needed <= DEPLOY["REQUIRED"]


def test_app_manifest_is_owner_only_and_reads_through_the_warehouse():
    app = resource("brickpulse.app.yml")["apps"]["brickpulse"]
    env = {e["name"]: e for e in app["config"]["env"]}
    assert app["config"]["command"] == ["python", "-m", "brickpulse"]
    assert app["permissions"] == [{"user_name": "${workspace.current_user.userName}", "level": "CAN_MANAGE"}]
    assert app["resources"][0]["sql_warehouse"] == {"id": "${var.warehouse_id}", "permission": "CAN_USE"}
    assert env["DATABRICKS_WAREHOUSE_ID"]["value_from"] == "sql-warehouse"
    assert env["PYTHONPATH"]["value"] == "src"
    requirements = (ROOT / "requirements.txt").read_text().split()
    assert "." not in requirements and not any(r.startswith(("-e", "brickpulse")) for r in requirements)
    assert env["BRICKPULSE_AUTH_MODE"]["value"] == "sp"
    assert json.loads(env["BRICKPULSE_USER_SCOPE_JSON"]["value"]) == {
        "${workspace.current_user.userName}": {"role": "platform_admin"}}
    assert env["BRICKPULSE_TABLE"]["value"] == "${var.catalog}.${var.schema}.finops_recommendations"
    assert env["BRICKPULSE_DISABLED_RULES"]["value"] == "${var.disabled_rules}"


def test_pipeline_and_job_run_the_bundle_wheel():
    pipeline = resource("precompute.pipeline.yml")["pipelines"]["precompute"]
    job = resource("daily_refresh.job.yml")["jobs"]["daily_refresh"]
    assert pipeline["serverless"] and not pipeline["continuous"]
    assert pipeline["event_log"]["name"] == "finops_run_log"
    assert pipeline["environment"]["dependencies"] == ["../dist/*.whl"]
    assert pipeline["configuration"]["brickpulse.disabled_rules"] == "${var.disabled_rules}"
    assert (ROOT / "resources" / pipeline["libraries"][0]["file"]["path"]).resolve().is_file()
    assert [t["task_key"] for t in job["tasks"]] == ["run_pipeline", "track_changes"]
    assert job["tasks"][0]["pipeline_task"]["pipeline_id"] == "${resources.pipelines.precompute.id}"
    assert (ROOT / "resources" / job["tasks"][1]["spark_python_task"]["python_file"]).resolve().is_file()
    assert job["environments"][0]["spec"]["dependencies"] == ["../dist/*.whl"]
    assert job["schedule"] == {"quartz_cron_expression": "0 0 5 * * ?", "timezone_id": "UTC",
                               "pause_status": "UNPAUSED"}


def test_bundle_has_no_default_target_and_no_workspace_values():
    bundle = yaml.safe_load((ROOT / "databricks.yml").read_text())
    assert set(bundle["targets"]) == {"dev", "prod"}
    for target in bundle["targets"].values():
        assert not target.get("default")
        assert "host" not in (target.get("workspace") or {})
    assert "/Shared" not in bundle["targets"]["prod"]["workspace"]["root_path"]
    # The app runs from synced source, and sync patterns match at any depth.
    source_dirs = {p.name for p in (ROOT / "src").rglob("*") if p.is_dir()}
    excluded = {p.split("/")[0] for p in bundle["sync"]["exclude"] if not p.startswith("**")}
    assert not excluded & source_dirs
    for name in ("catalog", "warehouse_id"):
        assert "default" not in bundle["variables"][name]
    committed = "\n".join(p.read_text() for p in [ROOT / "databricks.yml", *(ROOT / "resources").glob("*.yml")])
    assert not re.search(r"@[a-z0-9-]+\.[a-z]|[0-9a-f]{16}|cloud\.databricks\.com", committed)


def test_pipeline_uses_modern_batch_api_and_preserves_nulls():
    text = Path("src/brickpulse/pipelines/pipeline.py").read_text()
    tree = ast.parse(text)
    assert "import dlt" not in text
    assert "dp.materialized_view" in text
    assert "temporary=True" not in text
    assignments = {n.targets[0].id: ast.literal_eval(n.value) for n in tree.body
        if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "_JSON_OPTIONS"}
    assert assignments["_JSON_OPTIONS"]["ignoreNullFields"] == "false"
