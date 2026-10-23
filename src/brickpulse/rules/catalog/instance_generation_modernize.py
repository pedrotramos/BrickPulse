"""Rule: classic compute on a previous-generation instance family.

EXPERIMENTAL (Labs tab).
Newer instance generations (AWS i4i / m6i-m7i / Graviton, Azure v5, GCP N2) do the
same work on newer CPUs, and the Arm families usually cost less per hour. This rule
finds all-purpose clusters, jobs and pipelines whose nodes still run on a family that
``config/instance_generations.yaml`` lists as replaced, and names the like-for-like
successor: same cores, the same or more memory, present in ``compute.node_types``.

Grain: an all-purpose cluster is its own resource; job and pipeline clusters are
ephemeral, so they roll up to the job / pipeline that owns the cluster config.

**Advisory, $0 — both booked AND addressable.** Nothing in system tables gives the
new type's VM price, and its DBU rate is known only when this account has billed it
(measured, like ``allpurpose_shape_mismatch``). Public list prices show the like-for-like
x86 successor usually costs the SAME OR MORE per hour (us-east-1: c5d→c6id +5%,
i3→i4i +10%, m5d→m6id +5%; only Graviton is cheaper), so the saving is the speed-up —
a performance claim no system table can predict. Carrying the old-generation spend as
"addressable" read as a saving the size of the cluster's whole bill, so the finding
reports that spend as evidence (``old_generation_monthly_usd``) and in the copy, and
books and addresses $0. When both DBU rates are measured the DBU/node-hour change is
shown, as information.
"""

from __future__ import annotations

import dataclasses
import re
from functools import lru_cache

from brickpulse.rules.contract import (
    EffortBand,
    Rule,
    RuleContext,
    RuleFinding,
    RulePriority,
)
from brickpulse.rules.coverage import coverage_evidence, coverage_note, underpriced
from brickpulse.rules.evidence import CONFIDENCE_LOW, CONFIDENCE_MEDIUM, evidence_block
from brickpulse.rules.history import history_evidence, history_note, insufficient_history
from brickpulse.rules.pricing import to_monthly
from brickpulse.rules.registry import register

MAPPING_FILE = "instance_generations.yaml"

_BASIS = (
    "system.compute.node_timeline (node type per node-minute, weighted by cores from "
    "system.compute.node_types) + system.billing.usage x list_prices for each cluster's "
    "corrected cost, {n}-day lookback; job and pipeline clusters roll up to their "
    "job / pipeline. The family mapping is maintained in config/instance_generations.yaml; "
    "a target is only suggested when it appears in system.compute.node_types with the "
    "same cores, no less memory and no GPU. DBU rates per node type are OBSERVED from "
    "this account's billing (homogeneous clusters, full constant-node hours), never "
    "assumed; VM prices are not in system tables. Experimental (Labs) — validate "
    "before trusting."
)


@dataclasses.dataclass(frozen=True)
class GenerationEntry:
    cloud: str
    source: re.Pattern
    x86: tuple[str, ...]
    arm: tuple[str, ...]
    min_dbr: dict

    def targets(self, arch: str) -> tuple[str, ...]:
        return self.x86 if arch == "x86" else self.arm


def _family_regex(family: str) -> str:
    """A target family pattern -> a full node-type-name pattern (the family name
    followed by nothing or a '.'/'-' size suffix)."""
    return f"(?:{family})(?:[.-].+)?"


def parse_mapping(data: dict) -> tuple[GenerationEntry, ...]:
    entries = []
    for cloud, items in (data or {}).items():
        for item in items or []:
            entries.append(GenerationEntry(
                cloud=str(cloud),
                source=re.compile(f"^(?:{item['from']})$"),
                x86=tuple(item.get("x86") or ()),
                arm=tuple(item.get("arm") or ()),
                min_dbr=dict(item.get("min_dbr") or {}),
            ))
    return tuple(entries)


