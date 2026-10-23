# Security policy

## Supported versions

BrickPulse is pre-1.0. Security fixes go into the latest release only.

| Version | Supported |
| --- | --- |
| 0.1.x (latest) | ✅ |
| older | ❌ |

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report them privately through GitHub's
[private vulnerability reporting](https://github.com/pedrotramos/BrickPulse/security/advisories/new)
(*Security* tab → *Report a vulnerability*). If you can't use GitHub, email **pedrotramos97@gmail.com**
with `[BrickPulse security]` in the subject.

Please include:

- the BrickPulse version (or commit) and how it was deployed;
- what an attacker can do, and the steps to reproduce it;
- any proof of concept, with all real workspace URLs, emails, tokens and ids redacted.

What to expect:

- an acknowledgement within **5 business days**;
- an initial assessment within **10 business days**;
- a fix, or a mitigation plan, coordinated with you before any public disclosure. Reporters are credited
  in the advisory unless they prefer otherwise.

BrickPulse is maintained by volunteers. These are goals, not contractual SLAs.

## Scope

In scope, for example:

- bypassing the user scope map (`BRICKPULSE_USER_SCOPE_JSON`), or seeing workspaces outside your scope;
- SQL injection through rule templates, API parameters or configuration;
- exposure of tokens or credentials by the app, pipeline or deploy script;
- over-broad grants created by `scripts/deploy.py`;
- cross-site scripting or similar issues in the UI.

Out of scope:

- deployments that ignore the documented model, for example exposing the backend outside the Databricks
  Apps proxy, or running `demo`/`obo` mode in production (see [security](docs/en/security.md));
- vulnerabilities in Databricks itself. Report those to
  Databricks through its [Trust Center](https://www.databricks.com/trust).
- findings from automated scanners without a demonstrated impact.
