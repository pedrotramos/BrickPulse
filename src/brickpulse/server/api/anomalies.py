"""Cost anomaly endpoint — where spend broke away from its own recent normal.

Read-only and outside the recommendation flow, like the Genie and Apps endpoints:
nothing here produces a finding, a savings figure or an NBA score. An anomaly is a
question for the owner ("was this expected?"), not a saving — the response carries
that reason in ``analysis_note`` and the method in ``method_note``.

See ``finops/data/anomaly_sql.py`` for the detection method.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..data import AnomalySource
from ..data.scoping import ScopeError
from ..deps import get_anomaly_source, get_scope, get_view_scope
from ..identity import Scope
from ..models.anomaly import CostAnomalies

router = APIRouter(prefix="/api", tags=["anomalies"])


@router.get("/anomalies", response_model=CostAnomalies)
def cost_anomalies(
    scope: Scope = Depends(get_view_scope),
    source: AnomalySource = Depends(get_anomaly_source),
) -> CostAnomalies:
    """Spikes, step changes and new spenders in scope over the last 7 days.

    ``unavailable=true`` means no anomaly data could be read at all (no snapshot yet
    and the live query failed or missed its budget) — the UI says so rather than
    showing "no anomalies". ``no_anomalies=true`` means the data was read and nothing
    broke away from its baseline."""
    return source.cost_anomalies(scope)


@router.get("/anomalies/{workspace_id}", response_model=CostAnomalies)
def cost_anomalies_for_workspace(
    workspace_id: str,
    scope: Scope = Depends(get_scope),
    source: AnomalySource = Depends(get_anomaly_source),
) -> CostAnomalies:
    """The same view for a SINGLE workspace — the per-workspace drill-in.

    Mirrors ``GET /api/apps/{workspace_id}``. ``workspace_id`` is client-supplied, so
    it goes through ``Scope.narrowed_to``, which applies it as a filter *within* the
    caller's server-derived scope and can only ever remove access.
    """
    try:
        narrowed = scope.narrowed_to(workspace_id)
    except ScopeError:
        raise HTTPException(status_code=400, detail="workspace_id must be numeric") from None
    return source.cost_anomalies(narrowed)
