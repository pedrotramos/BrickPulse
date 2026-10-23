# Deployment

**English** · [Español](../es/deployment.md) · [Português](../pt-BR/deployment.md)

BrickPulse installs into one Databricks workspace as a
[Declarative Automation Bundle](https://docs.databricks.com/dev-tools/bundles/). `databricks.yml` and
`resources/` declare three resources:

| Resource | What it is |
| --- | --- |
| `brickpulse` app | The Databricks App (FastAPI + compiled React UI) |
| `precompute` pipeline | A serverless Lakeflow pipeline that evaluates the rules and writes the `finops_*` tables |
| `daily_refresh` job | Runs the pipeline, then the savings tracker, every day at 05:00 UTC |

No workspace value is committed, and there is no default target. You always choose the workspace with
`--profile` and the target (`dev` or `prod`) explicitly.

## Prerequisites

**On your machine**

- The local setup from [getting started](getting-started.md), including the `.venv` with the `build` package.
- The [Databricks CLI](https://docs.databricks.com/dev-tools/cli/install.html), authenticated to the
  target workspace through a profile (`databricks auth login --host <workspace-url> --profile <PROFILE>`).
- A built frontend. `src/brickpulse/server/static` is committed, so you only need `npm run build` after
  changing the frontend.

**In the workspace**

- A Unity Catalog **catalog** where you can create a schema. BrickPulse creates `brickpulse` (or
  `brickpulse_dev` for the `dev` target).
- A **SQL warehouse** where you have `CAN_USE`. The deploy checks and the app both query through it.
- Permission to create Databricks Apps, pipelines and jobs.
- **Read access to the system tables** below, granted to you, the deploying user. The pipeline runs as you.

### System tables

Required. The deploy stops if any of these is unreadable:

```text
system.billing.usage            system.billing.list_prices
system.access.audit             system.access.workspaces_latest
system.compute.clusters         system.compute.warehouses
system.lakeflow.jobs            system.lakeflow.pipelines
```

Optional. If one of these is unreadable, the deploy switches off only the rules that depend on it:

```text
system.access.table_lineage                 system.ai_gateway.usage
system.alert.alerts                         system.alert.alert_evaluation_history
system.billing.account_prices               system.billing.cloud_infra_cost
system.compute.instance_events              system.compute.instance_pools
system.compute.node_timeline                system.compute.node_types
system.compute.warehouse_events             system.lakeflow.job_run_timeline
system.lakeflow.job_task_run_timeline       system.lakeflow.job_tasks
system.lakeflow.pipeline_update_timeline    system.query.history
system.serving.endpoint_usage               system.serving.served_entities
system.sharing.materialization_history      system.storage.predictive_optimization_operations_history
```

`system.billing.account_prices` becomes required with `--pricing-mode account`. If you lack access, the
deploy prints the exact `GRANT` statements a metastore admin needs to run, for example:

```sql
GRANT USE CATALOG ON CATALOG system TO `you@example.com`;
GRANT USE SCHEMA ON SCHEMA system.billing TO `you@example.com`;
GRANT SELECT ON TABLE system.billing.usage TO `you@example.com`;
```

> [!NOTE]
> System tables only cover the workspaces in the **metastore's region**. BrickPulse reports the account
> spend it can see in that region; it is not a cross-region account view.

## First install

```sh
.venv/bin/python scripts/deploy.py --profile <PROFILE> --target prod \
  --catalog <CATALOG> --warehouse-id <WAREHOUSE_ID>
```

The script runs these steps:

1. Checks your user, the catalog and the warehouse.
2. Tests read access to every system table it uses. If a required table is missing, it stops and prints
   the `GRANT`s you need. If an optional table is missing, it switches off the rules that depend on it.
   Pass `--stop-on-missing` to stop in that case too.
3. Writes the chosen values to `.databricks/bundle/<target>/variable-overrides.json`. That folder is
   outside git, and later runs reuse the values.
4. Creates the schema.
5. Runs `databricks bundle deploy`.
6. Runs the pipeline and the tracker once. The first run reads 30 days of system tables.
7. Grants the app's service principal `SELECT` table by table, never on the whole schema. It also tries to
   grant read on the system tables the app queries live. That needs a metastore admin. If it fails, the app
   still works, and the script prints the commands for an admin to run.
8. Starts the app and prints its URL.

At first, only you can open the app (`CAN_MANAGE`), plus workspace admins who inherit access. See
[security](security.md) to give access to more people.

Run with `--dry-run` first to do only the checks and bundle validation.

## Upgrading

Deploying a new version doesn't run any job:

```sh
git pull
databricks bundle deploy -t prod --profile <PROFILE>
databricks bundle run brickpulse -t prod --profile <PROFILE>
```

If the release changes rules, SQL or the pipeline, the data only reflects the change after a refresh.
Run the refresh job now, or wait for the next daily run:

```sh
databricks bundle run daily_refresh -t prod --profile <PROFILE>
```

`scripts/deploy.py --profile <PROFILE> --target prod --refresh` deploys, refreshes and re-applies the
grants on any new tables in one step.

## Deploy script options

| Flag | Effect |
| --- | --- |
| `--profile` (required) | Databricks CLI profile of the target workspace |
| `--target` (required) | `dev` or `prod` |
| `--catalog`, `--warehouse-id` | Required on the first deploy to a target, then remembered |
| `--schema` | Overrides the per-target default schema |
| `--pricing-mode auto\|list\|account` | Price source. See [configuration](configuration.md#pricing-mode) |
| `--lookback-days N` | Days of history each refresh reads (1–90, default 30) |
| `--apps-tab` / `--no-apps-tab` | Show or hide the Apps inventory tab (remembered) |
| `--genie-costs-tab` / `--no-genie-costs-tab` | Show or hide the Genie Costs tab (remembered) |
| `--check-sources` | Re-checks system-table access, for example after an admin grants new tables, and switches the matching rules back on |
| `--stop-on-missing` | Stops instead of switching off rules whose tables are unreadable |
| `--refresh` | Runs the pipeline and tracker now |
| `--bind-existing` | Adopts an app, pipeline and job installed before the bundle existed |
| `--dry-run` | Runs the checks and bundle validation only |

## `dev` and `prod` targets

The `dev` target uses bundle development mode. Resources get a name prefix, the schedule is paused, the
schema is `brickpulse_dev` and the app is `brickpulse-dev`. Both targets can live in the same workspace.
`prod` deploys under `/Workspace/Users/<you>/.bundle/brickpulse/prod`, which is private to the deploying
user and never `/Shared`.

## Installs from before the bundle

If you installed with the old `00_deploy_brickpulse` notebook, pass `--bind-existing`. It binds the
existing app, pipeline and job (same names) to the bundle instead of creating duplicates. Before the first
deploy, check that the plan contains only updates:

```sh
databricks bundle plan -t prod --profile <PROFILE>
```

## Uninstalling

```sh
databricks bundle destroy -t prod --profile <PROFILE>
```

This removes the app, the pipeline and the job. Removing the pipeline also removes the tables the pipeline
manages. The schema and the tracker's history tables (plain Delta) stay. Drop the schema yourself when you
no longer need the history:

```sql
DROP SCHEMA <CATALOG>.brickpulse CASCADE;
```

If something goes wrong, see [troubleshooting](troubleshooting.md).
