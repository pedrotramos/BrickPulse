from . import (
    account,
    anomalies,
    apps,
    experimental,
    genie,
    health,
    meta,
    nba,
    pricing,
    realized,
    recommendations,
    rules,
    serverless,
    spend,
    tagging,
    workspaces,
)

ROUTERS = [
    meta.router,
    account.router,
    workspaces.router,
    recommendations.router,
    nba.router,
    rules.router,
    spend.router,
    realized.router,
    serverless.router,
    tagging.router,
    experimental.router,
    genie.router,
    apps.router,
    anomalies.router,
    health.router,
    pricing.router,
]

__all__ = ["ROUTERS"]
