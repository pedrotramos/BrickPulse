"""Rule catalog endpoint — powers the "what does this app check for" page."""

from __future__ import annotations

from fastapi import APIRouter

from brickpulse.rules import list_rules, load_thresholds

from ..config import get_settings
from ..models import RuleMeta

router = APIRouter(prefix="/api", tags=["rules"])


@router.get("/rules", response_model=list[RuleMeta])
def rules() -> list[RuleMeta]:
    thresholds = load_thresholds()
    hidden = get_settings().hidden_rule_ids
    out: list[RuleMeta] = []
    for rule in list_rules():
        # Rules hidden from the GUI are omitted from the catalog too — the page
        # documents "what this app checks for you", and listing a rule whose
        # findings never appear would be misleading. The pipeline still runs it.
        if rule.id in hidden:
            continue
        block = thresholds.get(rule.id, {})
        enabled = block.get("enabled")
        enabled = rule.default_enabled if enabled is None else bool(enabled)
        out.append(
            RuleMeta(
                id=rule.id,
                version=rule.version,
                title=rule.title,
                category=rule.category,
                resource_type=rule.resource_type,
                enabled=enabled,
                thresholds={k: v for k, v in block.items() if k != "enabled"},
            )
        )
    return out