@lru_cache(maxsize=1)
def load_mapping() -> tuple[GenerationEntry, ...]:
    """The maintained generation mapping, shipped as package data in config/."""
    from brickpulse.rules import _load_yaml

    return parse_mapping(_load_yaml(MAPPING_FILE))


def _sql_regex(patterns: list[str]) -> str:
    """One anchored alternation, escaped for a Spark SQL string literal (backslashes
    are escape characters there by default)."""
    joined = "^(?:" + "|".join(f"(?:{p})" for p in patterns) + ")$"
    return joined.replace("\\", "\\\\").replace("'", "")


def old_gen_regex(entries) -> str:
    return _sql_regex([e.source.pattern[4:-2] for e in entries])


def target_gen_regex(entries) -> str:
    fams = sorted({f for e in entries for f in (*e.x86, *e.arm)})
    return _sql_regex([_family_regex(f) for f in fams])


def match_entry(node_type: str, entries) -> GenerationEntry | None:
    for e in entries:
        if e.source.match(node_type or ""):
            return e
    return None


def _f(obj: dict, key: str) -> float | None:
    value = obj.get(key) if obj else None
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rate_pair(photon: bool, cur: dict, alt: dict) -> tuple[float, float] | None:
    """(current, target) DBU/node-hour at the SAME photon setting, preferring the
    resource's own setting. None unless both types were measured."""
    order = ("photon", "std") if photon else ("std", "photon")
    for key in order:
        c, t = _f(cur, f"rate_{key}"), _f(alt, f"rate_{key}")
        if c and t and c > 0 and t > 0:
            return c, t
    return None


def choose_target(
    entry: GenerationEntry, arch: str, cur: dict, alts: list[dict], photon: bool
) -> tuple[dict | None, tuple[float, float] | None]:
    """The successor for one old node type on one architecture. Families are tried in
    the mapping's order; within a family the leanest type that fits wins (the SQL
    already bounded memory). A family whose target has a measured DBU rate beats an
    earlier one without, so a dollar-backed suggestion is preferred when one exists."""
    first: tuple[dict, None] | None = None
    for family in entry.targets(arch):
        pat = re.compile(f"^{_family_regex(family)}$")
        fitting = sorted(
            (a for a in alts if a and pat.match(str(a.get("node_type") or ""))),
            key=lambda a: (_f(a, "memory_mb") or 0.0, str(a.get("node_type"))),
        )
        if not fitting:
            continue
        for a in fitting:
            pair = _rate_pair(photon, cur, a)
            if pair:
                return a, pair
        if first is None:
            first = (fitting[0], None)
    return first if first else (None, None)


# The runtime version LEADS the string ("15.4.x-scala2.12", "18.x-scala2.13",
# "dlt:17.3.18-delta-pipelines-..."). Anchor on it: a search would read the Scala
# version ("2.13") out of "18.x-scala2.13".
_DBR_RE = re.compile(r"^(?:dlt:)?(\d+)\.(\d+|x)")


def _dbr_tuple(version: str | None) -> tuple[int, int] | None:
    m = _DBR_RE.match(str(version or "").strip())
    if not m:
        return None
    minor = m.group(2)
    return int(m.group(1)), (0 if minor == "x" else int(minor))


def lowest_dbr(versions) -> tuple[int, int] | None:
    parsed = [v for v in (_dbr_tuple(x) for x in (versions or [])) if v]
    return min(parsed) if parsed else None


def _fmt(v: tuple[int, int]) -> str:
    return f"{v[0]}.{v[1]}"


