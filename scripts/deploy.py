"""Install or update BrickPulse in any workspace through its Declarative Automation Bundle.

    python3 scripts/deploy.py --profile PROFILE --target prod --catalog CATALOG --warehouse-id ID

The first run checks which system tables you can read, creates the schema, deploys the
bundle, runs the pipeline and tracker once, grants the app's service principal read on
the results and starts the app. Later runs only deploy and restart the app; add
--refresh when a release changes rules or the pipeline, so the data catches up.

Every Databricks call names --profile and -t explicitly. SQL runs on the given warehouse
as you, the same identity the pipeline runs as. Nothing is granted on a whole schema.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SYSTEM_CATALOG = "system"
# Read by the pipeline itself, not by one rule (spend, Genie, Apps, anomaly sinks).
REQUIRED = {
    "billing.usage", "billing.list_prices", "access.audit",
    "access.workspaces_latest", "compute.clusters", "compute.warehouses",
    "lakeflow.jobs", "lakeflow.pipelines",
}
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class DeployError(RuntimeError):
    pass


class Workspace:
    """Databricks CLI calls pinned to one profile and one bundle target."""

    def __init__(self, profile: str, target: str):
        self.profile, self.target = profile, target
        # The artifact build runs `python3`: resolve it to this interpreter's environment.
        self.env = {**os.environ, "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}"}

    def cli(self, *args: str, timeout: int = 300) -> dict:
        command = ["databricks", *args, "--profile", self.profile, "-o", "json"]
        result = self._run(command, timeout=timeout, capture=True)
        try:
            return json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise DeployError(f"`{' '.join(args[:3])}` did not return JSON.") from exc

    def bundle(self, *args: str, capture: bool = False, timeout: int = 7200):
        command = ["databricks", "bundle", *args, "-t", self.target, "--profile", self.profile]
        return self._run(command, timeout=timeout, capture=capture)

    def _run(self, command: list[str], *, timeout: int, capture: bool):
        try:
            result = subprocess.run(command, capture_output=capture, text=True, timeout=timeout,
                                    check=False, cwd=ROOT, env=self.env)
        except FileNotFoundError as exc:
            raise DeployError("Install the Databricks CLI (v0.250 or later) first.") from exc
        except subprocess.TimeoutExpired as exc:
            raise DeployError(f"Timed out: {' '.join(command[1:4])}") from exc
        if result.returncode:
            detail = ((result.stderr or result.stdout or "") if capture else "").strip()
            raise DeployError(f"`{' '.join(command[1:4])}` failed. {detail[:1600]}".strip())
        return result

    def exists(self, *args: str) -> bool:
        try:
            self.cli(*args)
            return True
        except DeployError as exc:
            if re.search(r"does not exist|not found|RESOURCE_DOES_NOT_EXIST|NOT_FOUND", str(exc), re.I):
                return False
            raise

    def sql(self, warehouse_id: str, statement: str) -> str | None:
        """Run one statement; the error message, or None when it succeeded."""
        body = {"warehouse_id": warehouse_id, "statement": statement, "wait_timeout": "50s",
                "on_wait_timeout": "CONTINUE", "disposition": "INLINE"}
        response = self.cli("api", "post", "/api/2.0/sql/statements", "--json", json.dumps(body))
        deadline = time.monotonic() + 1800
        while (response.get("status") or {}).get("state") in ("PENDING", "RUNNING"):
            if time.monotonic() > deadline:
                raise DeployError(f"Statement timed out: {statement[:120]}")
            time.sleep(3)
            response = self.cli("api", "get", f"/api/2.0/sql/statements/{response['statement_id']}")
        status = response.get("status") or {}
        if status.get("state") == "SUCCEEDED":
            return None
        return ((status.get("error") or {}).get("message") or status.get("state") or "failed").splitlines()[0]


# --- configuration --------------------------------------------------------------

def overrides_path(target: str) -> Path:
    return ROOT / ".databricks" / "bundle" / target / "variable-overrides.json"


def settle_variables(args, saved: dict) -> dict:
    """This run's variables: flags win over the values saved by the previous run."""
    flags = {"catalog": args.catalog, "warehouse_id": args.warehouse_id, "schema": args.schema,
             "pricing_mode": args.pricing_mode,
             "lookback_days": None if args.lookback_days is None else str(args.lookback_days),
             "apps_tab": None if args.apps_tab is None else str(args.apps_tab).lower(),
             "genie_costs_tab": None if args.genie_costs_tab is None else str(args.genie_costs_tab).lower()}
    values = {**saved, **{k: v for k, v in flags.items() if v is not None}}
    for name in ("catalog", "warehouse_id"):
        if not values.get(name):
            raise DeployError(f"--{name.replace('_', '-')} is required on the first deploy to this target.")
    for name in ("catalog", "schema"):
        if name in values and not IDENTIFIER.fullmatch(values[name]):
            raise DeployError(f"{name} must be a plain SQL identifier, got {values[name]!r}.")
    if values.get("pricing_mode", "auto") not in ("auto", "list", "account"):
        raise DeployError("Pricing mode must be auto, list or account.")
    if not 1 <= int(values.get("lookback_days", "30")) <= 90:
        raise DeployError("The lookback must be between 1 and 90 days.")
    return values


