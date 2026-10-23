"""Persona scoping — the single choke point for workspace access control.

Both execution modes funnel through here. The workspace ids come only from the
server-side :class:`Scope` (derived from the resolved identity), never from
client input, so a workspace admin cannot reach another workspace's data by
tampering with query params. Ids are validated as digits and inlined (they are
Databricks numeric workspace ids), which keeps the ``IN`` list simple across
both the live subquery and the precompute SELECT.
"""

from __future__ import annotations

import re

from ..identity import Scope

_WORKSPACE_ID_RE = re.compile(r"^\d+$")


class ScopeError(ValueError):
    """Raised when a scope contains a non-numeric / untrusted workspace id."""


def _validated_ids(scope: Scope) -> list[str]:
    ids = []
    for wid in scope.workspace_ids:
        s = str(wid)
        if not _WORKSPACE_ID_RE.match(s):
            raise ScopeError(f"Invalid workspace id in scope: {wid!r}")
        ids.append(s)
    return ids


def where_clause(scope: Scope, column: str = "workspace_id") -> str:
    """Return a ``WHERE`` fragment (or empty string) enforcing the scope."""
    if scope.all_workspaces:
        return ""
    ids = _validated_ids(scope)
    if not ids:
        # A scoped user with no workspaces sees nothing.
        return f"WHERE {column} IN (NULL)"
    return f"WHERE {column} IN ({', '.join(ids)})"


def wrap_live(rule_sql: str, scope: Scope) -> str:
    """Wrap a rule's candidate SQL as a subquery and apply the scope filter.

    The rule SQL never filters by workspace; we add it here so the same rule
    serves account-wide precompute and workspace-scoped live queries."""
    clause = where_clause(scope)
    return f"SELECT * FROM (\n{rule_sql}\n) _rule {clause}".strip()
