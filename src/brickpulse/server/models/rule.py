"""Rule catalog metadata exposed to the frontend Rule Catalog page."""

from __future__ import annotations

from pydantic import BaseModel


class RuleMeta(BaseModel):
    id: str
    version: str
    title: str
    category: str
    resource_type: str
    enabled: bool
    thresholds: dict


class Identity(BaseModel):
    """The resolved caller: who they are and what they may see."""

    user: str
    role: str  # "platform_admin" | "workspace_admin"
    workspace_ids: list[str]  # empty when all_workspaces is True
    all_workspaces: bool
    compute_mode: str
    auth_mode: str
    workspace_host: str = ""  # bare hostname for building resource deep links
