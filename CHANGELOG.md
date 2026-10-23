# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/pedrotramos/BrickPulse/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/pedrotramos/BrickPulse/releases/tag/v0.1.0
