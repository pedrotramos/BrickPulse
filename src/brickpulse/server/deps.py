"""FastAPI dependencies — wires config -> identity -> scope -> data source.

The identity provider is a singleton; identity, token, connection and source are
resolved per request so the SQL connection always uses the caller's token (OBO)
or the SP token (prod), and scope is always derived server-side.
"""

from __future__ import annotations

from functools import lru_cache

from fastapi import Depends, HTTPException, Request

from .config import get_settings
from .data import (
    AnomalySource,
    AppsSource,
    FleetSource,
    GenieSource,
    RealizedSource,
    SpendSource,
    make_anomaly_source,
    make_apps_source,
    make_fleet_source,
    make_genie_source,
    make_realized_source,
    make_recommendation_source,
    make_spend_source,
)
from .data.scoping import ScopeError
from .data.sql_client import get_connection
from .identity import IdentityProvider, Scope, make_identity_provider
from .models import Identity
from .services import RecommendationService
from .services.health_service import HealthService


@lru_cache(maxsize=1)
def get_identity_provider() -> IdentityProvider:
    return make_identity_provider(get_settings())


def get_identity(request: Request) -> Identity:
    identity = get_identity_provider().resolve(request)
    if identity.auth_mode == "sp" and not (identity.all_workspaces or identity.workspace_ids):
        raise HTTPException(status_code=403, detail="User has no BrickPulse scope")
    return identity


def get_scope(identity: Identity = Depends(get_identity)) -> Scope:
    return Scope.from_identity(identity)


def get_view_scope(request: Request, scope: Scope = Depends(get_scope)) -> Scope:
    """Scope for the account-wide *content* views, narrowed to a single workspace
    when the client asks for one via ``?workspace_id=``.

    This is what makes the UI's global workspace switcher apply everywhere at once:
    the frontend appends ``workspace_id`` to every request, so one selection scopes
    recommendations, the overview, the serverless case, Genie and Apps together
    rather than only the two pages that have a ``/workspaces/{id}/...`` route.

    It is still a *filter within* the caller's server-derived scope, never a grant:
    narrowing only removes access (see :meth:`Scope.narrowed_to`), so a workspace
    admin naming a workspace they do not own gets empty results, not someone else's.

    An absent or blank ``workspace_id`` (the "all workspaces" selection) leaves the
    caller's full scope untouched. A malformed (non-numeric) id is ignored here
    rather than raised: unlike the path-param endpoints, this is an optional view
    filter, so a bad value should fall back to the full scope, not 400 the page.
    """
    workspace_id = request.query_params.get("workspace_id")
    if not workspace_id:
        return scope
    try:
        return scope.narrowed_to(workspace_id)
    except ScopeError:
        return scope


def get_recommendation_service(request: Request) -> RecommendationService:
    settings = get_settings()

    # The SQL connection uses an explicit local PAT in dev (DATABRICKS_TOKEN), and
    # otherwise the app's own service principal via the SDK (see get_connection).
    # It is independent of the caller's identity, which is used only for scoping.
    # (Connecting with the forwarded OBO user token against the SQL warehouse hangs,
    # so we deliberately do NOT use it here; UC grants for the app SP govern reads.)
    def connect():
        return get_connection(settings, settings.databricks_token or None)

    source = make_recommendation_source(settings, connect)
    return RecommendationService(source)


def get_spend_source(request: Request) -> SpendSource:
    """Total-workspace-spend reader. Uses the same SP connection as the
    recommendation source (see the note above on why the caller's OBO token is
    not used for SQL); the caller's identity governs scoping only."""
    settings = get_settings()

    def connect():
        return get_connection(settings, settings.databricks_token or None)

    return _spend_source_for(settings, connect)


def _spend_source_for(settings, connect) -> SpendSource:
    # One instance per process so its short-lived cache is actually shared across
    # requests (the spend query scans billing data; see SpendSource).
    global _SPEND_SOURCE
    if _SPEND_SOURCE is None:
        _SPEND_SOURCE = make_spend_source(settings, connect)
    return _SPEND_SOURCE


_SPEND_SOURCE: SpendSource | None = None


def get_genie_source(request: Request) -> GenieSource:
    """Genie cost reader. Process-singleton like the spend source: its live path
    scans billing.usage four times, so the cache must be shared across requests."""
    settings = get_settings()

    def connect():
        return get_connection(settings, settings.databricks_token or None)

    global _GENIE_SOURCE
    if _GENIE_SOURCE is None:
        _GENIE_SOURCE = make_genie_source(settings, connect)
    return _GENIE_SOURCE


_GENIE_SOURCE: GenieSource | None = None


