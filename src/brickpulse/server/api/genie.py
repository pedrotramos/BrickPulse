"""Genie cost endpoint — platform-level analysis of AI/BI Genie spend.

Deliberately outside the recommendation flow: nothing here produces a finding, a
savings figure or an NBA score, so Genie spend never enters the recommendation
lists, the savings totals or the ranking. It is its own read-only view, which is
why it has its own source and model rather than a rule in ``rules/``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..config import get_settings
from ..data import GenieSource
from ..data.scoping import ScopeError
from ..deps import (
    get_genie_conversation,
    get_genie_source,
    get_scope,
    get_user_token,
    get_view_scope,
    require_genie_costs_tab,
)
from ..identity import Scope
from ..models.genie import GenieAskRequest, GenieAskResponse, GenieCosts

router = APIRouter(prefix="/api", tags=["genie"])

# The Genie Costs tab is optional (FINOPS_GENIE_COSTS_TAB_ENABLED, off by default):
# its two GET endpoints 404 when it is off. Deliberately per route, not on the router,
# because POST /genie/ask belongs to the Ask Genie panel and has its own gate.
_COSTS_TAB = [Depends(require_genie_costs_tab)]


@router.get("/genie", response_model=GenieCosts, dependencies=_COSTS_TAB)
def genie_costs(
    scope: Scope = Depends(get_view_scope),
    source: GenieSource = Depends(get_genie_source),
) -> GenieCosts:
    """Genie spend for every workspace in scope: free-tier split, top spenders,
    surface mix, per-space cost and the adoption trend.

    ``unavailable=true`` means no Genie data could be read at all (no snapshot yet
    and the live queries failed or weren't permitted) — the UI omits the figures
    rather than showing a misleading $0. ``no_usage=true`` means Genie is readable
    but genuinely unused here, which is a different message."""
    return source.genie_costs(scope)


@router.get("/genie/{workspace_id}", response_model=GenieCosts, dependencies=_COSTS_TAB)
def genie_costs_for_workspace(
    workspace_id: str,
    scope: Scope = Depends(get_scope),
    source: GenieSource = Depends(get_genie_source),
) -> GenieCosts:
    """The same analysis for a SINGLE workspace — the per-workspace drill-in.

    Mirrors ``GET /api/spend/{workspace_id}``. All four Genie aggregates are already
    grouped by ``workspace_id`` in SQL (and it is a real column on the snapshot table,
    never inside the JSON payload), so this is the same rollup over a narrower scope
    rather than a second code path.

    ``workspace_id`` comes from the client, so it is applied via
    ``Scope.narrowed_to`` — a filter *within* the caller's server-derived scope. A
    workspace admin naming a workspace they do not own gets empty results, not
    someone else's spend.
    """
    try:
        narrowed = scope.narrowed_to(workspace_id)
    except ScopeError:
        # A non-numeric id is a malformed request, not a server fault. The id is
        # already rejected before it can reach any SQL (scoping validates it), but
        # letting the error surface as a 500 misreports whose problem it is.
        raise HTTPException(status_code=400, detail="workspace_id must be numeric") from None
    return source.genie_costs(narrowed)


@router.post("/genie/ask", response_model=GenieAskResponse)
def genie_ask(
    req: GenieAskRequest,
    client=Depends(get_genie_conversation),
    user_token: str | None = Depends(get_user_token),
) -> GenieAskResponse:
    """One turn of the context-aware "Ask Genie" chat (FINOPS_GENIE_MODE=api).

    Proxies the Genie Conversation API so the backend can inject the workspace /
    resource the page is showing into the prompt — the thing the cross-origin
    iframe never could. Runs as the signed-in user (their token governs which data
    Genie may read), so this is not a scope-bypass: prompt context is guidance, and
    Unity Catalog grants are the real boundary.

    404 when the API panel isn't configured, so the SPA can hide the input rather
    than POST into the void. Genie/network failures come back as a stated ``error``
    on a 200 (the panel shows it in-bubble), never a 500 that breaks the page.
    """
    settings = get_settings()
    if not (settings.genie_panel_enabled and settings.genie_use_api):
        raise HTTPException(status_code=404, detail="Genie chat is not enabled")
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question must not be empty")
    return client.ask(req, user_token)