@register
class InstanceGenerationModernizeRule(Rule):
    id = "instance_generation_modernize"
    experimental = True
    # Advisory: the new type's VM price is unknown and its DBU rate is only known
    # when billed here, so there is no defensible saving to book.
    default_savings_status = "advisory"
    version = "0.1.0"
    title = "Compute runs on a previous-generation instance type"
    resource_type = "cluster"
    category = "compute"
    contract_pricing = "none"  # $0 advisory: no dollar figure to re-price
    cloud_savings = "none"
    sql_file = "instance_generation_modernize.sql.j2"

    def render_sql(self, ctx: RuleContext) -> str:
        """Inject the two regexes derived from the maintained mapping, so the SQL
        narrows to old-generation node types and their candidate successors."""
        entries = load_mapping()
        extended = dataclasses.replace(ctx, thresholds={
            **ctx.thresholds,
            "old_gen_regex": old_gen_regex(entries),
            "target_gen_regex": target_gen_regex(entries),
        })
        return super().render_sql(extended)

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        if insufficient_history(row, ctx):
            return None
        if underpriced(row):
            return None

        t = ctx.thresholds
        entries = load_mapping()
        photon = bool(int(row.get("photon") or 0))
        alts_all = [a for a in (row.get("alt_node_types") or []) if a]
        lowest = lowest_dbr(row.get("dbr_versions"))

        moves = []
        for cur in row.get("old_node_types") or []:
            if not cur:
                continue
            nt = str(cur.get("node_type") or "")
            entry = match_entry(nt, entries)
            if entry is None:
                continue
            alts = [a for a in alts_all if a.get("for_node_type") == nt]
            x86, x86_pair = choose_target(entry, "x86", cur, alts, photon)
            arm, arm_pair = choose_target(entry, "arm", cur, alts, photon)
            if x86 is None and arm is None:
                continue  # no like-for-like successor offered here: don't guess
            arm_min = entry.min_dbr.get("arm")
            moves.append({
                "node_type": nt,
                "cores": _f(cur, "core_count"),
                "memory_mb": _f(cur, "memory_mb"),
                "node_minutes": cur.get("node_minutes"),
                "cost_30d_usd": _f(cur, "cost_30d_usd") or 0.0,
                "x86_target": x86.get("node_type") if x86 else None,
                "x86_dbu_rates": list(x86_pair) if x86_pair else None,
                "arm_target": arm.get("node_type") if arm else None,
                "arm_dbu_rates": list(arm_pair) if arm_pair else None,
                "arm_min_dbr": arm_min,
                "arm_needs_dbr_upgrade": bool(
                    arm and arm_min and lowest and lowest < _dbr_tuple(arm_min)),
            })
        if not moves:
            return None

        moves.sort(key=lambda mv: -mv["cost_30d_usd"])
        old_30d = sum(mv["cost_30d_usd"] for mv in moves)
        total_30d = float(row.get("cost_30d_usd") or 0.0)
        old_30d = min(old_30d, total_30d) if total_30d > 0 else old_30d
        addressable = to_monthly(old_30d, ctx.lookback_days)
        if addressable < float(t.get("min_addressable_usd", 50)):
            return None
        monthly_total = to_monthly(total_30d, ctx.lookback_days)
        share = old_30d / total_30d if total_30d > 0 else 1.0

        kind = row.get("resource_kind") or "cluster"
        rid = str(row["resource_id"])
        name = row.get("resource_name") or rid
        noun = {"job": "Job", "pipeline": "Pipeline"}.get(kind, "All-purpose cluster")
        top = moves[0]

        targets = []
        if top["x86_target"]:
            targets.append(f"{top['x86_target']} (current-generation x86)")
        if top["arm_target"]:
            targets.append(f"{top['arm_target']} (Arm / Graviton)")
        others = [mv["node_type"] for mv in moves[1:]]
        others_clause = f" (also: {', '.join(others)})" if others else ""

        rate_lines = []
        for arch, key in (("x86", "x86"), ("Arm", "arm")):
            pair = top[f"{key}_dbu_rates"]
            if pair:
                cur_r, tgt_r = pair
                rate_lines.append(
                    f"{top[f'{key}_target']} bills ~{tgt_r:.2f} DBU/node-hour vs "
                    f"~{cur_r:.2f} for {top['node_type']} in this account "
                    f"({(tgt_r / cur_r - 1) * 100:+.0f}% DBUs)"
                )
        rate_clause = (
            " Measured from your own billing: " + "; ".join(rate_lines) + "."
            if rate_lines else
            " Neither successor has been billed often enough in this account to "
            "measure its DBU rate, so no DBU comparison is shown."
        )

        why = (
            f"{noun} '{name}' ran ~${addressable:,.0f}/mo "
            f"({share * 100:.0f}% of its ~${monthly_total:,.0f}/mo) on "
            f"{top['node_type']}{others_clause}, a previous-generation instance family. "
            f"The like-for-like successor with the same cores and at least the same "
            f"memory is {' or '.join(targets)}. Newer generations usually finish the "
            f"same work faster on newer CPUs"
            + (", and Arm types usually cost less per hour." if top["arm_target"] else ".")
            + f"{rate_clause} No saving is shown: a like-for-like newer x86 type "
            f"often costs the same or more per hour, so what it saves depends on how "
            f"much faster your workload runs on it, which only a test run tells you."
        )

        where = {
            "job": "the job's cluster settings (Jobs > the job > Compute > Edit)",
            "pipeline": "the pipeline's compute settings (Pipelines > the pipeline > Settings > Compute)",
        }.get(kind, "the cluster (Compute > the cluster > Edit > Worker / Driver type)")
        how_parts = [
            f"Change the node type to {top['x86_target'] or top['arm_target']} in {where}, "
            f"then compare the run time and cost of a few runs before rolling it out."
        ]
        if top["arm_target"] and top["x86_target"]:
            how_parts.append(
                f"{top['arm_target']} (Arm) is usually the cheaper option, but check "
                f"that libraries, init scripts and native JARs have ARM64 builds first."
            )
        if top["arm_needs_dbr_upgrade"]:
            how_parts.append(
                f"Arm types need Databricks Runtime {top['arm_min_dbr']} or newer; this "
                f"resource runs {_fmt(lowest)}, so upgrade the runtime first."
            )
        how = " ".join(how_parts)

        any_rate = any(mv["x86_dbu_rates"] or mv["arm_dbu_rates"] for mv in moves)
        confidence = CONFIDENCE_MEDIUM if any_rate else CONFIDENCE_LOW

        if addressable >= 2000:
            priority = RulePriority.HIGH
        elif addressable >= 300:
            priority = RulePriority.MEDIUM
        else:
            priority = RulePriority.LOW

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type=kind,
            resource_id=rid,
            resource_name=name,
            # $0 addressable too: the old-generation spend is NOT the saving (see the
            # module docstring) — it travels as evidence.old_generation_monthly_usd.
            monthly_waste_usd=0.0,
            monthly_savings_usd=0.0,
            savings_status="advisory",
            monthly_savings_low_usd=0.0,
            effort=EffortBand.MEDIUM,  # a config change + a validation run
            priority=priority,
            why=why,
            how=how,
            evidence=evidence_block(
                confidence=confidence,
                data_basis=_BASIS.format(n=ctx.lookback_days)
                + history_note(row, ctx)
                + coverage_note(row),
                **history_evidence(row, ctx),
                **coverage_evidence(row),
                resource_kind=kind,
                cluster_source=row.get("cluster_source"),
                clusters=row.get("clusters"),
                dbr_versions=sorted(str(v) for v in (row.get("dbr_versions") or []) if v),
                old_generation_node_types=[mv["node_type"] for mv in moves],
                moves=[{k: (round(v, 2) if isinstance(v, float) else v)
                        for k, v in mv.items()} for mv in moves],
                old_generation_share=round(share, 4),
                old_generation_monthly_usd=round(addressable, 2),
                cost_30d_usd=row.get("cost_30d_usd"),
                dbus_30d=row.get("dbus_30d"),
            ),
        )
