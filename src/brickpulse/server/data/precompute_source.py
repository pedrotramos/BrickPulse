"""Precompute mode: read the latest snapshot from the Delta recommendation table.

The scheduled job (see ``precompute/``) has already run every rule account-wide
and written rows shaped exactly like :class:`Recommendation`. Reading is a cheap
scoped SELECT, so this scales to 100s of workspaces.
"""

from __future__ import annotations

from brickpulse.environment import get_env

import json
import os as _os
from datetime import datetime, timezone

from ..identity import Scope
from ..models import Recommendation
from .base import RecommendationSource
from .bounded_cache import BoundedCache
from .scoping import where_clause
from .sql_client import qualified_table

# Process-level cache for precompute reads. Unlike live mode this is a cheap
# SELECT, but on a contended shared warehouse it is still seconds per request and
# the recommendation service is NOT a process singleton — so without this, every
# page re-queries the warehouse. Keyed by (table, scope). Enabled by
# BRICKPULSE_PRECOMPUTE_CACHE_TTL (seconds); default 300; 0 disables caching.
# Safe to share objects because RecommendationService._annotate is non-mutating.
_CACHE_TTL = float(get_env("FINOPS_PRECOMPUTE_CACHE_TTL", "300") or "300")
# LRU bound on total cached rows (~15 KB each), not entries.
_CACHE_MAX_ROWS = int(get_env("FINOPS_PRECOMPUTE_CACHE_MAX_ROWS", "10000") or "10000")
_CACHE = BoundedCache(ttl_s=_CACHE_TTL, max_weight=_CACHE_MAX_ROWS, weight=len)

# NB: additive columns (resource_owner/resource_creator) require the table to carry
# them before this SELECT runs — the pipeline recreates the MV with the new schema
# on its next run, so deploy/run the precompute pipeline before (or with) the app,
# per the additive-contract discipline in contract_schema.py.
_COLUMNS = [
    "recommendation_id", "snapshot_date", "rule_id", "rule_version", "rule_title",
    "category", "workspace_id", "workspace_name", "workspace_url", "resource_type", "resource_id",
    "resource_name", "resource_owner", "resource_creator", "why", "how",
    "monthly_waste_usd", "monthly_savings_usd",
    "monthly_savings_low_usd", "monthly_savings_high_usd", "savings_status",
    "effort_band", "effort_score", "priority", "nba_score", "evidence", "computed_at",
]

# Contract / cloud pricing columns (finops_rules.price_basis). OPTIONAL on read: a
# snapshot written before they existed simply lacks them (backward compat),
# so they are selected only when the table actually has them and default to None.
_OPTIONAL_COLUMNS = [
    "price_basis", "contract_price_coverage",
    "monthly_savings_contract_usd", "monthly_savings_contract_low_usd",
    "monthly_savings_contract_high_usd", "cloud_cost_30d_usd", "monthly_cloud_savings_usd",
    # Absent on an older snapshot: the model then derives it (finops.finding_key).
    "finding_key",
    "affected_spend_scope", "savings_composition",
]

# Per-table cache of which optional columns exist (one LIMIT 0 probe per process).
_TABLE_COLUMNS = BoundedCache(ttl_s=300, max_weight=128)


class PrecomputeRecommendationSource(RecommendationSource):
    def _optional_present(self, table: str) -> list[str]:
        """The optional pricing columns this snapshot table actually carries."""
        key = (self.settings.server_hostname, self.settings.warehouse_id, table)
        cols = _TABLE_COLUMNS.get(key)
        if cols is None:
            try:
                with self.connect() as conn, conn.cursor() as cur:
                    cur.execute(f"SELECT * FROM {table} LIMIT 0")
                    cols = frozenset(d[0] for d in cur.description)
            except Exception:
                return []
            _TABLE_COLUMNS.put(key, cols)
        return [c for c in _OPTIONAL_COLUMNS if c in cols]

    def load(self, scope: Scope) -> list[Recommendation]:
        if _CACHE.ttl_s <= 0:
            return self._query(scope)
        key = (
            self.settings.server_hostname, self.settings.warehouse_id,
            self.settings.finops_table,
            "all" if scope.all_workspaces else ",".join(sorted(scope.workspace_ids)),
        )
        return _CACHE.get_or_load(key, lambda: self._query(scope))

    def _query(self, scope: Scope) -> list[Recommendation]:
        table = qualified_table(self.settings.finops_table)
        clause = where_clause(scope)
        columns_wanted = _COLUMNS + self._optional_present(table)
        cols = ", ".join(columns_wanted)
        sql = (
            f"SELECT {cols} FROM {table} "
            f"WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM {table}) "
            f"{('AND ' + clause[len('WHERE '):]) if clause else ''} "
            f"ORDER BY nba_score DESC"
        )
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(sql)
            columns = [d[0] for d in cur.description]
            rows = [dict(zip(columns, r)) for r in cur.fetchall()]
        return [self._to_recommendation(r, columns_wanted) for r in rows]

    @staticmethod
    def _to_recommendation(row: dict, columns_wanted: list[str]) -> Recommendation:
        evidence = row.get("evidence")
        if isinstance(evidence, str):
            try:
                evidence = json.loads(evidence)
            except (json.JSONDecodeError, TypeError):
                evidence = {}
        row["evidence"] = evidence or {}
        row.setdefault("computed_at", datetime.now(timezone.utc))
        return Recommendation(**{k: row[k] for k in columns_wanted if k in row and row[k] is not None})
