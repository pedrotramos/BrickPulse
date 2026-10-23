"""Service-principal identity provider (prod default).

The app runs as a service principal that has account-wide ``SELECT`` on
``system.*`` (or reads the precomputed ``finops_recommendations`` table). The SQL
connection therefore uses the SP token, NOT the user's. The *user's* identity is
still read from the forwarded headers, and a user->scope mapping decides which
workspaces they may see (app-side role scoping).

For this PoC the mapping is loaded from ``FINOPS_USER_SCOPE_JSON`` (a JSON object
``{email: {"role": ..., "workspace_ids": [...]}}``). In production this would be
a small governed table (``finops_user_scope``). Users not in the mapping default
to a workspace_admin with no workspaces (least privilege).
"""

from __future__ import annotations

from brickpulse.environment import get_env

import json
import os

from ..config import Settings
from ..models import Identity
from .base import PLATFORM_ADMIN, WORKSPACE_ADMIN, IdentityProvider


class SpIdentityProvider(IdentityProvider):
    def __init__(self, settings: Settings):
        self.settings = settings
        self._scope_map = self._load_scope_map()

    @staticmethod
    def _load_scope_map() -> dict:
        raw = get_env("FINOPS_USER_SCOPE_JSON", "").strip()
        if not raw:
            return {}
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                return {}
            return {k: v for k, v in value.items() if isinstance(v, dict)}
        except json.JSONDecodeError:
            return {}

    def resolve(self, request) -> Identity:
        headers = getattr(request, "headers", {}) or {}
        user = (
            headers.get("x-forwarded-email")
            or headers.get("x-forwarded-preferred-username")
            or headers.get("x-forwarded-user")
            or "unknown"
        )
        entry = self._scope_map.get(user, {})
        role = (entry.get("role") or WORKSPACE_ADMIN).lower()
        all_workspaces = role == PLATFORM_ADMIN
        workspaces = [str(w) for w in entry.get("workspace_ids", [])]
        return Identity(
            user=user,
            role=role,
            workspace_ids=[] if all_workspaces else workspaces,
            all_workspaces=all_workspaces,
            compute_mode=self.settings.compute_mode,
            auth_mode=self.settings.auth_mode,
        )

    def access_token(self, request) -> str | None:
        # SP credentials. In Databricks Apps the SP token is provided via the
        # environment; locally fall back to DATABRICKS_TOKEN.
        return self.settings.databricks_token or os.getenv("DATABRICKS_TOKEN")
