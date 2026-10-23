"""Application settings, sourced from environment variables (+ optional .env).

Two orthogonal switches drive the whole app:

* ``FINOPS_COMPUTE_MODE`` = ``live`` | ``precompute``
* ``FINOPS_AUTH_MODE``    = ``obo``  | ``sp``

Dev pairs ``live`` + ``obo``; prod pairs ``precompute`` + ``sp`` (see resources/brickpulse.app.yml).
"""

from __future__ import annotations

from brickpulse.environment import get_env

import os
from dataclasses import dataclass, field
from functools import lru_cache
from brickpulse.rules.pricing import validate_pricing_mode


def _split_csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


@dataclass
class Settings:
    # --- execution mode ---
    compute_mode: str = "live"  # live | precompute
    auth_mode: str = "obo"      # obo | sp

    # --- Databricks connection ---
    server_hostname: str = ""
    warehouse_id: str = ""
    databricks_token: str = ""  # user token (dev/OBO) or SP token
    catalog: str = "system"     # system schema (or a mirror)

    # --- precompute mode ---
    finops_table: str = "main.finops.finops_recommendations"
    # Per-workspace total spend (sibling of finops_table unless set). Read for the
    # Total Spend KPI; the app falls back to a live billing query if it's absent.
    spend_table: str = ""
    # Genie cost aggregates (sibling of finops_table unless set). Read by the Genie
    # Costs tab; the app falls back to live billing queries if it's absent.
    genie_table: str = ""
    # Databricks Apps inventory (sibling of finops_table unless set). Read by the
    # Apps tab; the app falls back to live billing queries if it's absent.
    apps_table: str = ""
    # Cost anomalies (sibling of finops_table unless set). Read by the Cost
    # Anomalies tab; falls back to a live billing query if it's absent.
    anomaly_table: str = ""
    # The precompute pipeline's own event log (sibling of finops_table unless set).
    # Read by /api/data-health to report the last run; absent on a fresh install or
    # if the pipeline predates the event_log setting, which is reported as
    # unavailable rather than as a failure.
    run_log_table: str = ""
    # "Saved so far" history (siblings of finops_table unless set). Plain Delta tables
    # written by the daily job's track_changes task (precompute/change_tracker.py), not
    # by the pipeline; absent until that task's first run. See docs/DATA_MODEL.md
    # "Savings tracker".
    change_events_table: str = ""
    finding_ledger_table: str = ""
    workspace_retirement_table: str = ""
    savings_daily_table: str = ""

    # --- rules ---
    # Rules computed and STORED by the pipeline but hidden from the whole GUI:
    # they contribute nothing to any list, chart, savings total or NBA ranking.
    # This is deliberately a presentation-layer switch, not `enabled: false` in
    # thresholds.yaml — we keep collecting the data (it feeds the About Serverless
    # evidence, and we may resurface these findings later), we just don't put them
    # in front of users today. Override with FINOPS_HIDDEN_RULES (CSV, or empty to
    # show everything).
    hidden_rules: list[str] = field(
        default_factory=lambda: ["unused_scheduled_jobs", "enable_photon_dbr"]
    )
    # Rule CATEGORIES pulled out of the main recommendation surfaces and shown on
    # their own tab instead of mixed into the optimization lists. Unlike
    # ``hidden_rules`` these findings are NOT hidden — they are fully visible, just
    # relocated: they must not count toward the recommendation total, must not
    # appear as an option on a resource that can be optimized, and must not enter
    # the Next Best Actions ranking or the savings leaderboard. ``governance`` is
    # the tagging/chargeback category (untagged_spend and any future governance
    # rule): its findings carry $0 savings and instead measure unattributable
    # spend, so ranking them next to real-dollar fixes created sprawl. Override
    # with FINOPS_SIDECAR_CATEGORIES (CSV, or empty to fold everything back into
    # the main surfaces). See data/base.py (the read-time partition) and the
    # /api/tagging endpoint that serves the tab.
    sidecar_categories: list[str] = field(default_factory=lambda: ["governance"])
    # net-new "opportunity" rules still being validated. Like
    # ``sidecar_categories`` these are RELOCATED (not hidden): fully computed and
    # stored, but served only by the Labs / Experimental tab (``fetch_experimental``),
    # so they never count toward the recommendation total, never appear as an option
    # on an optimizable resource, and never enter the Next Best Actions ranking or the
    # savings leaderboard. Unlike sidecar (keyed on category) this is keyed on the
    # RULE, and the default is DERIVED from the registry (rules that self-declare
    # ``experimental = True``) so adding one needs no edit here — see
    # :attr:`experimental_rule_ids`. Override with FINOPS_EXPERIMENTAL_RULES (CSV to
    # pin an explicit set, or empty to fold the experimental rules back into the main
    # surfaces). ``None`` means "use the registry-declared set".
    experimental_rules: list[str] | None = None
    lookback_days: int = 30
    # Live mode truncates the window to stay responsive (it scans billing.usage
    # synchronously per request). Precompute uses the full thresholds window.
    live_lookback_days: int = 3
    currency: str = "USD"
    pricing_mode: str = "auto"  # auto | list | account

    # --- Genie panel (optional PoC augmentation; OFF by default) ---
    # An embedded, read-only Databricks Genie space ("Ask Genie") shown on the
    # overview / workspace / resource pages so users can ask free-form questions
    # over the precomputed FinOps tables. Entirely optional: with the toggle off
    # (the default) the app behaves exactly as before and never references Genie.
    # Enable at deploy time with FINOPS_GENIE_ENABLED=true plus either an explicit
    # embed URL (FINOPS_GENIE_SPACE_URL) or just the space id (FINOPS_GENIE_SPACE_ID),
    # from which we build the canonical room URL using the workspace host.
    genie_enabled: bool = False
    genie_space_url: str = ""
    genie_space_id: str = ""
    # How the panel talks to Genie:
    #   "iframe" (default) — the embedded Genie space; needs the embedding
    #       allowlist and cannot know what the page is showing (the original PoC).
    #   "api"              — the native chat backed by the Genie Conversation API,
    #       proxied through this app, so the backend injects the workspace/resource
    #       the page is on and answers render in the app's own UI. Needs only the
    #       space id (no embedding allowlist). Falls back to iframe behaviour if the
    #       space id is missing.
    genie_mode: str = "iframe"

    # --- Optional analysis tabs, OFF by default ---
    # The Apps inventory tab (/apps, GET /api/apps*) and the Genie Costs tab
    # (/genie-costs, GET /api/genie and /api/genie/{workspace_id}). Off, the nav link,
    # the routes and the GET endpoints (404) are gone. The pipeline keeps writing
    # finops_apps_usage / finops_genie_usage either way. Not to be confused with
    # genie_enabled above, which is the "Ask Genie" panel (POST /api/genie/ask).
    apps_tab_enabled: bool = False
    genie_costs_tab_enabled: bool = False

    # --- dev-only persona simulation (OBO can't always resolve role) ---
    dev_role: str = "platform_admin"          # platform_admin | workspace_admin
    dev_workspaces: list[str] = field(default_factory=list)

    log_level: str = "INFO"

    @property
    def hidden_rule_ids(self) -> frozenset[str]:
        """Hidden rule ids as a set, for the read-time filter in
        :class:`data.base.RecommendationSource`."""
        return frozenset(self.hidden_rules)

    @property
    def sidecar_category_set(self) -> frozenset[str]:
        """Categories relocated to their own tab, as a set, for the read-time
        partition in :class:`data.base.RecommendationSource`."""
        return frozenset(self.sidecar_categories)

    @property
    def experimental_rule_ids(self) -> frozenset[str]:
        """Rule ids relocated to the Labs / Experimental tab, for the read-time
        partition in :class:`data.base.RecommendationSource`.

        When ``experimental_rules`` is None (the default) the set is DERIVED from the
        registry — every rule that self-declares ``experimental = True`` — so a new
        experimental rule is relocated automatically with no edit here. An explicit
        list (from FINOPS_EXPERIMENTAL_RULES) overrides that, and an empty list folds
        the experimental rules back into the main surfaces. The registry import is
        lazy and defensive: if the rules package can't be imported (a backend-only
        context), it degrades to "nothing experimental" rather than raising."""
        if self.experimental_rules is not None:
            return frozenset(self.experimental_rules)
        try:
            from brickpulse.rules import list_rules

            return frozenset(r.id for r in list_rules() if getattr(r, "experimental", False))
        except Exception:
            return frozenset()

    @property
    def genie_embed_url(self) -> str:
        """iframe ``src`` for the Genie panel, or ``''`` if not configured.

        An explicit ``FINOPS_GENIE_SPACE_URL`` wins — an embed URL can carry a
        path/query that the id alone can't reconstruct — otherwise build the
        canonical **embed** URL from the space id and the workspace host.

        NB: the path is ``/embed/genie/rooms/<id>``, NOT ``/genie/rooms/<id>``.
        The interactive room URL always refuses framing (X-Frame-Options); only
        the ``/embed/...`` endpoint is embeddable (subject to the workspace's
        approved-domains allowlist)."""
        if self.genie_space_url:
            return self.genie_space_url
        if self.genie_space_id and self.server_hostname:
            return f"https://{self.server_hostname}/embed/genie/rooms/{self.genie_space_id}"
        return ""

    @property
    def genie_space_id_resolved(self) -> str:
        """The Genie space id, from ``FINOPS_GENIE_SPACE_ID`` or parsed out of the
        configured URL (``.../genie/rooms/<id>`` with or without ``/embed``).

        The Conversation API is addressed by space id, not by an embed URL, so the
        API path needs the id even when only a URL was configured."""
        if self.genie_space_id:
            return self.genie_space_id
        url = self.genie_space_url
        if url:
            # Take the path segment after ".../rooms/", trimming any query/fragment.
            marker = "/rooms/"
            i = url.find(marker)
            if i != -1:
                tail = url[i + len(marker):]
                return tail.split("?")[0].split("#")[0].strip("/")
        return ""

    @property
    def genie_use_api(self) -> bool:
        """True when the panel should use the native Conversation-API chat rather
        than the embedded iframe. Requires a resolved space id; otherwise the panel
        degrades to iframe behaviour (which needs the embed URL instead)."""
        return (
            self.genie_enabled
            and self.genie_mode.lower() == "api"
            and bool(self.genie_space_id_resolved)
        )

    @property
    def genie_panel_enabled(self) -> bool:
        """Effective on/off: the toggle AND a usable transport. The iframe path
        needs an embed URL; the API path needs a resolved space id. A truthy toggle
        with neither stays off so the UI never renders a broken panel."""
        if not self.genie_enabled:
            return False
        if self.genie_use_api:
            return True
        return bool(self.genie_embed_url)

    def __post_init__(self) -> None:
        self.pricing_mode = validate_pricing_mode(self.pricing_mode)
        if not self.spend_table:
            parts = self.finops_table.split(".")
            parts[-1] = "finops_workspace_spend"
            self.spend_table = ".".join(parts)
        if not self.genie_table:
            parts = self.finops_table.split(".")
            parts[-1] = "finops_genie_usage"
            self.genie_table = ".".join(parts)
        if not self.apps_table:
            parts = self.finops_table.split(".")
            parts[-1] = "finops_apps_usage"
            self.apps_table = ".".join(parts)
        if not self.anomaly_table:
            parts = self.finops_table.split(".")
            parts[-1] = "finops_cost_anomalies"
            self.anomaly_table = ".".join(parts)
        if not self.run_log_table:
            parts = self.finops_table.split(".")
            parts[-1] = "finops_run_log"
            self.run_log_table = ".".join(parts)
        for attr, name in (
            ("change_events_table", "finops_change_events"),
            ("finding_ledger_table", "finops_finding_ledger"),
            ("workspace_retirement_table", "finops_workspace_retirement"),
            ("savings_daily_table", "finops_savings_daily"),
        ):
            if not getattr(self, attr):
                parts = self.finops_table.split(".")
                parts[-1] = name
                setattr(self, attr, ".".join(parts))

    @classmethod
    def from_env(cls) -> Settings:
        host = os.getenv("DATABRICKS_SERVER_HOSTNAME") or os.getenv("DATABRICKS_HOST", "")
        host = host.replace("https://", "").replace("http://", "").rstrip("/")
        return cls(
            compute_mode=get_env("FINOPS_COMPUTE_MODE", "live").lower(),
            auth_mode=get_env("FINOPS_AUTH_MODE", "obo").lower(),
            server_hostname=host,
            warehouse_id=os.getenv("DATABRICKS_WAREHOUSE_ID", ""),
            databricks_token=os.getenv("DATABRICKS_TOKEN", ""),
            catalog=get_env("FINOPS_CATALOG", "system"),
            finops_table=get_env("FINOPS_TABLE", "main.finops.finops_recommendations"),
            spend_table=get_env("FINOPS_SPEND_TABLE", ""),
            genie_table=get_env("FINOPS_GENIE_TABLE", ""),
            apps_table=get_env("FINOPS_APPS_TABLE", ""),
            anomaly_table=get_env("FINOPS_ANOMALY_TABLE", ""),
            run_log_table=get_env("FINOPS_RUN_LOG_TABLE", ""),
            change_events_table=get_env("FINOPS_CHANGE_EVENTS_TABLE", ""),
            finding_ledger_table=get_env("FINOPS_FINDING_LEDGER_TABLE", ""),
            workspace_retirement_table=get_env("FINOPS_WORKSPACE_RETIREMENT_TABLE", ""),
            savings_daily_table=get_env("FINOPS_SAVINGS_DAILY_TABLE", ""),
            hidden_rules=(
                _split_csv(get_env("FINOPS_HIDDEN_RULES"))
                if get_env("FINOPS_HIDDEN_RULES") is not None
                else ["unused_scheduled_jobs", "enable_photon_dbr"]
            ),
            sidecar_categories=(
                _split_csv(get_env("FINOPS_SIDECAR_CATEGORIES"))
                if get_env("FINOPS_SIDECAR_CATEGORIES") is not None
                else ["governance"]
            ),
            # None => derive from the registry (rules with experimental=True); an
            # explicit CSV (incl. empty) overrides. See experimental_rule_ids.
            experimental_rules=(
                _split_csv(get_env("FINOPS_EXPERIMENTAL_RULES"))
                if get_env("FINOPS_EXPERIMENTAL_RULES") is not None
                else None
            ),
            lookback_days=int(get_env("FINOPS_LOOKBACK_DAYS", "30")),
            live_lookback_days=int(get_env("FINOPS_LIVE_LOOKBACK_DAYS", "3")),
            currency=get_env("FINOPS_CURRENCY", "USD"),
            pricing_mode=get_env("FINOPS_PRICING_MODE", "auto"),
            genie_enabled=get_env("FINOPS_GENIE_ENABLED", "false").lower()
            in ("1", "true", "yes", "on"),
            genie_space_url=get_env("FINOPS_GENIE_SPACE_URL", "").strip(),
            genie_space_id=get_env("FINOPS_GENIE_SPACE_ID", "").strip(),
            genie_mode=get_env("FINOPS_GENIE_MODE", "iframe").strip().lower(),
            dev_role=get_env("FINOPS_DEV_ROLE", "platform_admin").lower(),
            dev_workspaces=_split_csv(get_env("FINOPS_DEV_WORKSPACES")),
            apps_tab_enabled=get_env("FINOPS_APPS_TAB_ENABLED", "false").strip().lower()
            in ("1", "true", "yes", "on"),
            genie_costs_tab_enabled=get_env("FINOPS_GENIE_COSTS_TAB_ENABLED", "false")
            .strip()
            .lower()
            in ("1", "true", "yes", "on"),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:  # dotenv is optional; env vars still work without it
        pass
    return Settings.from_env()
