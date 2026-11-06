import pytest

from brickpulse.server.data.genie_source import _rollup


def _data():
    return {
        "user": [
            {
                "workspace_id": "1",
                "workspace_name": "alpha",
                "principal": "ana@example.com",
                "free_dbus": 3.0,
                "paid_dbus": 7.0,
                "spend_usd": 0.5,
                "active_days": 4,
                "last_active_date": "2026-11-01",
            }
        ],
        "surface": [
            {
                "workspace_id": "1",
                "surface": "GENIE_CODE",
                "channel": "UI",
                "is_free": False,
                "dbus": 7.0,
                "spend_usd": 0.5,
                "users": 1,
            },
            {
                "workspace_id": "1",
                "surface": "GENIE_CODE",
                "channel": "UI",
                "is_free": True,
                "dbus": 3.0,
                "spend_usd": 0.0,
                "users": 1,
            },
        ],
        "space": [
            {
                "workspace_id": "1",
                "space_id": "s1",
                "dbus": 4.0,
                "api_dbus": 1.0,
                "spend_usd": 0.25,
                "users": 1,
            }
        ],
        "daily": [
            {
                "workspace_id": "1",
                "usage_date": "2026-11-01",
                "free_dbus": 3.0,
                "paid_dbus": 7.0,
                "spend_usd": 0.5,
                "users": 1,
            }
        ],
    }


def test_monthly_dbus_scale_with_window():
    # A 15-day window is extrapolated to a 30-day month, so every monthly figure is 2x.
    costs = _rollup(_data(), lookback_days=15, unavailable=False)
    user = costs.top_users[0]
    assert (user.monthly_free_dbus, user.monthly_paid_dbus, user.monthly_dbus) == (6.0, 14.0, 20.0)
    assert costs.monthly_dbus == 20.0 == costs.total_dbus * 2
    assert costs.free_tier.monthly_free_dbus == 6.0
    assert costs.free_tier.monthly_paid_dbus == 14.0
    assert costs.workspaces[0].monthly_dbus == 20.0
    assert costs.spaces[0].monthly_dbus == 8.0
    assert costs.daily[0].dbus == 10.0


def test_thirty_day_window_is_not_rescaled():
    costs = _rollup(_data(), lookback_days=30, unavailable=False)
    assert costs.monthly_dbus == costs.total_dbus == 10.0
    assert costs.monthly_spend_usd == costs.total_spend_usd == 0.5


def test_free_and_paid_surface_stay_separate():
    costs = _rollup(_data(), lookback_days=15, unavailable=False)
    by_tier = {s.is_free: s for s in costs.surfaces}
    assert set(by_tier) == {True, False}
    assert by_tier[False].surface_label == "Genie Code"
    assert by_tier[False].monthly_dbus == 14.0
    assert by_tier[False].monthly_spend_usd == 1.0
    assert by_tier[True].monthly_spend_usd == 0.0


@pytest.mark.parametrize("days", [3, 15, 30])
def test_unavailable_has_no_figures(days):
    costs = _rollup({}, lookback_days=days, unavailable=True)
    assert costs.unavailable and costs.monthly_dbus == 0.0


def test_free_tier_equivalent_uses_the_paid_rate_of_the_same_surface():
    # Paid Genie Code UI: 7 DBU cost $0.50 -> ~$0.0714/DBU. The 3 free DBUs are worth $0.21.
    costs = _rollup(_data(), lookback_days=30, unavailable=False)
    free = next(s for s in costs.surfaces if s.is_free)
    paid = next(s for s in costs.surfaces if not s.is_free)
    assert free.monthly_equivalent_usd == pytest.approx(3 * 0.5 / 7, abs=0.01)
    assert paid.monthly_equivalent_usd == 0.0
    # Billed spend is never inflated by the estimate.
    assert costs.monthly_spend_usd == 0.5
    assert costs.free_tier.monthly_free_equivalent_usd == free.monthly_equivalent_usd
    assert costs.monthly_total_value_usd == pytest.approx(0.5 + 3 * 0.5 / 7, abs=0.01)


def test_free_slice_without_paid_usage_falls_back_to_the_overall_rate():
    data = _data()
    data["surface"].append(
        {"workspace_id": "1", "surface": "GENIE_ONE", "channel": "UI", "is_free": True,
         "dbus": 14.0, "spend_usd": 0.0, "users": 1}
    )
    costs = _rollup(data, lookback_days=30, unavailable=False)
    one = next(s for s in costs.surfaces if s.surface == "GENIE_ONE")
    assert one.monthly_equivalent_usd == pytest.approx(14 * 0.5 / 7, abs=0.01)


