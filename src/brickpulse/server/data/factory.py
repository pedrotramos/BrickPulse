"""Pick the recommendation source from ``FINOPS_COMPUTE_MODE``."""

from __future__ import annotations

from collections.abc import Callable

from ..config import Settings
from .base import RecommendationSource
from .demo_source import DemoRecommendationSource
from .live_source import LiveRecommendationSource
from .precompute_source import PrecomputeRecommendationSource
from .fleet_source import DemoFleetSource, FleetSource
from .anomaly_source import AnomalySource, DemoAnomalySource
from .apps_source import AppsSource, DemoAppsSource
from .genie_source import DemoGenieSource, GenieSource
from .realized_source import DemoRealizedSource, RealizedSource
from .spend_source import DemoSpendSource, SpendSource


def make_recommendation_source(settings: Settings, connect: Callable) -> RecommendationSource:
    if settings.compute_mode == "precompute":
        return PrecomputeRecommendationSource(settings, connect)
    if settings.compute_mode == "live":
        return LiveRecommendationSource(settings, connect)
    if settings.compute_mode == "demo":
        # Offline, deterministic sample data — no warehouse needed.
        return DemoRecommendationSource(settings, connect)
    raise ValueError(f"Unknown FINOPS_COMPUTE_MODE: {settings.compute_mode!r}")


def make_spend_source(settings: Settings, connect: Callable) -> SpendSource:
    """Total-workspace-spend source. Only demo mode needs a distinct class: the
    live/precompute behaviour (snapshot table, else live billing query) is the same
    object, since it already falls back on its own."""
    if settings.compute_mode == "demo":
        return DemoSpendSource(settings, connect)
    return SpendSource(settings, connect)


def make_genie_source(settings: Settings, connect: Callable) -> GenieSource:
    """Genie cost source. Like the spend source, only demo mode needs a distinct
    class — the snapshot-then-live fallback is built into ``GenieSource`` itself."""
    if settings.compute_mode == "demo":
        return DemoGenieSource(settings, connect)
    return GenieSource(settings, connect)


def make_apps_source(settings: Settings, connect: Callable) -> AppsSource:
    """Databricks Apps inventory source. Same shape as the Genie source — only demo
    mode needs a distinct class, since the snapshot-then-live fallback is built in."""
    if settings.compute_mode == "demo":
        return DemoAppsSource(settings, connect)
    return AppsSource(settings, connect)


def make_anomaly_source(settings: Settings, connect: Callable) -> AnomalySource:
    """Cost anomaly source. Same shape as the Apps source — only demo mode needs a
    distinct class, since the snapshot-then-live fallback is built in."""
    if settings.compute_mode == "demo":
        return DemoAnomalySource(settings, connect)
    return AnomalySource(settings, connect)


def make_fleet_source(settings: Settings, connect: Callable) -> FleetSource:
    """Fleet-wide serverless evidence (DBR spread, start-up latency, autoscaling).
    Always queries system tables live — these are slow-moving aggregates the
    precompute contract doesn't cover — except in demo mode."""
    if settings.compute_mode == "demo":
        return DemoFleetSource(settings, connect)
    return FleetSource(settings, connect)


def make_realized_source(settings: Settings, connect: Callable) -> RealizedSource:
    """The "Saved so far" source. Only demo mode needs a distinct class. There is no live
    variant: outside precompute mode ``RealizedSource`` itself reports ``unavailable``,
    since the history only exists once the daily tracker has run."""
    if settings.compute_mode == "demo":
        return DemoRealizedSource(settings, connect)
    return RealizedSource(settings, connect)
