"""Identity + config endpoints used to bootstrap the SPA and route personas."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ... import __version__
from ..config import get_settings
from ..deps import get_identity
from ..models import Identity

router = APIRouter(prefix="/api", tags=["meta"])


@router.get("/me", response_model=Identity)
def me(identity: Identity = Depends(get_identity)) -> Identity:
    """Who the caller is, their role, and which workspaces they may see."""
    # Surface the workspace host so the SPA can deep-link to the actual resource.
    identity.workspace_host = get_settings().server_hostname
    return identity


@router.get("/config")
def config() -> dict:
    s = get_settings()
    return {
        "brand": "BrickPulse",
        "version": __version__,
        "locales": ["pt-BR", "en", "es"],
        "compute_mode": s.compute_mode,
        "auth_mode": s.auth_mode,
        "currency": s.currency,
        "lookback_days": s.lookback_days,
        "pricing_mode": s.pricing_mode,
        # Labs tab. True when at least one experimental rule is active, so the
        # SPA shows the Labs nav; a deploy that sets FINOPS_EXPERIMENTAL_RULES="" hides
        # the tab entirely. Experimental findings never enter the main surfaces either way.
        "experimental_enabled": bool(s.experimental_rule_ids),
        # Optional Apps / Genie Costs tabs, both false by default. The SPA treats a
        # missing key (an older backend) as false too.
        "apps_tab_enabled": s.apps_tab_enabled,
        "genie_costs_tab_enabled": s.genie_costs_tab_enabled,
        # Optional Genie panel. Off unless enabled AND a transport resolves, so the
        # SPA simply renders nothing when Genie isn't wired up.
        "genie_enabled": s.genie_panel_enabled,
        # "api" = native context-aware chat (proxied Conversation API); "iframe" =
        # the embedded space. The SPA switches which panel it renders on this.
        "genie_mode": "api" if (s.genie_panel_enabled and s.genie_use_api) else "iframe",
        # Only meaningful in iframe mode; "" in API mode (the SPA doesn't frame anything).
        "genie_embed_url": (
            s.genie_embed_url if (s.genie_panel_enabled and not s.genie_use_api) else ""
        ),
    }
