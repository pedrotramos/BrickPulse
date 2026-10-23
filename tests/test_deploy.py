import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from brickpulse.rules import apply_disabled, load_thresholds


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/deploy.py"
spec = importlib.util.spec_from_file_location("deploy", SCRIPT)
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class FakeWorkspace:
    """Records every call; answers like a workspace where everything already exists."""

    def __init__(self, profile, target, *, unreadable=(), installed=True):
        self.profile, self.target = profile, target
        self.unreadable, self.installed = set(unreadable), installed
        self.calls, self.statements = [], []

    def cli(self, *args, **kwargs):
        self.calls.append(("cli", args))
        if args[:2] == ("current-user", "me"):
            return {"userName": "owner@example.com"}
        if args[:2] == ("apps", "get"):
            return {"service_principal_client_id": "sp-1", "url": "https://app",
                    "compute_status": {"state": "ACTIVE"}, "app_status": {"state": "RUNNING"}}
        if args[:2] == ("tables", "list"):
            return [{"name": "finops_recommendations", "table_type": "MATERIALIZED_VIEW"},
                    {"name": "finops_savings_daily", "table_type": "MANAGED"}]
        return {"name": "wh"}

    def exists(self, *args):
        self.calls.append(("exists", args))
        return self.installed or args[0] != "tables"

    def bundle(self, *args, capture=False, **kwargs):
        self.calls.append(("bundle", args))
        if args[:1] == ("validate",) and "-o" in args:
            return SimpleNamespace(stdout=json.dumps({
                "variables": {"catalog": {"value": "cat"}, "schema": {"value": "brickpulse"}},
                "resources": {"apps": {"brickpulse": {"name": "brickpulse"}}}}))
        return SimpleNamespace(stdout="{}")

    def sql(self, warehouse_id, statement):
        self.statements.append(statement)
        table = statement.removeprefix("SELECT * FROM system.").removesuffix(" LIMIT 0")
        return "PERMISSION_DENIED" if table in self.unreadable else None


@pytest.fixture
def run(monkeypatch, tmp_path):
    monkeypatch.setattr(deploy, "overrides_path", lambda target: tmp_path / target / "overrides.json")

    def go(*argv, **kwargs):
        made = []
        monkeypatch.setattr(deploy, "Workspace", lambda p, t: made.append(FakeWorkspace(p, t, **kwargs)) or made[-1])
        assert deploy.main(["--profile", "p", "--target", "prod", *argv]) == 0
        return made[0], json.loads((tmp_path / "prod" / "overrides.json").read_text())

    return go


def bundle_steps(ws):
    return [args[:2] for kind, args in ws.calls if kind == "bundle" and "-o" not in args]


def test_cli_and_bundle_calls_name_profile_and_target(monkeypatch):
    seen = []
    monkeypatch.setattr(deploy.subprocess, "run", lambda command, **kw: seen.append(command)
                        or SimpleNamespace(returncode=0, stdout="{}", stderr=""))
    ws = deploy.Workspace("p", "prod")
    ws.cli("current-user", "me")
    ws.bundle("deploy")
    assert seen == [["databricks", "current-user", "me", "--profile", "p", "-o", "json"],
                    ["databricks", "bundle", "deploy", "-t", "prod", "--profile", "p"]]


def test_first_install_checks_sources_refreshes_then_grants_per_table_before_the_app(run):
    ws, saved = run("--catalog", "cat", "--warehouse-id", "wh1", installed=False)
    assert bundle_steps(ws) == [("validate", "--strict"), ("deploy",), ("run", "daily_refresh"),
                                ("run", "brickpulse")]
    assert saved == {"catalog": "cat", "warehouse_id": "wh1", "disabled_rules": "none"}
    grants = [s for s in ws.statements if s.startswith("GRANT")]
    assert "GRANT SELECT ON TABLE cat.brickpulse.finops_recommendations TO `sp-1`" in grants
    assert not any("SELECT ON SCHEMA" in s for s in grants)
    assert ws.statements.index("CREATE SCHEMA IF NOT EXISTS cat.brickpulse") < ws.statements.index(grants[0])


def test_update_only_deploys_and_restarts_the_app(run, monkeypatch, tmp_path):
    (tmp_path / "prod").mkdir()
    (tmp_path / "prod" / "overrides.json").write_text(
        json.dumps({"catalog": "cat", "warehouse_id": "wh1", "disabled_rules": "none"}))
    ws, _ = run()
    assert bundle_steps(ws) == [("validate", "--strict"), ("deploy",), ("run", "brickpulse")]
    assert not [s for s in ws.statements if s.startswith(("SELECT", "GRANT"))]


