"""Data-source health — why a rule found nothing.

The app reads 18 system tables across six schemas, several of which are enabled
and granted independently. When one is unreadable, the rules that depend on it
raise, get caught, and return no findings — so the UI showed "no savings found",
which is indistinguishable from "everything is already efficient". For a tool
whose whole job is to be believed about money, silently reporting a permissions
gap as good news is the worst available failure mode.

These models carry the answer to the reader: which sources are readable, which
rules are consequently unavailable, and what to grant to fix it. Deliberately
NOT part of the recommendation contract — health is about the pipeline, not about
a resource — so it has its own endpoint and its own models, like ``spend`` and
``genie``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SourceHealth(BaseModel):
    """Availability of one system table."""

    name: str                    # schema.table, e.g. "compute.node_timeline"
    status: str                  # PASS | WARN | FAIL
    severity: str                # INFO | MEDIUM | CRITICAL
    required: bool               # a core billing table the app cannot work without
    message: str = ""            # the underlying error, when unavailable
    #: Rules that cannot run because this table is unreadable. Lets the UI answer
    #: "what am I not seeing?" next to the grant that would fix it.
    blocks_rules: list[str] = Field(default_factory=list)


class RuleAvailability(BaseModel):
    """A rule that cannot run, and what it is waiting on."""

    rule_id: str
    missing_sources: list[str] = Field(default_factory=list)


class LastRun(BaseModel):
    """Summary of the most recent precompute pipeline update.

    Read from ``finops_run_log`` (the pipeline's own event log). Answers the
    question the recommendation table cannot: are these findings from last night,
    or from a run that half-failed a week ago? A stale or partially-failed run
    makes every number on screen older than it looks."""

    #: ISO timestamp of the latest update's last event.
    finished_at: str | None = None
    #: Whether that update completed. None when it is still running.
    ok: bool | None = None
    #: Flows that reached COMPLETED / FAILED in the latest update.
    flows_ok: int = 0
    flows_failed: int = 0
    #: Names of failed flows, so the reader knows WHICH rule is missing.
    failed_flows: list[str] = Field(default_factory=list)
    #: Rows written by the recommendations flow in that update.
    rows_written: int | None = None
    #: Set when the run log itself could not be read (no table yet, or no grant).
    unavailable: bool = False


class HealthReport(BaseModel):
    """The whole picture, as one payload.

    ``degraded`` is the single flag the UI needs to decide whether to show a
    banner at all: on a fully-granted workspace the entire section stays hidden
    rather than adding a permanently-green ornament nobody reads."""

    checked_at: str
    catalog: str
    compute_mode: str
    sources: list[SourceHealth] = Field(default_factory=list)
    unavailable_rules: list[RuleAvailability] = Field(default_factory=list)
    #: True when any source failed a probe. Note this is about READABILITY, never
    #: about a table being empty — a new workspace with an empty audit log is
    #: healthy, and conflating the two would cry wolf on every fresh deployment.
    degraded: bool = False
    #: Set when the probe itself could not run (no warehouse, bad credentials).
    #: Distinct from `degraded`: we know nothing here, rather than knowing
    #: something is broken, and the UI must not claim sources are fine.
    unknown: bool = False
    message: str = ""
    #: The latest precompute run, when readable. Only meaningful in precompute mode
    #: (live mode computes on demand, so there is no run to report).
    last_run: LastRun | None = None

    @property
    def failing_required(self) -> list[str]:
        """Core tables that are unreadable — the app cannot produce findings."""
        return [s.name for s in self.sources if s.required and s.status == "FAIL"]