def test_nothing_paid_means_no_invented_price():
    data = _data()
    data["surface"] = [s for s in data["surface"] if s["is_free"]]
    costs = _rollup(data, lookback_days=30, unavailable=False)
    assert costs.free_tier.monthly_free_equivalent_usd == 0.0


def _rich():
    """Two users in two workspaces, valued per surface and channel."""
    return {
        "surface": [
            {"workspace_id": "1", "surface": "GENIE_CODE", "channel": "UI", "is_free": False,
             "dbus": 100.0, "spend_usd": 10.0, "users": 2},       # $0.10/DBU
            {"workspace_id": "1", "surface": "GENIE_AGENTS", "channel": "UI", "is_free": False,
             "dbus": 100.0, "spend_usd": 5.0, "users": 2},        # $0.05/DBU
            {"workspace_id": "1", "surface": "GENIE_CODE", "channel": "UI", "is_free": True,
             "dbus": 50.0, "spend_usd": 0.0, "users": 2},
            {"workspace_id": "1", "surface": "GENIE_AGENTS", "channel": "UI", "is_free": True,
             "dbus": 40.0, "spend_usd": 0.0, "users": 2},
        ],
        "user": [
            {"workspace_id": "1", "principal": "a", "free_dbus": 60.0, "paid_dbus": 100.0,
             "spend_usd": 10.0,
             "free_by_surface": [{"surface": "GENIE_CODE", "channel": "UI", "dbus": 30.0},
                                 {"surface": "GENIE_AGENTS", "channel": "UI", "dbus": 30.0}]},
            {"workspace_id": "2", "principal": "b", "free_dbus": 30.0, "paid_dbus": 100.0,
             "spend_usd": 5.0,
             "free_by_surface": [{"surface": "GENIE_CODE", "channel": "UI", "dbus": 20.0},
                                 {"surface": "GENIE_AGENTS", "channel": "UI", "dbus": 10.0}]},
        ],
        "space": [
            {"workspace_id": "1", "space_id": "s", "dbus": 140.0, "free_dbus": 40.0,
             "spend_usd": 5.0, "users": 2},
        ],
        "daily": [],
    }


def test_rows_are_valued_at_the_rate_of_their_own_surface():
    costs = _rollup(_rich(), lookback_days=30, unavailable=False)
    by_user = {u.principal: u for u in costs.top_users}
    # a: 30 x $0.10 + 30 x $0.05; b: 20 x $0.10 + 10 x $0.05
    assert by_user["a"].monthly_free_equivalent_usd == 4.5
    assert by_user["b"].monthly_free_equivalent_usd == 2.5
    assert by_user["a"].monthly_total_value_usd == 14.5
    by_ws = {w.workspace_id: w for w in costs.workspaces}
    assert by_ws["1"].monthly_free_equivalent_usd == 4.5
    assert by_ws["2"].monthly_free_equivalent_usd == 2.5
    assert by_ws["1"].monthly_total_value_usd == 14.5
    # Spaces only exist on GENIE_AGENTS, so they use that surface's rate.
    space = costs.spaces[0]
    assert space.monthly_free_equivalent_usd == 2.0
    assert (space.monthly_free_dbus, space.monthly_paid_dbus) == (40.0, 100.0)
    assert space.monthly_total_value_usd == 7.0


def test_users_workspaces_and_surfaces_agree_on_the_free_value():
    costs = _rollup(_rich(), lookback_days=15, unavailable=False)
    from_users = sum(u.monthly_free_equivalent_usd for u in costs.top_users)
    from_workspaces = sum(w.monthly_free_equivalent_usd for w in costs.workspaces)
    from_surfaces = sum(s.monthly_equivalent_usd for s in costs.surfaces)
    assert from_users == from_workspaces == from_surfaces == costs.free_tier.monthly_free_equivalent_usd


def test_snapshot_rows_without_the_free_breakdown_use_the_overall_rate():
    data = _rich()
    for row in data["user"]:
        del row["free_by_surface"]
    costs = _rollup(data, lookback_days=30, unavailable=False)
    overall = 15.0 / 200.0
    a = next(u for u in costs.top_users if u.principal == "a")
    assert a.monthly_free_equivalent_usd == pytest.approx(60.0 * overall, abs=0.01)


def test_cut_space_list_reports_the_uncut_count():
    data = _rich()
    data["space"] = [
        {"workspace_id": "1", "space_id": f"s{i}", "dbus": 1.0, "free_dbus": 0.0,
         "spend_usd": 0.1, "users": 1}
        for i in range(1200)
    ]
    costs = _rollup(data, lookback_days=30, unavailable=False)
    assert costs.space_total == 1200 and len(costs.spaces) == 1000
