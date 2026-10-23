"""Change tracking: did a flagged resource move the way we recommended?

This is the per-rule half of "Saved so far" (docs/DATA_MODEL.md, "Savings tracker").
The daily ``track_changes`` task (``precompute/change_tracker.py``) reads the current
configuration of every resource the app has flagged and asks, for each finding:
*was the proposed setting moved toward the proposal, or was the resource deleted?*
The answer is a :class:`ChangeAssessment` with a ``fraction`` in [0, 1] of the
finding's own estimate. The tracker values it as ``fraction * estimate_before``.

Per-rule tracking is DECLARATIVE and lives in the rule's own file (the golden rule:
no shared list). A rule sets the class attribute ``tracked_setting``::

    tracked_setting = TrackedSetting(
        resource="warehouse", column="auto_stop_minutes",
        baseline_key="auto_stop_minutes", proposed_key="target_autostop_minutes",
        direction="decrease", off_values=(0, None),
    )

and the generic engine (:func:`assess_change`) does the rest. A rule that declares
nothing still gets deletion credit. A rule with an odd case may define its own
``assess_change(self, baseline, last_open_evidence, now)`` method, which
:func:`assess_for_rule` calls instead.

Everything here is pure Python on plain dicts, so it is unit-tested with fixtures
(``tests/test_change_tracking.py``). No Spark, no SQL.

Valuation of a partial move, best method first:

1. **curve**: piecewise-linear interpolation over the finding's own savings curve.
   Two sources: ``evidence.phasing`` (Workstream C: current -> 0, recommended step ->
   ``step_savings_usd``, data target -> ``target_savings_usd``) and
   ``evidence.savings_curve`` (``[{"value", "monthly_usd"}, ...]``, emitted by the
   auto-stop rules from PR F1b). The fraction is ``curve(after) / curve(proposed)``.
2. **ladder**: warehouse t-shirt sizes double their DBU rate per step, so moving
   ``k_done`` of ``k_proposed`` steps is worth ``(1 - 2^-k_done) / (1 - 2^-k_proposed)``.
3. **linear**: ``(before - after) / (before - proposed)``. A timeout of "off" counts as
   the longest idle gap from the evidence when present, else 1,440 minutes.

The fraction is capped at 1.0 (reaching or going beyond the proposal is worth the
estimate, never more) and floored at 0 (a move away is worth nothing, never less).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

#: Kinds of change, as stored in ``finops_change_events.change_kind``.
KIND_TOWARD = "toward"      # moved toward the proposal, not all the way
KIND_REACHED = "reached"    # reached the proposal exactly (or a binary change)
KIND_BEYOND = "beyond"      # went past the proposal (credit capped at 1.0)
KIND_DELETED = "deleted"    # the resource is gone (delete_time set)
KIND_PAUSED = "paused"      # a job's schedule was paused
KIND_AWAY = "away"          # moved away again (or back to the baseline): value 0
KIND_UNCHANGED = "unchanged"  # engine-only: same as the baseline (never stored)

CHANGE_KINDS = (KIND_TOWARD, KIND_REACHED, KIND_BEYOND, KIND_DELETED, KIND_PAUSED, KIND_AWAY)
#: Kinds that carry a positive valuation.
CREDIT_KINDS = frozenset({KIND_TOWARD, KIND_REACHED, KIND_BEYOND, KIND_DELETED, KIND_PAUSED})

#: Valuation methods, as stored in ``finops_change_events.valuation_method``.
METHOD_CURVE = "curve"
METHOD_LADDER = "ladder"
METHOD_LINEAR = "linear"
METHOD_BINARY = "binary"
METHOD_DELETION = "deletion"

#: A timeout of "off" with no idle-gap evidence counts as one day.
OFF_DEFAULT_MINUTES = 1440.0

#: Evidence keys the ledger keeps in ``evidence_core`` for every finding, on top of
#: the rule's own tracked-setting keys. Small on purpose: the ledger has one row per
#: finding per day, and these are all the valuation ever reads.
CORE_EVIDENCE_KEYS = (
    "phasing",
    "savings_curve",
    "longest_idle_gap_min",
    "longest_idle_gap_hours",
)

#: Key under which the tracker stores the config value it OBSERVED for the tracked
#: column on that day. The baseline falls back to it when the evidence does not
#: carry the "from" value (e.g. a job's ``paused`` flag is in no rule's evidence).
OBSERVED_KEY = "_observed"


@dataclass(frozen=True)
class TrackedSetting:
    """Which configuration setting a rule proposes to change, and how to value a move.

    ``resource`` names the config query (``precompute/change_tracker.py``
    ``CONFIG_QUERIES``): ``warehouse`` | ``cluster`` | ``job`` | ``serving_endpoint`` |
    ``instance_pool`` | ``pipeline`` | ``alert``. ``column`` is a column of that query's
    normalized output row (usually the system-table column itself).
    """

    resource: str
    column: str
    #: Evidence key holding the value when the finding was raised (the "from" value).
    #: None, or absent from the evidence: fall back to the tracker's own observation.
    baseline_key: str | None = None
    #: Evidence key holding the proposed value. None: use ``proposed_value``.
    proposed_key: str | None = None
    #: Fixed proposed value, when the evidence does not carry one.
    proposed_value: Any = None
    #: ``decrease`` | ``increase`` (numbers, or ladder positions) | ``equals`` (a value
    #: such as ``SERVERLESS`` or ``True``).
    direction: str = "decrease"
    #: Values meaning "off" for a timeout (0 / NULL = never stops). For ``decrease``
    #: they rank above every number.
    off_values: tuple = ()
    #: Evidence keys giving the effective value of "off" in minutes (the longest idle
    #: gap). ``*_hours`` keys are converted. First present wins.
    off_equivalent_keys: tuple = ("longest_idle_gap_min", "longest_idle_gap_hours")
    off_default: float = OFF_DEFAULT_MINUTES
    #: ``linear`` | ``ladder`` | ``binary`` (only reaching the proposal counts) |
    #: ``any_toward`` (any move toward counts in full). A savings curve in the
    #: evidence takes precedence for ``linear`` and ``ladder``.
    valuation: str = "linear"
    #: Ordered values for ``ladder`` (smallest first).
    ladder: tuple = ()
    #: The rule emits ``evidence.phasing`` for THIS setting: its data target is
    #: the full-credit proposal and its three points form a savings curve.
    uses_phasing: bool = False
    #: Kind recorded when the proposal is reached (``paused`` for a job schedule).
    reached_kind: str = KIND_REACHED
    #: Human label for the UI ("Auto-stop", "Minimum clusters").
    label: str = ""

    def evidence_keys(self) -> tuple[str, ...]:
        keys = [k for k in (self.baseline_key, self.proposed_key) if k]
        keys.extend(self.off_equivalent_keys)
        return tuple(dict.fromkeys(keys))


@dataclass(frozen=True)
class ConfigState:
    """The current configuration of one resource: its latest SCD row.

    ``values`` holds the normalized setting columns. A key that is ABSENT means the
    query could not read it (unknown); a key present with value None means NULL.
    """

    values: dict
    change_time: datetime | None = None
    delete_time: datetime | None = None


@dataclass(frozen=True)
class ChangeAssessment:
    """How far a finding's resource moved toward what we proposed."""

    kind: str
    fraction: float
    method: str
    changed_at: datetime | None
    setting: str | None = None
    baseline_value: Any = None
    after_value: Any = None
    proposed_value: Any = None

    @property
    def credited(self) -> bool:
        return self.kind in CREDIT_KINDS and self.fraction > 0


