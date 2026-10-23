"""Table-name resolver for system table references.

Rule SQL references a system table as ``{{ tbl('schema','table') }}``. The resolver
turns that call into ``{catalog}.schema.table``: the ``system`` catalog by default, or
a ``system``-shaped mirror when ``catalog`` points at one.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TableResolver:
    """Callable used in templates as ``tbl('schema','table')`` — see module docstring.

    Returns the standard nested namespace path ``{catalog}.schema.table``.
    """

    catalog: str = "system"

    @classmethod
    def nested(cls, catalog: str = "system") -> TableResolver:
        return cls(catalog=catalog)

    # -- rendering -------------------------------------------------------------
    def __call__(self, schema: str, table: str) -> str:
        return f"{self.catalog}.{schema}.{table}"

    # -- render context --------------------------------------------------------
    def render_context(self) -> dict:
        """Jinja variables this resolver contributes: ``tbl``."""
        return {"tbl": self}