def test_refresh_reruns_the_pipeline_and_grants_new_tables(run, tmp_path):
    (tmp_path / "prod").mkdir()
    (tmp_path / "prod" / "overrides.json").write_text(
        json.dumps({"catalog": "cat", "warehouse_id": "wh1", "disabled_rules": "none"}))
    ws, _ = run("--refresh")
    assert ("run", "daily_refresh") in bundle_steps(ws)
    assert any(s.startswith("GRANT SELECT ON TABLE cat.brickpulse.") for s in ws.statements)


def test_unreadable_optional_table_switches_off_only_its_rules(run):
    by_table = deploy.rule_tables()
    ws, saved = run("--catalog", "cat", "--warehouse-id", "wh1", unreadable={"alert.alerts"})
    assert saved["disabled_rules"] == ",".join(sorted(by_table["alert.alerts"]))


def test_unreadable_required_table_stops_with_grants(monkeypatch, tmp_path):
    monkeypatch.setattr(deploy, "overrides_path", lambda target: tmp_path / "o.json")
    monkeypatch.setattr(deploy, "Workspace", lambda p, t: FakeWorkspace(p, t, unreadable={"billing.usage"}))
    with pytest.raises(deploy.DeployError, match="GRANT SELECT ON TABLE system.billing.usage"):
        deploy.main(["--profile", "p", "--target", "prod", "--catalog", "cat", "--warehouse-id", "wh1"])


def test_first_deploy_requires_catalog_and_warehouse(monkeypatch, tmp_path):
    monkeypatch.setattr(deploy, "overrides_path", lambda target: tmp_path / "o.json")
    monkeypatch.setattr(deploy, "Workspace", FakeWorkspace)
    with pytest.raises(deploy.DeployError, match="--catalog"):
        deploy.main(["--profile", "p", "--target", "prod"])
    with pytest.raises(deploy.DeployError, match="identifier"):
        deploy.main(["--profile", "p", "--target", "prod", "--catalog", "x;DROP", "--warehouse-id", "w"])


def test_disabled_rules_switch_rules_off_in_app_and_pipeline(monkeypatch):
    rule = deploy.rule_tables()["alert.alerts"][0]  # an enabled rule
    assert apply_disabled({rule: {"x": 1}}, f" {rule} ,")[rule] == {"x": 1, "enabled": False}
    assert apply_disabled({rule: {"x": 1}}, "none") == {rule: {"x": 1}}
    monkeypatch.setenv("BRICKPULSE_DISABLED_RULES", rule)
    assert load_thresholds()[rule]["enabled"] is False
    monkeypatch.delenv("BRICKPULSE_DISABLED_RULES")
    assert load_thresholds()[rule].get("enabled") is not False


def test_bind_existing_adopts_only_resources_the_plan_would_create():
    ws = FakeWorkspace("p", "prod")
    names = {"apps": "brickpulse", "pipelines": "[BrickPulse] Precompute (cat.brickpulse)",
             "jobs": "[BrickPulse] Daily refresh (cat.brickpulse)"}
    plan = {"resources.apps.brickpulse": {"action": "update"},
            "resources.pipelines.precompute": {"action": "create"},
            "resources.jobs.daily_refresh": {"action": "create"}}
    answers = {("pipelines", "list-pipelines"): [{"name": names["pipelines"], "pipeline_id": "pl-1"}],
               ("jobs", "list"): [{"job_id": 42, "settings": {"name": names["jobs"]}}]}
    ws.cli = lambda *args: answers.get(args[:2], {})
    ws.bundle = lambda *args, **kw: ws.calls.append(args) or SimpleNamespace(stdout=json.dumps({"plan": plan}))
    config = {"resources": {"apps": {"brickpulse": {"name": names["apps"]}},
                            "pipelines": {"precompute": {"name": names["pipelines"]}},
                            "jobs": {"daily_refresh": {"name": names["jobs"]}}}}
    deploy.bind_existing(ws, config)
    binds = [args[2:4] for args in ws.calls if args[:2] == ("deployment", "bind")]
    assert binds == [("precompute", "pl-1"), ("daily_refresh", "42")]


def test_optional_tabs_are_saved_for_later_deploys(run, tmp_path):
    (tmp_path / "prod").mkdir()
    (tmp_path / "prod" / "overrides.json").write_text(
        json.dumps({"catalog": "cat", "warehouse_id": "wh1", "disabled_rules": "none"}))
    _, saved = run("--apps-tab", "--genie-costs-tab")
    assert saved["apps_tab"] == saved["genie_costs_tab"] == "true"
    _, saved = run("--no-apps-tab")
    assert saved["apps_tab"] == "false" and saved["genie_costs_tab"] == "true"