def namespace(bundle: dict) -> str:
    """catalog.schema as the bundle resolved it (flags, saved values, then target defaults)."""
    variables = bundle["variables"]
    return f"{variables['catalog']['value']}.{variables['schema']['value']}"


# --- preflight ------------------------------------------------------------------

def rule_tables() -> dict[str, list[str]]:
    """System table (schema.table) -> the enabled rules that read it."""
    from brickpulse.rules import RuleContext, get_enabled_rules
    from brickpulse.rules import _base_thresholds
    from brickpulse.rules.sources import rule_dependencies

    thresholds = _base_thresholds()
    tables: dict[str, list[str]] = {}
    for rule in get_enabled_rules(thresholds):
        for table in rule_dependencies(rule, RuleContext(thresholds=thresholds.get(rule.id, {}))):
            tables.setdefault(table, []).append(rule.id)
    return tables


def source_tables(pricing_mode: str) -> tuple[set[str], set[str]]:
    """(required, all) system tables this configuration reads."""
    required = set(REQUIRED) | ({"billing.account_prices"} if pricing_mode == "account" else set())
    optional = {"billing.cloud_infra_cost"} | ({"billing.account_prices"} if pricing_mode != "list" else set())
    return required, required | optional | set(rule_tables())


def grant_hint(tables, user: str) -> str:
    lines = ["-- run as a metastore admin (enable the system schema first if it isn't):"]
    lines += [f"GRANT USE SCHEMA ON SCHEMA {SYSTEM_CATALOG}.{s} TO `{user}`;"
              for s in sorted({t.split(".")[0] for t in tables})]
    lines += [f"GRANT SELECT ON TABLE {SYSTEM_CATALOG}.{t} TO `{user}`;" for t in sorted(tables)]
    return "\n".join(lines)


def check_sources(ws: Workspace, warehouse_id: str, pricing_mode: str, user: str,
                  stop_on_missing: bool) -> tuple[list[str], list[str]]:
    """(rules to switch off, readable tables). Stops if a required table is unreadable."""
    required, tables = source_tables(pricing_mode)
    print(f"Checking read access to {len(tables)} system tables...", flush=True)
    with ThreadPoolExecutor(max_workers=6) as pool:
        errors = dict(zip(sorted(tables), pool.map(
            lambda t: ws.sql(warehouse_id, f"SELECT * FROM {SYSTEM_CATALOG}.{t} LIMIT 0"), sorted(tables))))
    unreadable = {t: e for t, e in errors.items() if e}
    for t in sorted(unreadable):
        print(f"  MISS {SYSTEM_CATALOG}.{t}  <- {unreadable[t][:160]}", flush=True)
    missing_required = sorted(set(unreadable) & required)
    if missing_required:
        raise DeployError("You can't read these required system tables: " + ", ".join(missing_required)
                          + "\n" + grant_hint(missing_required, user))
    by_rule = rule_tables()
    disabled = sorted({rid for t in unreadable for rid in by_rule.get(t, [])})
    if disabled and stop_on_missing:
        raise DeployError(f"{len(disabled)} rule(s) need system tables you can't read: "
                          + ", ".join(disabled) + "\n" + grant_hint(unreadable, user))
    if disabled:
        print(f"{len(disabled)} rule(s) switched off until their tables are readable: {', '.join(disabled)}")
    else:
        print("All system tables readable; every rule stays on.", flush=True)
    return disabled, sorted(t for t in tables if t not in unreadable)


