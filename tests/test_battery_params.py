"""Rena hjälpare för serverns battery_params (egna batterivärden)."""

from custom_components.wolta import battery_params as bp


def test_is_active():
    assert bp.is_active(None) is False
    assert bp.is_active({}) is False
    assert bp.is_active({"active": False}) is False
    assert bp.is_active({"active": "yes"}) is False
    assert bp.is_active({"active": True}) is True


def test_own_value():
    assert bp.own_value({"capacity": {"own": 18.0}}, "capacity") == 18.0
    assert bp.own_value({"capacity": {"own": None}}, "capacity") is None
    assert bp.own_value(None, "capacity") is None
    assert bp.own_value({"power": {"own": 3.0}}, "capacity") is None
    assert bp.own_value({"capacity": "x"}, "capacity") is None


def test_quantity():
    assert bp.quantity({"eff": {"value": 0.9}}, "eff") == {"value": 0.9}
    assert bp.quantity({"eff": 1}, "eff") is None
    assert bp.quantity(None, "eff") is None


def test_has_dated_own_rows():
    assert bp.has_dated_own_rows(
        {"own_timeline": {"reserve": [{"valid_from": None, "value": 10}]}}, "reserve") is False
    assert bp.has_dated_own_rows(
        {"own_timeline": {"reserve": [{"valid_from": "2026-11-01", "value": 10}]}}, "reserve") is True
    assert bp.has_dated_own_rows({}, "reserve") is False
    assert bp.has_dated_own_rows(None, "reserve") is False
    assert bp.has_dated_own_rows({"own_timeline": {"reserve": "x"}}, "reserve") is False


def _q(value, state, source):
    return {"capacity": {"value": value, "state": state, "source": source}}


def test_wolta_value_kind():
    def with_measured(m):
        return {"capacity": {"value": 10.0, "state": "ok", "source": "own", "measured": m}}

    assert bp.wolta_value_kind(
        with_measured({"value": 9.4, "state": "measured", "source": "soc"}), "capacity") == "measured"
    assert bp.wolta_value_kind(
        with_measured({"value": 9.4, "state": "measured", "source": "datasheet"}), "capacity") == "datasheet"
    assert bp.wolta_value_kind(
        with_measured({"value": 9.4, "state": "assumed", "source": "assumed"}), "capacity") == "assumed"
    assert bp.wolta_value_kind(
        with_measured({"value": 9.4, "state": "datasheet", "source": "x"}), "capacity") == "datasheet"
    assert bp.wolta_value_kind(
        with_measured({"value": None, "state": "measured", "source": "soc"}), "capacity") is None
    assert bp.wolta_value_kind(
        with_measured({"value": 9.4, "state": "missing", "source": "soc"}), "capacity") is None
    assert bp.wolta_value_kind(with_measured(None), "capacity") is None
    assert bp.wolta_value_kind({"capacity": {}}, "capacity") is None
    assert bp.wolta_value_kind(None, "capacity") is None