# --------------------------------------------------------------------------- utils


def _is_off(value, setting: TrackedSetting) -> bool:
    return value in setting.off_values if setting.off_values else False


def _norm_text(value) -> str:
    return str(value).strip().upper().replace("-", "_").replace(" ", "_").replace(
        "XSMALL", "X_SMALL"
    )


def _num(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _as_bool(value) -> bool | None:
    """Booleans arrive as real bools (Spark) or as 'true'/'false' text (REST)."""
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no"):
        return False
    return None


def _equal(a, b, setting: TrackedSetting) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(setting.proposed_value, bool) or isinstance(a, bool) or isinstance(b, bool):
        return _as_bool(a) == _as_bool(b)
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None:
        return na == nb
    return _norm_text(a) == _norm_text(b)


def _off_minutes(evidence: dict, setting: TrackedSetting) -> float:
    for key in setting.off_equivalent_keys:
        v = _num((evidence or {}).get(key))
        if v is not None and v > 0:
            return v * 60.0 if key.endswith("_hours") else v
    return float(setting.off_default)


def _position(value, setting: TrackedSetting, evidence: dict) -> float | None:
    """A comparable number for a setting value: a ladder index, or the number itself
    with "off" mapped to the off-equivalent minutes. None when not comparable."""
    if setting.valuation == "ladder" or setting.ladder:
        if value is None:
            return None
        ladder = [_norm_text(x) for x in setting.ladder]
        text = _norm_text(value)
        return float(ladder.index(text)) if text in ladder else None
    if _is_off(value, setting):
        return _off_minutes(evidence, setting)
    return _num(value)


def _fmt(value) -> str | None:
    """Values are stored as strings in the events table."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _phasing(evidence: dict) -> dict | None:
    ph = (evidence or {}).get("phasing")
    if isinstance(ph, str):
        try:
            ph = json.loads(ph)
        except ValueError:
            return None
    return ph if isinstance(ph, dict) else None


def curve_points(
    evidence: dict, setting: TrackedSetting, baseline_value
) -> list[tuple[float, float]]:
    """The finding's savings curve as ``[(position, monthly_usd), ...]``, sorted by
    position, or ``[]`` when the evidence carries none.

    Sources, in order: ``evidence.savings_curve`` (several points, PR F1b) and
    ``evidence.phasing`` (three points). Both are anchored at the baseline
    with a saving of 0 (the current setting saves nothing). Points whose value is not
    comparable (e.g. an unknown ladder size) are dropped.
    """
    ev = evidence or {}
    raw: list[tuple[Any, Any]] = []
    curve = ev.get("savings_curve")
    if isinstance(curve, str):
        try:
            curve = json.loads(curve)
        except ValueError:
            curve = None
    if isinstance(curve, list) and curve:
        for p in curve:
            if isinstance(p, dict):
                raw.append((p.get("value"), p.get("monthly_usd")))
    elif setting.uses_phasing:
        ph = _phasing(ev)
        if ph:
            raw.append((ph.get("current"), 0.0))
            raw.append((ph.get("recommended_step"), ph.get("step_savings_usd")))
            raw.append((ph.get("data_target"), ph.get("target_savings_usd")))
    if not raw:
        return []
    raw.append((baseline_value, 0.0))
    pts: dict[float, float] = {}
    for value, usd in raw:
        x = _position(value, setting, ev)
        y = _num(usd)
        if x is None or y is None:
            continue
        # Keep the larger saving if two sources name the same position.
        pts[x] = max(pts.get(x, 0.0), max(0.0, y))
    return sorted(pts.items())


def interpolate(points: list[tuple[float, float]], x: float) -> float:
    """Piecewise-linear interpolation, clamped to the end points."""
    if not points:
        return 0.0
    if x <= points[0][0]:
        return points[0][1]
    if x >= points[-1][0]:
        return points[-1][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        if x0 <= x <= x1:
            if x1 == x0:
                return max(y0, y1)
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


def ladder_fraction(k_done: float, k_proposed: float) -> float:
    """Share of a ``k_proposed``-step size cut achieved by ``k_done`` steps, when each
    step halves the rate: ``(1 - 2^-k_done) / (1 - 2^-k_proposed)``."""
    if k_proposed <= 0:
        return 0.0
    return (1.0 - 2.0 ** (-k_done)) / (1.0 - 2.0 ** (-k_proposed))


def _clamp(fraction: float) -> float:
    return round(min(1.0, max(0.0, fraction)), 4)


# ------------------------------------------------------------------------ the engine


def _proposed(setting: TrackedSetting, evidence: dict):
    """The full-credit proposal: the phasing data target when the rule phases this
    setting (the finding's estimate is the saving at the data target), else the
    evidence key, else the fixed value."""
    if setting.uses_phasing:
        ph = _phasing(evidence)
        if ph and ph.get("data_target") is not None:
            return ph["data_target"]
    if setting.proposed_key and (evidence or {}).get(setting.proposed_key) is not None:
        return evidence[setting.proposed_key]
    return setting.proposed_value


def _baseline(setting: TrackedSetting, baseline: dict):
    """(found, value). The evidence's own "from" value wins (it is what the finding
    was about); the tracker's observation of that day is the fallback."""
    ev = baseline or {}
    if setting.baseline_key and setting.baseline_key in ev:
        return True, ev[setting.baseline_key]
    observed = ev.get(OBSERVED_KEY)
    if isinstance(observed, dict) and setting.column in observed:
        return True, observed[setting.column]
    return False, None


def assess_change(
    setting: TrackedSetting | None,
    baseline: dict,
    last_open_evidence: dict,
    now: ConfigState,
) -> ChangeAssessment | None:
    """Assess one finding's resource against what we proposed.

    * ``baseline``: the finding's FIRST ledger evidence (its "from" value).
    * ``last_open_evidence``: the evidence of the last snapshot before the change (the
      proposal, the curve and the estimate it valued).
    * ``now``: the resource's current config.

    Returns None when nothing can be said (setting unknown, values not comparable),
    ``kind="unchanged"`` when the setting still equals the baseline, and otherwise
    the kind and fraction. Deletion wins over everything and needs ``delete_time``:
    absence from a query is never a deletion.
    """
    if now.delete_time is not None:
        return ChangeAssessment(
            kind=KIND_DELETED, fraction=1.0, method=METHOD_DELETION,
            changed_at=now.delete_time,
            setting=setting.column if setting else None,
        )
    if setting is None or setting.column not in now.values:
        return None
    found, before = _baseline(setting, baseline)
    if not found:
        return None
    ref = last_open_evidence or {}
    after = now.values[setting.column]
    proposed = _proposed(setting, ref)

    def result(kind: str, fraction: float, method: str) -> ChangeAssessment:
        return ChangeAssessment(
            kind=kind, fraction=_clamp(fraction), method=method,
            changed_at=now.change_time, setting=setting.column,
            baseline_value=_fmt(before), after_value=_fmt(after),
            proposed_value=_fmt(proposed),
        )

    if _equal(after, before, setting) or (
        _is_off(after, setting) and _is_off(before, setting)
    ):
        return result(KIND_UNCHANGED, 0.0, METHOD_BINARY)

    if setting.direction == "equals":
        if proposed is None:
            return None
        if _equal(after, proposed, setting):
            return result(setting.reached_kind, 1.0, METHOD_BINARY)
        return result(KIND_AWAY, 0.0, METHOD_BINARY)

    # Numeric / ladder directions. Off ranks above every number for a decrease.
    sign = 1.0 if setting.direction == "decrease" else -1.0
    b = _position(before, setting, ref)
    a = _position(after, setting, ref)
    if a is None or b is None:
        return None
    b, a = sign * b, sign * a
    if a >= b:  # not lower than where it started: a move away
        return result(KIND_AWAY, 0.0, METHOD_BINARY)

    if setting.valuation == "any_toward":
        return result(setting.reached_kind, 1.0, METHOD_BINARY)

    p_raw = _position(proposed, setting, ref) if proposed is not None else None
    if p_raw is None:
        return None
    p = sign * p_raw
    if b <= p:
        # The baseline was already at the proposal by this measure (e.g. "off" counts
        # as a short idle gap). Only reaching the proposal itself counts.
        if a <= p:
            return result(setting.reached_kind if a == p else KIND_BEYOND, 1.0, METHOD_BINARY)
        return result(KIND_TOWARD, 0.0, METHOD_LINEAR)
    if a == p:
        return result(setting.reached_kind, 1.0, METHOD_BINARY)
    if a < p:
        return result(KIND_BEYOND, 1.0, METHOD_BINARY)

    # A partial move toward the proposal.
    if setting.valuation == "binary":
        return result(KIND_TOWARD, 0.0, METHOD_BINARY)
    points = curve_points(ref, setting, before)
    if points:
        unsigned_p, unsigned_a = sign * p, sign * a
        full = interpolate(points, unsigned_p)
        if full > 0:
            return result(KIND_TOWARD, interpolate(points, unsigned_a) / full, METHOD_CURVE)
    if setting.valuation == "ladder":
        return result(KIND_TOWARD, ladder_fraction(b - a, b - p), METHOD_LADDER)
    return result(KIND_TOWARD, (b - a) / (b - p), METHOD_LINEAR)


def assess_for_rule(
    rule, baseline: dict, last_open_evidence: dict, now: ConfigState
) -> ChangeAssessment | None:
    """Call the rule's own ``assess_change`` override when it defines one, else the
    generic engine over its ``tracked_setting`` (None: deletion only). ``rule`` may be
    None for a rule no longer in the registry: deletion still counts."""
    override = getattr(rule, "assess_change", None) if rule is not None else None
    if callable(override):
        return override(baseline, last_open_evidence, now)
    setting = getattr(rule, "tracked_setting", None) if rule is not None else None
    return assess_change(setting, baseline, last_open_evidence, now)


def evidence_core(evidence: dict | None, setting: TrackedSetting | None) -> dict:
    """The compact subset of a finding's evidence the ledger keeps (``evidence_core``):
    the tracked-setting keys plus :data:`CORE_EVIDENCE_KEYS`."""
    ev = evidence or {}
    keys = list(CORE_EVIDENCE_KEYS)
    if setting is not None:
        keys.extend(setting.evidence_keys())
    return {k: ev[k] for k in dict.fromkeys(keys) if k in ev}


def to_utc_naive(value) -> datetime | None:
    """Timestamps arrive tz-aware (SQL connector), naive UTC (Spark) or as ISO text.
    Normalize to naive UTC so comparisons never mix the two."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    else:
        text = str(value).strip().replace("Z", "+00:00")
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt

