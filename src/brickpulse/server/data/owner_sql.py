"""Resource owner/creator resolution — one query, one caller (the precompute owner_map).

A recommendation says "this cluster wastes $800/mo", but the platform admin's very
next question is "whose is it?" — and until now the app could not answer, because
owner/creator live in the system tables and were never carried onto the finding.

Rather than join every one of the 20 rule templates to a resource dimension (each
resource type has a different owner column, and clusters/warehouses have none that
is reliably populated), we resolve ownership from the ONE table every rule already
reads and every finding is priced against: ``billing.usage``. Its
``identity_metadata`` struct carries, per usage row:

* ``owned_by``   — the resource owner (present for jobs/pipelines; the strongest signal)
* ``run_as``     — the identity the workload runs as (a good owner fallback)
* ``created_by`` — who created the resource

and its ``usage_metadata`` struct carries the resource id under a per-type field
(``cluster_id`` / ``warehouse_id`` / ``job_id`` / ``dlt_pipeline_id`` /
``endpoint_id``) — exactly the ids the rules key their findings on. This is the same
``identity_metadata`` the ``untagged_spend`` rule already reads and which was
verified live, so it needs no new source-table grant: any workspace that can price a
finding can resolve its owner. Where usage rows carry no identity (some resource
types never populate it), the owner is simply NULL — "unknown", which never
suppresses the finding.
"""

from __future__ import annotations


def owner_map_sql(
    catalog: str = "system", lookback_days: int = 30
) -> str:
    """Per-resource owner/creator over the window, keyed to match the findings.

    Emits one row per ``(workspace_id, resource_id)`` with the most-recent non-null
    ``owner`` and ``creator`` seen on that resource's billed usage. ``resource_id``
    is the COALESCE of the per-type id fields — identical to how ``untagged_spend``
    derives it — so it lines up with ``RuleFinding.resource_id`` for every resource
    type the rules emit (cluster / warehouse / job / pipeline / serving endpoint).

    Not scoped by workspace on purpose: like the rule and spend SQL, the caller
    applies scope, so the one query serves the account-wide precompute directly.
    """
    usage = f"{catalog}.billing.usage"
    return f"""
WITH ident AS (
  SELECT
    u.workspace_id,
    -- The billed resource this usage row belongs to. A usage row carries exactly
    -- one of these ids, so COALESCE picks it without ambiguity. Matches the
    -- resource_id every rule's finding is keyed on.
    COALESCE(
      u.usage_metadata.cluster_id,
      u.usage_metadata.warehouse_id,
      CAST(u.usage_metadata.job_id AS STRING),
      u.usage_metadata.dlt_pipeline_id,
      u.usage_metadata.endpoint_id
    ) AS resource_id,
    -- Owner: the resource's declared owner, falling back to the identity it runs
    -- as. NOT created_by here — "who owns it" and "who created it" are different
    -- questions the app surfaces separately.
    COALESCE(u.identity_metadata.owned_by, u.identity_metadata.run_as) AS owner,
    u.identity_metadata.created_by AS creator,
    -- Prefer the most recent attribution when a resource changed hands.
    u.usage_start_time AS seen_at
  FROM {usage} u
  WHERE u.usage_date >= date_sub(current_date(), {lookback_days})
    AND u.usage_date < current_date()  -- closed-open window
    -- documented exception: this picks the most-recent OWNER (non-additive
    -- metadata), not a cost sum, so ORIGINAL-only is correct here — a RETRACTION
    -- would otherwise compete with its ORIGINAL for "most recent" attribution.
    AND u.record_type = 'ORIGINAL'
    AND COALESCE(
          u.usage_metadata.cluster_id,
          u.usage_metadata.warehouse_id,
          CAST(u.usage_metadata.job_id AS STRING),
          u.usage_metadata.dlt_pipeline_id,
          u.usage_metadata.endpoint_id
        ) IS NOT NULL
)
SELECT
  workspace_id,
  resource_id,
  -- The latest non-null owner/creator for the resource. max_by(x, t) returns the
  -- x at the greatest t; filtering the nulls out first means a single recent row
  -- with an absent owner does not blank an owner seen on every other row.
  max_by(owner,   seen_at) FILTER (WHERE owner   IS NOT NULL) AS resource_owner,
  max_by(creator, seen_at) FILTER (WHERE creator IS NOT NULL) AS resource_creator
FROM ident
GROUP BY workspace_id, resource_id
"""