# --- bundle steps -----------------------------------------------------------------

def bundle_config(ws: Workspace) -> dict:
    return json.loads(ws.bundle("validate", "-o", "json", capture=True).stdout)


def bind_existing(ws: Workspace, config: dict) -> None:
    """Attach resources installed before the bundle (same names) instead of duplicating them.

    Only resources the plan would create are bound; the rest are already in the bundle's state."""
    resources = config["resources"]
    plan = json.loads(ws.bundle("plan", "-o", "json", capture=True).stdout).get("plan") or {}
    app = resources["apps"]["brickpulse"]["name"]
    pipeline = resources["pipelines"]["precompute"]["name"]
    job = resources["jobs"]["daily_refresh"]["name"]
    found = {
        "brickpulse": app if ws.exists("apps", "get", app) else None,
        "precompute": next((p["pipeline_id"] for p in ws.cli(
            "pipelines", "list-pipelines", "--filter", f"name LIKE '{pipeline}'") or []
            if p.get("name") == pipeline), None),
        "daily_refresh": next((str(j["job_id"]) for j in ws.cli("jobs", "list", "--name", job) or []
                               if (j.get("settings") or {}).get("name") == job), None),
    }
    kinds = {"brickpulse": "apps", "precompute": "pipelines", "daily_refresh": "jobs"}
    for key, resource_id in found.items():
        if (plan.get(f"resources.{kinds[key]}.{key}") or {}).get("action") != "create":
            print(f"{key}: already managed by this bundle")
        elif resource_id:
            print(f"Binding {key} to existing {kinds[key][:-1]} {resource_id}", flush=True)
            ws.bundle("deployment", "bind", key, resource_id, "--auto-approve", capture=True)
        else:
            print(f"{key}: none installed yet; the deploy creates it")


def grant_app_reads(ws: Workspace, values: dict, ns: str, app_name: str,
                    system_tables: list[str] | None) -> None:
    """Per-table SELECT for the app's service principal; never a schema-wide grant."""
    sp = ws.cli("apps", "get", app_name).get("service_principal_client_id")
    if not sp:
        raise DeployError(f"Could not read the service principal of app {app_name!r}.")
    catalog, schema = ns.split(".")
    tables = [t["name"] for t in ws.cli("tables", "list", catalog, schema) or []
              if t.get("table_type") != "VOLUME"]
    statements = [f"GRANT USE CATALOG ON CATALOG {catalog} TO `{sp}`",
                  f"GRANT USE SCHEMA ON SCHEMA {ns} TO `{sp}`"]
    statements += [f"GRANT SELECT ON TABLE {ns}.{t} TO `{sp}`" for t in tables]
    failed = [s for s in statements if ws.sql(values["warehouse_id"], s)]
    if failed:
        raise DeployError("App read grants incomplete; resolve before opening the app:\n" + "\n".join(failed))
    print(f"{ns}: app service principal can read {len(tables)} table(s)", flush=True)
    if system_tables is None:
        return
    # The data-health check and the About Serverless tab read system.* live.
    statements = [f"GRANT USE CATALOG ON CATALOG {SYSTEM_CATALOG} TO `{sp}`"]
    statements += [f"GRANT USE SCHEMA ON SCHEMA {SYSTEM_CATALOG}.{s} TO `{sp}`"
                   for s in sorted({t.split(".")[0] for t in system_tables})]
    statements += [f"GRANT SELECT ON TABLE {SYSTEM_CATALOG}.{t} TO `{sp}`" for t in system_tables]
    with ThreadPoolExecutor(max_workers=6) as pool:
        failed = [s for s, e in zip(statements, pool.map(lambda s: ws.sql(values["warehouse_id"], s),
                                                          statements)) if e]
    if failed:
        print("!! Could not grant the app read on system tables (needs a metastore admin). The app works")
        print("   without it, but Data health and About Serverless show sources as unavailable. Ask an")
        print("   admin to run:\n" + "\n".join(f"   {s};" for s in failed))
    else:
        print("App service principal can read the system tables it checks.", flush=True)


