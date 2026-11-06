# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-11-06

### Added

- Genie costs: a USD/DBU toggle (kept in the URL as `?unit=dbu`) that switches the headline figure, the
  daily chart and the surface, space, workspace and user tables. The free tier has no price, so its
  columns always stay in DBU.
- Genie costs: the top cards now read billed spend, the free tier's USD equivalent and the total value of
  usage. The equivalent is an estimate (free DBUs at the average paid rate of the same product and
  channel) and is never added to billed spend.
- Tables that can run long now page, filter and sort in the browser: the Genie tables, Workspaces, Apps,
  Cost anomalies, Rules, and the Tagging and Labs findings. Click a header to sort, type to filter.
  Opportunities keeps its own server-side paging, and short evidence tables are unchanged.
- Genie costs: users, spaces, workspaces and products each show billed USD, the free tier's USD equivalent
  and the total value (or free, paid and total DBU), so every table reads like the cards above it.
- Genie costs: a one-line description under every panel saying what it shows, and a warning when the
  figures come from fewer than 30 days and are extrapolated to a month.

### Changed

- Genie costs: run the pipeline once after upgrading. The user and space queries gained columns, and until
  the next run older snapshot rows value the free tier at the account-wide rate (users, workspaces) or show
  no equivalent (spaces).
- Genie costs: the user and space tables are no longer cut at 25 rows. They carry up to 1,000 rows ranked by
  spend and say when there are more.
- Genie costs: "Observed spend" is now "Billed spend", since the free tier bills at $0 and it never
  included free usage.
- Genie costs: the surface table now uses the same normalized 30-day month as the other tables, shows the
  product's display name instead of the raw `GENIE_CODE` code, and has a free/paid tier column.

## [0.2.0] - 2026-11-04

### Added

- Labs rule `lakebase_scale_to_zero`: Lakebase endpoints whose always-on floor bills through hours with no
  client connected, priced per 10-minute slot at each region's own rate. Needs the Lakebase Observability
  system tables (Beta, 7-day retention), enabled from the workspace Previews page.
- Labs rule `serverless_untagged_spend`: Apps, serverless pipelines, Lakebase, Vector Search and Model
  Serving spend missing the required ownership tags. It reuses the `untagged_spend` tag aliases and never
  covers the same usage.

### Changed

- Times of events (last refresh, data-health check, snapshot) show in the browser's time zone with
  its abbreviation. Billing days, hour-of-day heatmaps and evidence series stay in UTC, the zone system
  tables aggregate in.
- Updated the frontend (Playwright, Prettier, TanStack Query, Radix UI, JetBrains Mono) and Python runtime
  dependencies (FastAPI, Uvicorn, Pydantic, Databricks SDK and SQL connector, python-dotenv, ruff).
- CI tests the pinned runtime versions in `requirements.txt`.

### Removed

- Dependabot version updates.

## [0.1.0] - 2026-10-23

### Added

- First public version of BrickPulse, a read-only FinOps app for Databricks.
- 42 cost rules across clusters, SQL warehouses, jobs, pipelines, model serving, AI inference, storage and
  governance, with evidence, savings ranges and step-by-step guidance.
- Spend overview by workspace, day and SKU; next-best-action ranking with grouping of overlapping options.
- Cost anomalies, tag allocation, serverless readiness and savings tracking from observed changes.
- Optional contract-price estimates from `system.billing.account_prices` (`pricing_mode`).
- Optional Apps inventory, Genie costs and Ask Genie surfaces (off by default).
- UI in English, Spanish and Brazilian Portuguese.
- Installation as a Declarative Automation Bundle through `scripts/deploy.py`, with source checks,
  per-table grants and `dev`/`prod` targets.
- Apache-2.0 license, NOTICE, contributing guide, Code of Conduct and security policy.
- User guides (getting started, deployment, configuration, security, troubleshooting) in English, Spanish
  and Brazilian Portuguese, plus translated READMEs; architecture, testing and design-system references.
- Continuous integration (lint, Python tests on 3.10 and 3.13, frontend type check and build, committed
  build check, Playwright smoke tests), Dependabot, and issue and pull request templates.

[Unreleased]: https://github.com/pedrotramos/BrickPulse/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/pedrotramos/BrickPulse/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/pedrotramos/BrickPulse/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/pedrotramos/BrickPulse/releases/tag/v0.1.0
