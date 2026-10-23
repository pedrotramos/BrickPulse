"""The "Saved so far" endpoints: what changed on flagged resources, valued from our estimate.

Read from the savings tracker's history (see :mod:`..data.realized_source`). Outside
the recommendation flow: nothing here adds to the "Savings potential" headline, the
NBA ranking or any other total. It shows that changes happened in the direction we
recommended. It can't prove the app caused them.

Scope works exactly like the spend and Genie endpoints. The account-wide route takes the
global ``?workspace_id=`` view filter (``get_view_scope``). The per-workspace route
narrows the caller's server-derived scope with ``Scope.narrowed_to``, so a workspace
admin naming a workspace they don't own gets an empty result, and a non-numeric id is a
400.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

from ..data import RealizedSource
from ..data.scoping import ScopeError
from ..deps import get_realized_source, get_scope, get_view_scope
from ..identity import Scope
from ..models import RealizedSavings

router = APIRouter(prefix="/api", tags=["savings-realized"])


@router.get("/savings-realized", response_model=RealizedSavings)
def savings_realized(
    scope: Scope = Depends(get_view_scope),
    source: RealizedSource = Depends(get_realized_source),
    from_: date | None = Query(None, alias="from"),
    to: date | None = Query(None),
) -> RealizedSavings:
    """Saved so far for every workspace in scope.

    ``from`` / ``to`` (ISO dates, inclusive) pick any window. The default is
    ``tracking_since`` through yesterday, i.e. cumulative since deployment.
    ``unavailable=true`` means there's no history to read (live mode, or the tracker
    hasn't run yet). The UI then hides the tile rather than showing $0."""
    return source.realized(scope, window_start=from_, window_end=to)


@router.get("/savings-realized/{workspace_id}", response_model=RealizedSavings)
def savings_realized_for_workspace(
    workspace_id: str,
    scope: Scope = Depends(get_scope),
    source: RealizedSource = Depends(get_realized_source),
    from_: date | None = Query(None, alias="from"),
    to: date | None = Query(None),
) -> RealizedSavings:
    """The same figure for ONE workspace (the Workspace page tile).

    ``workspace_id`` comes from the client, so it is a filter *within* the caller's
    scope (``Scope.narrowed_to``), never a grant."""
    try:
        narrowed = scope.narrowed_to(workspace_id)
    except ScopeError:
        raise HTTPException(status_code=400, detail="workspace_id must be numeric") from None
    return source.realized(narrowed, window_start=from_, window_end=to)