def verify(ws: Workspace, app_name: str, disabled: list[str]) -> None:
    app = ws.cli("apps", "get", app_name)
    compute = (app.get("compute_status") or {}).get("state")
    status = (app.get("app_status") or {}).get("state")
    if compute != "ACTIVE" or status not in ("RUNNING", "ACTIVE"):
        raise DeployError(f"App deployed but not ready: compute={compute}, app={status}.")
    print(f"BrickPulse is running: {app.get('url')}")
    print(f"Rules off for unreadable sources: {', '.join(disabled) or 'none'}")


# --- main -------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", required=True, help="Databricks CLI profile of the target workspace")
    parser.add_argument("--target", required=True, choices=("dev", "prod"), help="bundle target")
    parser.add_argument("--catalog", help="existing catalog for the BrickPulse schema")
    parser.add_argument("--warehouse-id", help="existing SQL warehouse the app and the checks use")
    parser.add_argument("--schema", help="schema for BrickPulse tables (bundle default per target)")
    parser.add_argument("--pricing-mode", choices=("auto", "list", "account"))
    parser.add_argument("--lookback-days", type=int)
    parser.add_argument("--apps-tab", action=argparse.BooleanOptionalAction,
                        help="show the Apps inventory tab (kept for later deploys)")
    parser.add_argument("--genie-costs-tab", action=argparse.BooleanOptionalAction,
                        help="show the Genie Costs tab (kept for later deploys)")
    parser.add_argument("--check-sources", action="store_true",
                        help="re-check system-table access (always done on the first deploy)")
    parser.add_argument("--stop-on-missing", action="store_true",
                        help="stop instead of switching off rules whose tables are unreadable")
    parser.add_argument("--refresh", action="store_true",
                        help="run the pipeline and tracker now (releases that change rules or the pipeline)")
    parser.add_argument("--bind-existing", action="store_true",
                        help="adopt an app, pipeline and job installed before the bundle")
    parser.add_argument("--dry-run", action="store_true", help="checks and bundle validation only")
    args = parser.parse_args(argv)

    if not (ROOT / "src/brickpulse/server/static/index.html").is_file():
        raise DeployError("The frontend isn't built: run `npm run build` in frontend/ first.")
    ws = Workspace(args.profile, args.target)
    path = overrides_path(args.target)
    saved = json.loads(path.read_text()) if path.is_file() else {}
    values = settle_variables(args, saved)

    user = ws.cli("current-user", "me").get("userName")
    if not user or "`" in user:
        raise DeployError("Could not resolve the deploying user.")
    if not ws.exists("catalogs", "get", values["catalog"]):
        raise DeployError(f"Catalog {values['catalog']!r} not found or not visible to {user}.")
    warehouse = ws.cli("warehouses", "get", values["warehouse_id"])
    ws.cli("apps", "list", "--page-size", "1")
    print(f"Deploying as {user} to target {args.target}; warehouse {warehouse.get('name')}", flush=True)

    system_tables = None
    if args.check_sources or "disabled_rules" not in saved:
        disabled, system_tables = check_sources(ws, values["warehouse_id"],
                                                values.get("pricing_mode", "auto"), user, args.stop_on_missing)
        values["disabled_rules"] = ",".join(disabled) or "none"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values, indent=2) + "\n")

    config = bundle_config(ws)
    ns = namespace(config)
    app_name = config["resources"]["apps"]["brickpulse"]["name"]
    ws.bundle("validate", "--strict")
    if args.dry_run:
        print("Checks passed and the bundle is valid; nothing was deployed.")
        return 0

    error = ws.sql(values["warehouse_id"], f"CREATE SCHEMA IF NOT EXISTS {ns}")
    if error:
        raise DeployError(f"Could not create schema {ns}: {error}")
    if args.bind_existing:
        bind_existing(ws, config)
    ws.bundle("deploy")
    first = not ws.exists("tables", "get", f"{ns}.finops_recommendations")
    if first or args.refresh:
        print("Running the pipeline and tracker (the first run reads 30 days of system tables)...", flush=True)
        ws.bundle("run", "daily_refresh")
    # A refresh can add tables; a source check can change which system tables are readable.
    if first or args.refresh or system_tables is not None:
        grant_app_reads(ws, values, ns, app_name, system_tables)
    ws.bundle("run", "brickpulse")
    verify(ws, app_name, [r for r in values["disabled_rules"].split(",") if r not in ("", "none")])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DeployError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
