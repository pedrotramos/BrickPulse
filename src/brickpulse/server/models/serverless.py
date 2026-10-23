"""Models for the "About Serverless" view — the platform-admin case for moving.

This is a *narrative* built from data the app already has, not a new rule. The
argument is deliberately framed as "here are the problems you are managing today
that serverless removes entirely", because that is the honest version: serverless
does not make compute free, it removes whole categories of operational work
(sizing, idle timeouts, runtime upgrades, spot eviction handling, start latency).

Each :class:`ServerlessIssue` is one such category, carrying the *measured*
evidence from this account — how many resources are affected and what they cost —
so the pitch is specific rather than a brochure.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ServerlessIssueExample(BaseModel):
    """One affected resource, so the admin can click through to the evidence."""

    resource_type: str
    resource_id: str
    resource_name: str
    workspace_id: str
    workspace_name: str | None = None
    detail: str  # the specific measured problem, e.g. "avg 6% CPU across 8 workers"
    monthly_savings_usd: float = 0.0
    group_id: str | None = None  # deep link to the resource's recommendations


class ServerlessIssue(BaseModel):
    """A class of problem that serverless removes, with this account's evidence."""

    key: str  # stable slug, e.g. "cluster_sizing"
    title: str
    # What goes wrong today, in the admin's terms.
    problem: str
    # Why serverless makes the problem disappear (not just "cheaper").
    serverless_answer: str
    resource_count: int = 0
    # Addressable monthly savings across the affected resources, de-duplicated per
    # resource (never a sum of overlapping options on one resource).
    monthly_savings_usd: float = 0.0
    # Categories differ in how directly the data supports them; surfaced verbatim.
    evidence_note: str = ""
    examples: list[ServerlessIssueExample] = Field(default_factory=list)


class ServerlessReadiness(BaseModel):
    """How much of the estate is already serverless — the starting line."""

    serverless_spend_usd: float = 0.0
    classic_spend_usd: float = 0.0
    serverless_share: float = 0.0  # 0..1 of total spend already serverless
    spend_available: bool = True


class ServerlessCase(BaseModel):
    """The whole "About Serverless" payload."""

    # Total addressable savings across every issue, de-duplicated per RESOURCE so
    # a cluster flagged by three rules is counted once at its best option.
    total_monthly_savings_usd: float = 0.0
    affected_resource_count: int = 0
    workspace_count: int = 0
    lookback_days: int = 30
    readiness: ServerlessReadiness = Field(default_factory=ServerlessReadiness)
    issues: list[ServerlessIssue] = Field(default_factory=list)
    # Fleet-wide facts that support the case but aren't per-resource findings
    # (DBR version spread, query start-up latency, cluster start failures).
    fleet: dict = Field(default_factory=dict)
