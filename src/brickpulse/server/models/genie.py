"""Genie cost models — who uses Genie, what it costs, and who is still free.

Deliberately **not** part of the recommendation contract. Genie spend is
attributed to *people and spaces* rather than to compute resources, and this
release is analysis-only: nothing here produces a finding, a savings figure or an
NBA score. It is a separate read-only view of the account, which
is why it gets its own models, its own endpoint and its own tab.

On budgets
----------
There is no budget model here on purpose. ``usage_metadata.budget_policy_id`` is
NULL on every Genie usage row (verified account-wide), and the account-level
budget-policy API is not reachable from a workspace-scoped app, so *set budgets
cannot be read at all* from system tables today. :attr:`GenieCosts.budgets_note`
carries that explanation to the UI so the tab can say so plainly instead of
rendering an empty panel that looks like a bug.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

#: Human labels for the raw ``usage_metadata.genie.surface`` values.
SURFACE_LABELS = {
    "GENIE_CODE": "Genie Code",
    "GENIE_AGENTS": "Genie Spaces",
    "GENIE_ONE": "Genie One",
    "UNKNOWN": "Unattributed",
}


class GenieUser(BaseModel):
    """One person's (or service principal's) Genie usage over the window."""

    principal: str
    workspace_id: str | None = None
    workspace_name: str | None = None
    free_dbus: float = 0.0
    paid_dbus: float = 0.0
    spend_usd: float = 0.0
    monthly_spend_usd: float = 0.0
    active_days: int = 0
    last_active_date: str | None = None
    #: Surface/channel mix for this user, biggest first — explains *why* they are
    #: on the leaderboard, not just that they are.
    by_surface: list["GenieSurfaceUse"] = Field(default_factory=list)
    #: True when this principal never spilled past the free allowance.
    #:
    #: A stored field rather than ``paid_dbus <= 0``, because the displayed
    #: ``paid_dbus`` is rounded to 2dp and individual Genie records are routinely
    #: smaller than that (values like 0.009 DBU are normal). Deriving the tier from
    #: the rounded number would round a genuinely-paying user down to "free" and
    #: overstate the free-tier percentage — the tab's headline figure. The rollup
    #: sets this from the unrounded total.
    free_only: bool = True


class GenieSurfaceUse(BaseModel):
    """A (surface, channel) slice of one user's usage."""

    surface: str
    channel: str = "UNKNOWN"
    dbus: float = 0.0


class GenieSurface(BaseModel):
    """Account-wide spend for one surface x channel x tier combination."""

    surface: str
    surface_label: str = ""
    channel: str = "UNKNOWN"
    is_free: bool = False
    dbus: float = 0.0
    spend_usd: float = 0.0
    users: int = 0


class GenieSpace(BaseModel):
    """Cost attributed to a single Genie Space.

    Only ``GENIE_AGENTS`` usage carries a space id, so this covers Genie Spaces
    only — see :attr:`GenieCosts.space_coverage_note`.
    """

    space_id: str
    workspace_id: str | None = None
    workspace_name: str | None = None
    workspace_url: str | None = None
    dbus: float = 0.0
    spend_usd: float = 0.0
    monthly_spend_usd: float = 0.0
    users: int = 0
    active_days: int = 0
    #: Share of this space's DBUs driven through the REST API rather than the UI.
    #: A space that is mostly API traffic is an embedded/automated consumer, which
    #: is a different cost conversation from a space people click around in.
    api_share: float = 0.0
    last_active_date: str | None = None


class GenieWorkspace(BaseModel):
    """Genie spend attributed to a single workspace.

    Rolled up from the per-(workspace, principal) user rows rather than the
    surface or daily aggregates: those carry per-slice user counts that cannot be
    summed without double-counting anyone active on more than one surface or day.
    The user rows are one per workspace+principal, so distinct users and total
    spend are both exact here.
    """

    workspace_id: str
    workspace_name: str | None = None
    spend_usd: float = 0.0
    monthly_spend_usd: float = 0.0
    free_dbus: float = 0.0
    paid_dbus: float = 0.0
    users: int = 0
    #: Users in this workspace who spilled past the free allowance.
    paid_users: int = 0