def get_apps_source(request: Request) -> AppsSource:
    """Databricks Apps inventory reader. Process-singleton like the Genie source:
    its live path scans billing.usage and joins the audit log, so the cache must be
    shared across requests rather than rebuilt per call."""
    settings = get_settings()

    def connect():
        return get_connection(settings, settings.databricks_token or None)

    global _APPS_SOURCE
    if _APPS_SOURCE is None:
        _APPS_SOURCE = make_apps_source(settings, connect)
    return _APPS_SOURCE


_APPS_SOURCE: AppsSource | None = None


def get_anomaly_source(request: Request) -> AnomalySource:
    """Cost anomaly reader. Process-singleton like the Apps source: its live path
    scans 35 days of billing.usage, so the cache must be shared across requests."""
    settings = get_settings()

    def connect():
        return get_connection(settings, settings.databricks_token or None)

    global _ANOMALY_SOURCE
    if _ANOMALY_SOURCE is None:
        _ANOMALY_SOURCE = make_anomaly_source(settings, connect)
    return _ANOMALY_SOURCE


_ANOMALY_SOURCE: AnomalySource | None = None


def get_realized_source(request: Request) -> RealizedSource:
    """The "Saved so far" reader. Process-singleton like the spend source, so its short
    cache is shared: the tile is on every Overview and Workspace page."""
    settings = get_settings()

    def connect():
        return get_connection(settings, settings.databricks_token or None)

    global _REALIZED_SOURCE
    if _REALIZED_SOURCE is None:
        _REALIZED_SOURCE = make_realized_source(settings, connect)
    return _REALIZED_SOURCE


_REALIZED_SOURCE: RealizedSource | None = None


def get_fleet_source(request: Request) -> FleetSource:
    """Fleet-wide serverless evidence. Process-singleton like the spend source:
    its queries scan wide system tables, so the cache must be shared."""
    settings = get_settings()

    def connect():
        return get_connection(settings, settings.databricks_token or None)

    global _FLEET_SOURCE
    if _FLEET_SOURCE is None:
        _FLEET_SOURCE = make_fleet_source(settings, connect)
    return _FLEET_SOURCE


_FLEET_SOURCE: FleetSource | None = None


def get_health_service(request: Request) -> HealthService:
    """Data-source access checker. Process-singleton so the 15-minute probe cache
    is shared: grants change on the order of weeks, and re-probing 18 tables per
    page load would add latency for an answer that almost never moves."""
    settings = get_settings()

    def connect():
        return get_connection(settings, settings.databricks_token or None)

    global _HEALTH_SERVICE
    if _HEALTH_SERVICE is None:
        _HEALTH_SERVICE = HealthService(settings, connect)
    return _HEALTH_SERVICE


_HEALTH_SERVICE: HealthService | None = None


def get_genie_conversation(request: Request):
    """Client for the context-aware Genie chat (FINOPS_GENIE_MODE=api).

    Unlike the SQL sources, this is NOT a process singleton and does NOT hold a
    connection: it is a thin per-request wrapper over a stateless REST client, and
    it must run as the CALLER (their OBO token governs which data Genie can read),
    so caching one across users would be wrong."""
    from .services.genie_conversation import GenieConversationClient

    return GenieConversationClient(get_settings())


def get_user_token(request: Request) -> str | None:
    """The signed-in user's access token (OBO), for calls that must run AS the user
    rather than as the app SP — e.g. the Genie Conversation API, whose answers are
    scoped by the user's own Unity Catalog grants."""
    return get_identity_provider().access_token(request)



def apps_tab_enabled() -> bool:
    """FINOPS_APPS_TAB_ENABLED, as a dependency so tests can switch it."""
    return get_settings().apps_tab_enabled


def require_apps_tab(enabled: bool = Depends(apps_tab_enabled)) -> None:
    """404 the Apps inventory endpoints when the tab is off (the default)."""
    from fastapi import HTTPException

    if not enabled:
        raise HTTPException(status_code=404, detail="The Apps tab is not enabled")


def genie_costs_tab_enabled() -> bool:
    """FINOPS_GENIE_COSTS_TAB_ENABLED, as a dependency so tests can switch it."""
    return get_settings().genie_costs_tab_enabled


def require_genie_costs_tab(enabled: bool = Depends(genie_costs_tab_enabled)) -> None:
    """404 the Genie Costs GET endpoints when the tab is off (the default). Not the
    Ask Genie panel (POST /api/genie/ask), which has its own FINOPS_GENIE_ENABLED gate."""
    from fastapi import HTTPException

    if not enabled:
        raise HTTPException(status_code=404, detail="The Genie Costs tab is not enabled")


