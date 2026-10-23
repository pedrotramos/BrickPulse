"""Data-source health endpoint — why a rule found nothing.

Separate from ``/health`` in ``main.py``: that one is the Databricks Apps
readiness gate ("is the process up?"). This one answers the FinOps question
("can we actually read the tables our numbers come from?"), which is the
difference between "no waste found" and "you never had permission to look".
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import get_health_service
from ..models.health import HealthReport
from ..services.health_service import HealthService

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/data-health", response_model=HealthReport)
def data_health(service: HealthService = Depends(get_health_service)) -> HealthReport:
    """Which system tables are readable, and which rules are blocked without them.

    Cached for 15 minutes (grants change on the order of weeks). ``degraded=false``
    with ``unknown=false`` means everything the engine needs is readable and the UI
    can hide the whole section."""
    return service.report()
