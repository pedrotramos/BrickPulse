"""The Databricks Apps inventory model — what the Apps tab renders.

Like :mod:`models.genie`, this sits deliberately OUTSIDE the recommendation
contract: nothing here is a finding, carries a savings figure, or feeds the NBA
ranking. Unlike Genie, that is not only a scoping choice — for apps it is forced by
the data. There is no request or page-view log for apps in system tables (only
lifecycle events), so "is anyone using this?" cannot be answered and any idleness or
rightsizing claim would be a guess. See :mod:`finops.data.apps_sql` for the evidence.

So this model carries **facts and context, never verdicts**: what exists, what it
costs, how it is configured, when it was last touched. The fields are chosen to be
what a human needs in order to make the rightsizing call themselves —
``compute_size`` next to ``monthly_spend_usd`` next to ``last_deploy_time`` — with
``analysis_note`` stating in the UI why the app is not recommending anything.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AppInfo(BaseModel):
    """One Databricks App over the window: cost, configuration, lifecycle."""

    app_id: str
    app_name: str | None = None
    workspace_id: str | None = None
    workspace_name: str | None = None
    workspace_url: str | None = None

    #: Serverless compute size (MEDIUM / LARGE / …). The main configuration lever a
    #: human has, and the reason this tab exists.
    compute_size: str | None = None
    #: Distinct sizes seen in the window. > 1 means the app was resized, so the cost
    #: figure spans two configurations — worth showing rather than averaging away.
    size_changes: int = 1

    dbus: float = 0.0
    spend_usd: float = 0.0
    #: Window spend normalized to 30 days, comparable with every other monthly
    #: figure in the app.
    monthly_spend_usd: float = 0.0

    #: Distinct days this app billed. Apps are always-on while running and there is
    #: no scale-to-zero, so a count below the window means it was stopped for a
    #: while — a LIFECYCLE signal, never an idleness one.
    days_billed: int = 0
    observed_days: int = 0
    first_seen_date: str | None = None
    last_billed_date: str | None = None

    #: Lifecycle, from ``system.access.audit`` (the only usage-adjacent signal that
    #: exists for apps). All optional: most apps have no lifecycle event inside a
    #: given window, and absence means "not touched recently", not "unused".
    last_deploy_time: str | None = None
    last_start_time: str | None = None
    last_stop_time: str | None = None
    deploys: int = 0
    last_deployed_by: str | None = None


class AppSizeGroup(BaseModel):
    """Spend and count for one compute size — the estate's configuration mix."""

    compute_size: str
    apps: int = 0
    dbus: float = 0.0
    spend_usd: float = 0.0
    monthly_spend_usd: float = 0.0


class AppDay(BaseModel):
    """One day of the apps spend trend."""

    usage_date: str
    apps: int = 0
    dbus: float = 0.0
    spend_usd: float = 0.0


class AppsInventory(BaseModel):
    """Everything the Apps tab renders."""

    lookback_days: int = 30
    total_spend_usd: float = 0.0
    monthly_spend_usd: float = 0.0
    total_dbus: float = 0.0
    app_count: int = 0
    workspace_count: int = 0

    #: Apps that billed on every day of the window. Stated as a fact ("always-on"),
    #: which is normal for apps, NOT as a problem.
    always_on_count: int = 0

    #: True when no apps data could be read at all — the UI omits figures rather
    #: than showing a confident $0, matching ``AccountSpend.unavailable``.
    unavailable: bool = False
    #: True when apps are readable but genuinely unused here.
    no_usage: bool = False
    #: Sections that could not be read this time (e.g. a live query over the audit
    #: log that exceeded the time budget), so the UI can say why one is empty.
    missing_sections: list[str] = Field(default_factory=list)

    apps: list[AppInfo] = Field(default_factory=list)
    by_size: list[AppSizeGroup] = Field(default_factory=list)
    daily: list[AppDay] = Field(default_factory=list)

    #: Why this tab shows no recommendations. Rendered verbatim so the absence reads
    #: as a deliberate, explained limitation rather than an unfinished feature.
    analysis_note: str = ""
    #: Which system tables produced these figures, like the rules' ``data_basis``.
    data_basis: str = ""
