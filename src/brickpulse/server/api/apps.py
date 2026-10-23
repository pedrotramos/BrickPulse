"""Databricks Apps inventory endpoint — visibility, deliberately without verdicts.

Read-only, and outside the recommendation flow like the Genie endpoint: nothing here
produces a finding, a savings figure or an NBA score. The difference from Genie is
*why* — for apps it is not a scoping decision but a data limit. System tables carry
no app request/page-view log, only lifecycle events, so "is this app used?" cannot be
answered and any idleness or rightsizing claim would be a guess. The response carries
that reason in ``analysis_note`` so the UI states it rather than looking unfinished.

See ``finops/data/apps_sql.py`` for the evidence and for where a real rule would go
if Databricks ships an app usage log.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..data import AppsSource
from ..data.scoping import ScopeError
from ..deps import (
    get_apps_source,
    get_scope,
    get_view_scope,
    require_apps_tab,
)
from ..identity import Scope
from ..models.apps import AppsInventory

# Optional tab (FINOPS_APPS_TAB_ENABLED, off by default): every endpoint here 404s
# when it is off, so the SPA never renders a page whose data cannot load.
router = APIRouter(prefix="/api", tags=["apps"], dependencies=[Depends(require_apps_tab)])


@router.get("/apps", response_model=AppsInventory)
def apps_inventory(
    scope: Scope = Depends(get_view_scope),
    source: AppsSource = Depends(get_apps_source),
) -> AppsInventory:
    """Every Databricks App in scope: cost, compute size and lifecycle.

    ``unavailable=true`` means no apps data could be read at all (no snapshot yet and
    the live queries failed or weren't permitted) — the UI omits the figures rather
    than showing a misleading $0. ``no_usage=true`` means apps are readable but none
    are running here, which is a different message. ``missing_sections`` names any
    aggregate that didn't arrive; the inventory query joins the audit log and is the
    one most likely to miss the live budget."""
    return source.apps_inventory(scope)


@router.get("/apps/{workspace_id}", response_model=AppsInventory)
def apps_inventory_for_workspace(
    workspace_id: str,
    scope: Scope = Depends(get_scope),
    source: AppsSource = Depends(get_apps_source),
) -> AppsInventory:
    """The same inventory for a SINGLE workspace — the per-workspace drill-in.

    Mirrors ``GET /api/genie/{workspace_id}`` and ``GET /api/spend/{workspace_id}``.
    All three aggregates are grouped by ``workspace_id`` in SQL, so this is the same
    rollup over a narrower scope rather than a second code path.

    ``workspace_id`` is client-supplied, so it goes through ``Scope.narrowed_to``,
    which applies it as a filter *within* the caller's server-derived scope and can
    only ever remove access.
    """
    try:
        narrowed = scope.narrowed_to(workspace_id)
    except ScopeError:
        raise HTTPException(status_code=400, detail="workspace_id must be numeric") from None
    return source.apps_inventory(narrowed)
