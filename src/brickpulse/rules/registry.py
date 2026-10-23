"""Rule registration and discovery.

Rules register themselves with the ``@register`` decorator. ``discover()`` walks
the ``catalog`` package and imports every module, which triggers those
decorators — so *adding a file* is all it takes to register a new rule. No
central list, no shared file to edit, no merge conflicts.
"""

from __future__ import annotations

import importlib
import pkgutil

from .contract import Rule

_REGISTRY: dict[str, Rule] = {}
_DISCOVERED = False


def register(cls: type[Rule]) -> type[Rule]:
    """Class decorator: instantiate the rule and add it to the registry."""
    instance = cls()
    if not instance.id:
        raise ValueError(f"{cls.__name__} must define a non-empty `id`")
    if instance.id in _REGISTRY:
        raise ValueError(f"Duplicate rule id: {instance.id!r}")
    _REGISTRY[instance.id] = instance
    return cls


def discover() -> None:
    """Import every module under ``catalog/`` exactly once so decorators fire."""
    global _DISCOVERED
    if _DISCOVERED:
        return
    from . import catalog  # local import to avoid a cycle at package import

    for module in pkgutil.iter_modules(catalog.__path__):
        importlib.import_module(f"{catalog.__name__}.{module.name}")
    _DISCOVERED = True


def list_rules() -> list[Rule]:
    """All registered rules, discovered on first call."""
    discover()
    return list(_REGISTRY.values())


def get_rule(rule_id: str) -> Rule | None:
    discover()
    return _REGISTRY.get(rule_id)


def get_enabled_rules(thresholds: dict) -> list[Rule]:
    """Rules that are enabled given the thresholds config.

    A rule is enabled unless its config block sets ``enabled: false`` (or it is
    ``default_enabled=False`` and has no explicit ``enabled: true``)."""
    enabled: list[Rule] = []
    for rule in list_rules():
        block = thresholds.get(rule.id, {})
        flag = block.get("enabled")
        if flag is None:
            flag = rule.default_enabled
        if flag:
            enabled.append(rule)
    return enabled
