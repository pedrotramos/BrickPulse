"""Rule catalog — one file per cost-saving rule.

Modules here are auto-imported by ``finops_rules.registry.discover()``; each
registers its rule via the ``@register`` decorator. To add a rule, drop a new
module here (clone ``autotermination.py``) plus its ``../sql/<id>.sql.j2`` and a
``../config/thresholds.yaml`` block. Nothing else needs editing.
"""
