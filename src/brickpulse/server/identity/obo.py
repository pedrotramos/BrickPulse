"""On-behalf-of-user identity provider (dev default).

The access token is the logged-in user's OAuth token, forwarded by Databricks
Apps in the ``x-forwarded-access-token`` header; locally it comes from the
``DATABRICKS_TOKEN`` env var. SQL then
runs *as the user*, so Unity Catalog permissions naturally scope reads.

Role/workspace resolution over OBO is fuzzy (the token alone doesn't tell us the
persona), so for dev we let ``FINOPS_DEV_ROLE`` / ``FINOPS_DEV_WORKSPACES``
simulate either persona. In a real OBO deployment this would be replaced by a
lookup of the user's workspace-admin grants.
"""

from __future__ import annotations

from ..config import Settings
from ..models import Identity
from .base import PLATFORM_ADMIN, WORKSPACE_ADMIN, IdentityProvider


class OboIdentityProvider(IdentityProvider):
    def __init__(self, settings: Settings):
        self.settings = settings

    def resolve(self, request) -> Identity:
        headers = getattr(request, "headers", {}) or {}
        user = (
            headers.get("x-forwarded-email")
            or headers.get("x-forwarded-preferred-username")
            or headers.get("x-forwarded-user")
            or "local-dev@example.com"
        )
        role = self.settings.dev_role
        workspaces = self.settings.dev_workspaces
        all_workspaces = role == PLATFORM_ADMIN
        if role == WORKSPACE_ADMIN and not workspaces:
            # A workspace admin must be scoped to at least one workspace.
            workspaces = ["0"]
        return Identity(
            user=user,
            role=role,
            workspace_ids=[] if all_workspaces else workspaces,
            all_workspaces=all_workspaces,
            compute_mode=self.settings.compute_mode,
            auth_mode=self.settings.auth_mode,
        )

    def access_token(self, request) -> str | None:
        if self.settings.databricks_token:  # local dev
            return self.settings.databricks_token
        headers = getattr(request, "headers", {}) or {}
        return headers.get("x-forwarded-access-token")
