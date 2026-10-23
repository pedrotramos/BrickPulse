# Security and access

**English** · [Español](../es/security.md) · [Português](../pt-BR/security.md)

BrickPulse reads billing and usage data for a whole metastore region, so who can see what matters. This page
explains which identities exist, how access is scoped and how to grant access to more people. To report a
vulnerability, see [SECURITY.md](../../SECURITY.md).

## Two separate identities

The identity that **runs the SQL** is not the identity that **views the data**.

- **Queries** run as the app's **service principal** (`BRICKPULSE_AUTH_MODE=sp`). It needs `CAN_USE` on the
  warehouse and `SELECT` on the BrickPulse tables. The deploy grants `SELECT` table by table, never on a
  whole schema. Viewers don't inherit these grants.
- **Viewers** are identified from the headers set by the Databricks Apps authenticated proxy
  (`x-forwarded-email`, `x-forwarded-preferred-username`, `x-forwarded-user`). BrickPulse then looks the
  user up in an explicit scope map.

> [!CAUTION]
> These headers can only be trusted behind the Databricks Apps proxy. Never expose the backend directly to
> a network where clients can set arbitrary headers.

The `obo` mode only simulates a development persona. It does not make queries run with the viewer's own
grants, so never use it as production authorization.

## Who can open the app

A fresh install is locked down twice:

1. **App ACL.** Only the deploying user gets `CAN_MANAGE` on the app. Workspace admins keep the access they
   inherit.
2. **Scope map.** `BRICKPULSE_USER_SCOPE_JSON` lists only the deploying user, as `platform_admin`.

Access fails closed. A user who isn't in the scope map gets HTTP 403 on every API route, including
configuration, rules and data health. An empty or invalid map denies everyone. `/health` is a process
liveness probe only and reveals nothing about data or sources.

## Roles and workspace scope

| Role | Sees |
| --- | --- |
| `platform_admin` | Every workspace in the metastore region |
| `workspace_admin` | Only the workspaces in its `workspace_ids` list |

The server derives the scope from the viewer's identity. URL parameters and the workspace selector can
only **narrow** that set. A `workspace_admin` who asks for a workspace outside its list gets an empty
result, not extra access. Workspace ids used in queries are validated as numeric, and caches are keyed by
host, warehouse, table and scope.

This filtering happens in the app. It is **not** Unity Catalog row-level security.

## Granting access to more people

Adding people is a deliberate change with two parts. Do both, because each one on its own is not enough.

1. Add the users to the scope map. Edit the `BRICKPULSE_USER_SCOPE_JSON` value in
   `resources/brickpulse.app.yml`:

   ```json
   {
     "owner@example.com":    {"role": "platform_admin"},
     "operator@example.com": {"role": "workspace_admin", "workspace_ids": ["1234567890123456"]}
   }
   ```

2. Give the same users `CAN_USE` on the app. Add them under `permissions` in the same file, or use the app's
   **Permissions** page in the workspace.

Then redeploy:

```sh
databricks bundle deploy -t prod --profile <PROFILE>
databricks bundle run brickpulse -t prod --profile <PROFILE>
```

## What the app can and cannot do

- It **reads** system tables and its own `finops_*` tables. It has no code path that changes clusters,
  warehouses, jobs or any other analyzed resource.
- Rule SQL interpolates only validated identifiers (catalog and table names). Values derived from users
  are bound as parameters or validated as numeric.
- The pipeline and the tracker run as the deploying user, through the bundle's job.

## Genie

*Ask Genie* is off by default (`BRICKPULSE_GENIE_ENABLED=false`). In `api` mode, the proxy needs the user's
forwarded token and the Genie scope that the platform authorizes. The panel shows which identity answered,
shows the generated SQL and results, and warns that answers are AI-generated. Before you turn it on, review
the Genie space's grants and the app's `user_api_scopes`.

## Configuration names

`BRICKPULSE_*` variables take precedence over the legacy `FINOPS_*` aliases. Development defaults are not a
safe production configuration. Use the `sp` configuration the bundle deploys.
