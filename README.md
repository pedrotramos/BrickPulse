<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/brand/brickpulse-lockup-color-dark.svg">
    <img src="docs/brand/brickpulse-lockup-color.svg" alt="BrickPulse" width="320">
  </picture>
</p>

<p align="center">
  <strong>Evidence-led, read-only FinOps for Databricks.</strong><br>
  From account-level spend down to the evidence behind every savings opportunity.
</p>

<p align="center">
  <a href="https://github.com/pedrotramos/BrickPulse/actions/workflows/ci.yml"><img src="https://github.com/pedrotramos/BrickPulse/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue.svg" alt="License: Apache-2.0"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-3776ab.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/node-20.19%2B-339933.svg" alt="Node 20.19+">
</p>

<p align="center">
  <b>English</b> · <a href="README.es.md">Español</a> · <a href="README.pt-BR.md">Português</a>
</p>

---

BrickPulse is a [Databricks App](https://docs.databricks.com/dev-tools/databricks-apps/) that reads your
account's system tables, finds where money is being wasted, and explains each finding with the evidence,
the effort involved and a savings range. It is **advisory and read-only**: it never changes the resources
it analyzes.

<p align="center">
  <img src="docs/assets/screenshot-overview.png" alt="BrickPulse overview: observed spend, estimated opportunity and tracked savings" width="900">
</p>

<details>
<summary>More screenshots</summary>

![Next best actions ranked by estimated savings, effort and priority](docs/assets/screenshot-next-best-actions.png)

</details>

## What it does

- **Spend overview.** Observed spend by workspace, day and SKU, for the workspaces in the metastore region.
- **42 cost rules.** Rules cover clusters, SQL warehouses, jobs, pipelines, model serving, AI inference,
  storage and governance. Each finding comes with evidence, an estimate range, step-by-step guidance and a
  link to the resource in Databricks.
- **Next best actions.** Findings are ranked by estimated savings, effort and priority. When several
  options overlap on the same resource, they are grouped instead of summed.
- **Analysis.** Cost anomalies, tag-based allocation and serverless readiness.
- **Savings tracking.** Savings are estimated from the changes BrickPulse observes after a
  recommendation. These are estimates, not invoice reconciliation.
- **Contract pricing (optional).** Extra estimates from `system.billing.account_prices` reflect negotiated
  discounts. List price stays the comparable baseline.
- **Optional surfaces.** An Apps inventory, Genie costs and an *Ask Genie* panel. All three are off by default.
- **Three languages.** The UI is available in English, Spanish and Brazilian Portuguese. It detects the
  browser language, remembers an explicit choice and falls back to English. Amounts are always USD.

## Design principles

- **Every number explains itself.** Each figure shows its period, source, confidence and freshness.
- **Unknown is not zero.** Missing data is never shown as zero spend or "no opportunities".
- **Language never changes the math.** Scoring, ranking and amounts don't depend on the UI language.
- **Recommend, never act.** BrickPulse has no write path to the resources it analyzes.
- **Least privilege.** The app's service principal is granted `SELECT` table by table, never on a whole
  schema. At first, only the person who installed it can open the app.

## Quick start (local demo)

You can try the full UI on synthetic data, with no Databricks workspace. You need Python 3.10+, Node 20.19+
and npm.

```sh
git clone https://github.com/pedrotramos/BrickPulse.git && cd BrickPulse
python3 -m venv .venv && .venv/bin/python -m pip install -e '.[dev]'
npm --prefix frontend ci
npm run dev        # then open http://127.0.0.1:5173
```

Demo data is clearly labeled in the UI and never touches a real workspace. See the
[getting started guide](docs/en/getting-started.md) for details.

## Deploy to your workspace

BrickPulse ships as a [Declarative Automation Bundle](https://docs.databricks.com/dev-tools/bundles/). It
contains the app, a serverless pipeline and a daily refresh job. One command checks access, deploys
everything, runs the first refresh and starts the app:

```sh
.venv/bin/python scripts/deploy.py --profile <PROFILE> --target prod \
  --catalog <CATALOG> --warehouse-id <WAREHOUSE_ID>
```

Read the [deployment guide](docs/en/deployment.md) for prerequisites, the system-table grants, upgrades
and uninstalling.

## Documentation

| Guide | |
| --- | --- |
| [Getting started](docs/en/getting-started.md) | Local setup, demo mode, running the tests |
| [Deployment](docs/en/deployment.md) | Installing in a workspace, upgrades, `dev` vs `prod`, uninstall |
| [Configuration](docs/en/configuration.md) | Pricing mode, optional tabs, environment variables, rule thresholds |
| [Security](docs/en/security.md) | Identities, user scoping, granting access to more people |
| [Troubleshooting](docs/en/troubleshooting.md) | Missing grants, empty data, switched-off rules |

Contributor references (English only): [Architecture](docs/ARCHITECTURE.md),
[Data model](docs/DATA_MODEL.md), [Savings confidence model](docs/SAVINGS_CONFIDENCE_MODEL.md),
[Adding a rule](docs/ADDING_A_RULE.md), [Testing](docs/TESTING.md), [Design system](docs/DESIGN.md).
The full index is in [docs/](docs/README.md).

## Project structure

```text
frontend/src/        React app: app shell, features, components, lib, locales (en, es, pt-BR)
src/brickpulse/
  server/            FastAPI: API, identity, data sources, services, i18n, compiled UI (static/)
  rules/             42 rule plugins, Jinja SQL templates and thresholds
  pipelines/         Lakeflow precompute pipeline, change tracker and DDL
resources/           Bundle resources: app, pipeline, daily job (see databricks.yml)
scripts/             Dev server, deploy, wheel build, regression baseline
tests/               Python test suite and the frozen financial baseline
docs/                User guides (en, es, pt-BR) and contributor references
```

## Contributing

Contributions are welcome, whether they're new rules, bug fixes, translations or docs. Read
[CONTRIBUTING.md](CONTRIBUTING.md) to get started, and please follow the
[Code of Conduct](CODE_OF_CONDUCT.md). Report security issues privately, as described in
[SECURITY.md](SECURITY.md).

## License

[Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for third-party attributions.

BrickPulse is an independent open-source project. It is not affiliated with, endorsed by or sponsored by
Databricks, Inc. "Databricks" is a trademark of Databricks, Inc.
