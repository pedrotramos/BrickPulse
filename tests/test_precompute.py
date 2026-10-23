import json
from unittest.mock import MagicMock

from brickpulse.server.config import Settings
from brickpulse.server.data import precompute_source as module
from brickpulse.server.data.bounded_cache import BoundedCache
from brickpulse.server.identity import Scope


def test_optional_discovery_recovers_after_failed_probe(monkeypatch):
    monkeypatch.setattr(module, "_TABLE_COLUMNS", BoundedCache(300, 5))
    conn = MagicMock()
    cursor = conn.__enter__.return_value.cursor.return_value.__enter__.return_value
    cursor.description = [("finding_key",), ("affected_spend_scope",), ("savings_composition",)]
    calls = []
    def connect():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("temporary warehouse failure")
        return conn
    source = module.PrecomputeRecommendationSource(Settings(), connect)
    assert source._optional_present("table") == []
    assert set(source._optional_present("table")) == {"finding_key", "affected_spend_scope", "savings_composition"}
    assert source._optional_present("table")
    assert len(calls) == 2


def test_precompute_cache_separates_host_warehouse_and_scope(monkeypatch):
    monkeypatch.setattr(module, "_CACHE", BoundedCache(300, 20, len))
    calls = []
    def query(self, scope):
        calls.append((self.settings.server_hostname, self.settings.warehouse_id, scope.workspace_ids))
        return [str(len(calls))]
    monkeypatch.setattr(module.PrecomputeRecommendationSource, "_query", query)
    source = module.PrecomputeRecommendationSource(Settings(server_hostname="one", warehouse_id="a"), None)
    assert source.load(Scope(False, ["1"])) == ["1"]
    assert source.load(Scope(False, ["1"])) == ["1"]
    assert source.load(Scope(False, ["2"])) == ["2"]
    other = module.PrecomputeRecommendationSource(Settings(server_hostname="two", warehouse_id="a"), None)
    assert other.load(Scope(False, ["1"])) == ["3"]
    other.settings.warehouse_id = "b"
    assert other.load(Scope(False, ["1"])) == ["4"]


def test_persisted_json_retains_composition_and_message_params(source):
    rec = source.load(Scope(all_workspaces=True))[0]
    row = rec.model_dump(mode="json")
    row["evidence"] = json.dumps(row["evidence"])
    restored = module.PrecomputeRecommendationSource._to_recommendation(row, module._COLUMNS + module._OPTIONAL_COLUMNS)
    assert restored.affected_spend_scope == rec.affected_spend_scope
    assert restored.savings_composition == rec.savings_composition
    assert restored.evidence == rec.evidence
