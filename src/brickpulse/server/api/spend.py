"""Total-spend endpoints — what the platform costs, not just what to fix.

Separate from ``/account/overview`` (which rolls up recommendations) because the
figures come from a different source: total spend is read from the precomputed
spend snapshot, or a live ``system.billing.usage`` aggregate as a fallback.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import get_scope, get_spend_source, get_view_scope
from ..identity import Scope
from ..models import AccountSpend, WorkspaceSpend
from ..data import SpendSource

router = APIRouter(prefix="/api", tags=["spend"])


@router.get("/spend", response_model=AccountSpend)
def account_spend(
    scope: Scope = Depends(get_view_scope),
    source: SpendSource = Depends(get_spend_source),
) -> AccountSpend:
    """Total billed spend for every workspace in scope, plus account rollups.

    ``unavailable=true`` means no spend data could be read at all (no snapshot yet
    and the live query failed or wasn't permitted) — the UI omits the figure rather
    than showing a misleading $0."""
    return source.account_spend(scope)


@router.get("/spend/{workspace_id}", response_model=WorkspaceSpend | None)
def workspace_spend(
    workspace_id: str,
    scope: Scope = Depends(get_scope),
    source: SpendSource = Depends(get_spend_source),
) -> WorkspaceSpend | None:
    return source.workspace_spend(scope, workspace_id)
