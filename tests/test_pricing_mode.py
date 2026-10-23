import pytest

from brickpulse.rules.price_basis import probe_capabilities
from brickpulse.rules.pricing import validate_pricing_mode
from brickpulse.server.config import Settings


@pytest.mark.parametrize("mode", ["auto", "list", "account"])
def test_valid_modes(mode):
    assert validate_pricing_mode(mode.upper()) == mode
    assert Settings(pricing_mode=mode).pricing_mode == mode


def test_invalid_and_retired_modes_fail():
    for mode in ("effective", "mystery"):
        with pytest.raises(ValueError):
            validate_pricing_mode(mode)


def test_list_never_reads_account_prices():
    calls = []

    def query(sql):
        calls.append(sql)
        return []

    caps = probe_capabilities(query, "system", "USD", 30, "list")
    assert not caps.contract_prices
    assert len(calls) == 1
    assert "cloud_infra_cost" in calls[0]


def test_auto_uses_account_prices_when_available():
    def query(sql):
        return [1] if "account_prices" in sql else []

    caps = probe_capabilities(query, "system", "USD", 30, "auto")
    assert caps.contract_prices
    assert not caps.cloud_costs


@pytest.mark.parametrize("failure", ["empty", "denied"])
def test_account_mode_fails_closed(failure):
    def query(sql):
        if "account_prices" in sql:
            if failure == "denied":
                raise PermissionError("not granted")
            return []
        return []

    with pytest.raises(RuntimeError, match="account_prices"):
        probe_capabilities(query, "system", "USD", 30, "account")