class GenieDay(BaseModel):
    """One day of the adoption trend."""

    usage_date: str
    free_dbus: float = 0.0
    paid_dbus: float = 0.0
    spend_usd: float = 0.0
    users: int = 0


class GenieFreeTier(BaseModel):
    """The free-tier picture: how much of the estate costs nothing.

    ``GENIE_FREE_USAGE`` is a distinct zero-priced SKU, so these counts are read
    directly from billing rather than inferred from a spend threshold.
    """

    total_users: int = 0
    free_only_users: int = 0
    paid_users: int = 0
    free_only_pct: float = 0.0
    free_dbus: float = 0.0
    paid_dbus: float = 0.0
    #: Free DBUs consumed by users who never went paid — the allowance actually
    #: being absorbed at $0.
    free_dbus_free_only_users: float = 0.0


class GenieCosts(BaseModel):
    """Everything the Genie Costs tab renders."""

    lookback_days: int = 30
    total_spend_usd: float = 0.0
    monthly_spend_usd: float = 0.0
    total_dbus: float = 0.0
    workspace_count: int = 0
    #: True when no Genie usage could be read at all (no snapshot and the live
    #: query failed or was not permitted) — the UI omits figures rather than
    #: showing a confident $0, matching ``AccountSpend.unavailable``.
    unavailable: bool = False
    #: True when Genie is readable but simply unused in this account.
    no_usage: bool = False
    #: Aggregates that could not be read this time (e.g. a live query that exceeded
    #: the time budget). The sections they feed are empty for a reason the UI should
    #: state, rather than silently looking like "no Genie Spaces exist".
    missing_sections: list[str] = Field(default_factory=list)

    free_tier: GenieFreeTier = Field(default_factory=GenieFreeTier)
    top_users: list[GenieUser] = Field(default_factory=list)
    workspaces: list[GenieWorkspace] = Field(default_factory=list)
    surfaces: list[GenieSurface] = Field(default_factory=list)
    spaces: list[GenieSpace] = Field(default_factory=list)
    daily: list[GenieDay] = Field(default_factory=list)

    #: The single paid surface that dominates the bill (usually Genie Code) and its
    #: share of total paid spend, read from the surface breakdown. ``None`` when
    #: nothing is paid. Lets the UI state "~X% of paid Genie is <surface>" from the
    #: real data rather than a hardcoded figure.
    top_paid_surface_label: str | None = None
    top_paid_surface_pct: float = 0.0

    #: Why the per-space table covers less spend than the account total.
    space_coverage_note: str = ""
    #: Why there is no budget panel (see the module docstring).
    budgets_note: str = ""
    #: Which system tables produced these figures, shown verbatim in the UI like
    #: the rules' ``data_basis``.
    data_basis: str = ""


GenieUser.model_rebuild()


# --- Conversation API (the context-aware "Ask Genie" chat) --------------------
# These back the native chat panel (FINOPS_GENIE_MODE=api), which proxies the
# Genie Conversation API through this app so the backend can inject the workspace
# / resource the page is on — the thing the cross-origin iframe never could.


class GenieAskRequest(BaseModel):
    """One turn of the chat, from the SPA.

    ``conversation_id`` is absent on the first turn (the backend starts a new
    conversation) and echoed back on every turn after, so follow-ups keep context.
    ``workspace_id`` / ``resource_*`` describe what the page is currently showing;
    the backend weaves them into the prompt so "why is this idle?" resolves to the
    actual resource instead of getting a contextless answer. All optional: the
    account-overview panel sends none and asks account-wide questions.
    """

    question: str
    conversation_id: str | None = None
    workspace_id: str | None = None
    workspace_name: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    resource_name: str | None = None


class GenieAskResponse(BaseModel):
    """Genie's answer, shaped for the app's own chat bubble.

    ``text`` is the natural-language answer. ``query`` (when present) is the SQL
    Genie ran, and ``columns`` / ``rows`` its tabular result, so the panel can show
    a small result table without embedding Genie's own UI. ``error`` is set instead
    of ``text`` when the turn failed (Genie errored, timed out, or the space is
    misconfigured) so the panel states it plainly rather than hanging."""

    conversation_id: str | None = None
    message_id: str | None = None
    text: str = ""
    query: str | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[list] = Field(default_factory=list)
    truncated: bool = False
    error: str | None = None
