"""The stable id of one rule's finding on one resource.

``finding_key`` = ``sha256(rule_id|workspace_id|resource_type|resource_id)[:32]``,
lowercase hex. Unlike ``recommendation_id`` it has no ``snapshot_date`` in it, so a
link built from it still opens the finding after the nightly recompute, and the
"Saved so far" tracker can follow one finding across snapshots. It is one per
rule x resource; ``group_id`` is one per resource and doesn't say which rule.
"""

from __future__ import annotations

import hashlib


def finding_key(rule_id: str, workspace_id: str, resource_type: str, resource_id: str) -> str:
    """Snapshot-independent id of one rule's finding on one resource.

    The SQL equivalent is
    ``substr(sha2(concat_ws('|', rule_id, workspace_id, resource_type, resource_id), 256), 1, 32)``.
    """
    raw = f"{rule_id}|{workspace_id}|{resource_type}|{resource_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]
