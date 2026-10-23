"""Thin Databricks SQL connection helper.

Opens a ``databricks-sql-connector`` connection to a SQL warehouse using an
access token (the user's, in OBO; the SP's, in prod). Identifiers that we
interpolate (catalog/table) are regex-validated; all user-derived values go
through parameter binding or server-side-validated numeric scoping.
"""

from __future__ import annotations

import re

from ..config import Settings

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_identifier(name: str) -> str:
    """Validate a single SQL identifier part (schema/table/column)."""
    if not _IDENTIFIER_RE.match(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return name


def qualified_table(fqn: str) -> str:
    """Validate and backtick-quote a ``catalog.schema.table`` name."""
    parts = fqn.split(".")
    if not (1 <= len(parts) <= 3):
        raise ValueError(f"Invalid table name: {fqn!r}")
    return ".".join(f"`{validate_identifier(p)}`" for p in parts)


def http_path(warehouse: str) -> str:
    return warehouse if warehouse.startswith("/sql/") else f"/sql/1.0/warehouses/{warehouse}"


# --- pyarrow-absent fallback ------------------------------------------------
# databricks-sql-connector >=4.0 returns native ARRAY/MAP/STRUCT columns as
# Python objects ONLY when pyarrow is installed; without it, those columns come
# back as JSON *strings*. The rule/viz layer expects native lists/dicts (e.g.
# queries_by_hour), so when pyarrow is missing we transparently JSON-decode the
# cells the server declares as complex types — and ONLY those, so the app's own
# to_json() STRING columns (by_sku, by_day, ...) are left exactly as-is.
try:  # pragma: no cover - environment probe
    import pyarrow as _pyarrow  # noqa: F401

    _HAS_PYARROW = True
except Exception:  # pragma: no cover
    _HAS_PYARROW = False

import json as _json

_COMPLEX_TYPES = frozenset({"array", "map", "struct"})


class _DecodingCursor:
    """Wraps a cursor, JSON-decoding complex-typed cells on fetch."""

    def __init__(self, cur):
        self._cur = cur

    def __getattr__(self, name):
        return getattr(self._cur, name)

    def __enter__(self):
        self._cur.__enter__()
        return self

    def __exit__(self, *a):
        return self._cur.__exit__(*a)

    def _complex_idx(self):
        desc = self._cur.description or []
        return [i for i, d in enumerate(desc) if (d[1] or "").lower() in _COMPLEX_TYPES]

    def _decode(self, rows, idx):
        if not idx:
            return rows
        out = []
        for r in rows:
            r = list(r)
            for i in idx:
                v = r[i]
                if isinstance(v, str):
                    try:
                        r[i] = _json.loads(v)
                    except Exception:
                        pass
            out.append(tuple(r))
        return out

    def fetchall(self):
        rows = self._cur.fetchall()
        return self._decode(rows, self._complex_idx())

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        return self._decode([row], self._complex_idx())[0]


class _DecodingConnection:
    """Wraps a connection so every cursor decodes complex-typed columns."""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, *a):
        return self._conn.__exit__(*a)

    def cursor(self, *a, **k):
        return _DecodingCursor(self._conn.cursor(*a, **k))


def get_connection(settings: Settings, access_token: str | None):
    """Open a SQL connection to the warehouse.

    Two auth paths:
    * **Explicit token** (local dev `DATABRICKS_TOKEN`): connect with it directly.
    * **Deployed (no token)**: authenticate as the app's own service principal via
      the Databricks SDK, which auto-reads the `DATABRICKS_HOST` / `DATABRICKS_CLIENT_ID`
      / `DATABRICKS_CLIENT_SECRET` that Databricks Apps inject. This is the reliable
      in-app auth for reading the precomputed recommendation table.
    """
    from databricks import sql

    from ..logging import get_logger

    log = get_logger(__name__)

    if not settings.warehouse_id:
        raise RuntimeError("DATABRICKS_WAREHOUSE_ID is not set")
    path = http_path(settings.warehouse_id)
    # Fail fast instead of hanging forever if the warehouse endpoint is unreachable.
    timeouts = {"_socket_timeout": 40}

    if access_token:  # local dev / explicit PAT, or the forwarded OBO user token
        if not settings.server_hostname:
            raise RuntimeError("DATABRICKS_SERVER_HOSTNAME/DATABRICKS_HOST is not set")
        log.info("SQL connect via access_token host=%s http_path=%s", settings.server_hostname, path)
        conn = sql.connect(
            server_hostname=settings.server_hostname,
            http_path=path,
            access_token=access_token,
            **timeouts,
        )
        return conn if _HAS_PYARROW else _DecodingConnection(conn)

    # Deployed: connect as the app service principal via unified SDK auth. Use the
    # SDK Config's own header factory (cfg.authenticate) — the same pattern the
    # working reference app uses — which resolves whatever auth the Apps runtime
    # injects (SP OAuth) rather than forcing a specific M2M flow.
    from databricks.sdk.core import Config

    cfg = Config()  # picks up host + SP client credentials from the Apps runtime
    host = settings.server_hostname or (cfg.host or "").replace("https://", "").replace("http://", "").rstrip("/")
    if not host:
        raise RuntimeError("Workspace host not resolvable (DATABRICKS_HOST unset)")
    log.info("SQL connect via service principal host=%s http_path=%s", host, path)
    conn = sql.connect(
        server_hostname=host,
        http_path=path,
        credentials_provider=lambda: cfg.authenticate,
        **timeouts,
    )
    return conn if _HAS_PYARROW else _DecodingConnection(conn)
