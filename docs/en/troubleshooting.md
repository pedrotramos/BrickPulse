# Troubleshooting

**English** · [Español](../es/troubleshooting.md) · [Português](../pt-BR/troubleshooting.md)

Start with the **Data health** page in the app. It shows the last pipeline run, how fresh each source is and
which sources are unavailable.

## The deploy stops with "You can't read these required system tables"

A required system table is unreadable for the deploying user. The script prints the `GRANT` statements a
**metastore admin** needs to run. If a system schema isn't enabled yet, the admin has to
[enable it](https://docs.databricks.com/admin/system-tables/) first. Re-run the deploy after the grants.

## Some rules are switched off

An optional system table was unreadable during the deploy, so the rules that depend on it were switched
off. The script lists them (`N rule(s) switched off until their tables are readable: …`). Once an admin grants access, run:

```sh
.venv/bin/python scripts/deploy.py --profile <PROFILE> --target prod --check-sources --refresh
```

## "Could not grant the app read on system tables"

The data-health check and the *Serverless readiness* tab read some system tables live, as the app's service
principal. Only a metastore admin can grant that access. The app works without it, but those surfaces show
the sources as unavailable. Ask an admin to run the statements the script printed.

## The app opens but shows no data

- The first refresh may still be running. Check the `[BrickPulse] Daily refresh` job in the workspace.
- A release that changed rules or the pipeline needs a refresh before the data reflects it:
  `databricks bundle run daily_refresh -t prod --profile <PROFILE>`.
- Missing data is shown as *unknown*, never as zero. If a page says a source is unavailable, check its
  grants rather than reading it as "no spend".

## A workspace is missing from the overview

System tables only cover the workspaces in the **metastore's region**. A workspace in another region needs
its own install against a metastore in that region.

## HTTP 403 for a colleague

That user isn't in `BRICKPULSE_USER_SCOPE_JSON`, or doesn't have `CAN_USE` on the app. Both are needed. See
[granting access to more people](security.md#granting-access-to-more-people).

## "App deployed but not ready"

The app compute or the app itself didn't reach `RUNNING`. Open the app in the workspace and check its
**Logs** tab. Then start it again:

```sh
databricks bundle run brickpulse -t prod --profile <PROFILE>
```

## "The frontend isn't built"

`src/brickpulse/server/static/index.html` is missing. Run `npm --prefix frontend ci && npm run build`, then
deploy again.

## Still stuck?

Open an [issue](https://github.com/pedrotramos/BrickPulse/issues/new/choose). Include the BrickPulse version,
your cloud, the deploy target, and the deploy output or app logs. Redact workspace URLs, emails and ids.
