"""Regional workspace scoping — keep account-global billing aligned with the region.

``system.billing.usage`` is **account-global**: it carries a row for every
workspace in the account, in every region. Every OTHER system table the pipeline
reads — ``compute.clusters`` / ``compute.warehouses``, ``lakeflow.jobs`` and
``access.workspaces_latest`` — is scoped to THIS workspace's metastore, i.e. its
region. Left unfiltered, the spend / Genie / Apps surfaces therefore report
hundreds of workspaces the rules can never produce a recommendation for, and the
savings leaderboard overcounts the addressable estate (measured on one account:
billing spanned 10,340 workspaces vs. 9,010 the regional tables know about).

The fix is to filter billing to the UNION of workspace ids across those regional
tables — "every workspace this metastore has any data about". That is the same
set the rules operate on, so spend and recommendations line up.

Only the low-cardinality entity/latest tables are unioned. The per-minute timeline
tables (``compute.node_timeline``, ``lakeflow.job_run_timeline``) are excluded on
purpose: a timeline row's cluster/job must already exist in ``clusters`` / ``jobs``,
so their workspace ids are a strict subset — verified live, adding them changed
the union count by 0 — while scanning them would cost a full pass over hundreds of
millions of rows on every live query.
"""

from __future__ import annotations

#: Regional system tables whose workspace ids define "this region". Entity/latest
#: tables only — see the module docstring for why the timeline tables are omitted.
#: Every table here is metastore-scoped, so the union is implicitly regional.
_REGIONAL_TABLES = (
    "access.workspaces_latest",
    "compute.clusters",
    "compute.warehouses",
    "lakeflow.jobs",
)


def regional_workspaces_sql(catalog: str = "system") -> str:
    """A subquery selecting the workspace ids present in this metastore's region.

    Meant to be inlined into a ``workspace_id IN (...)`` filter on
    ``billing.usage``. Uses ``UNION`` (not ``UNION ALL``) so the ids are distinct.
    """
    return "\n    UNION ".join(
        f"SELECT workspace_id FROM {catalog}.{table}" for table in _REGIONAL_TABLES
    )


def out_of_scope_count_sql(catalog: str = "system", lookback_days: int = 30) -> str:
    """How many workspaces billed in the window but sit OUTSIDE this region.

    The companion to :func:`regional_workspaces_sql`. ``billing.usage`` is
    account-global, so it also holds workspaces this metastore has no data for;
    their spend is deliberately excluded from every total. This counts them, so the
    UI can say "N workspaces out of scope" instead of quietly dropping them.

    Account-level usage (``workspace_id IS NULL``, e.g. serverless/account SKUs) is
    not a workspace and is not counted here — same boundary as the billing filter.
    """
    return f"""
SELECT COUNT(DISTINCT u.workspace_id) AS out_of_scope_workspaces
FROM {catalog}.billing.usage u
WHERE u.workspace_id IS NOT NULL
  AND u.usage_date >= date_sub(current_date(), {lookback_days})
  AND u.usage_date < current_date()  -- closed-open window
  AND u.workspace_id NOT IN ({regional_workspaces_sql(catalog)})
""".strip()
