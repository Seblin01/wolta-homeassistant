"""`battery_status` läses på tre ställen (config_flow ×2, coordinator, sensor) – EN konstant
i const.py (`KEY_BATTERY_STATUS`) i st f fyra literaler, så en omdöpning i backend byts på
ett ställe. Vakten läser källfilerna: en literal som smyger tillbaka är ett rött test."""
from __future__ import annotations

from pathlib import Path

from custom_components.wolta.const import KEY_BATTERY_STATUS

_COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "wolta"


def test_konstanten():
    assert KEY_BATTERY_STATUS == "battery_status"


def test_ingen_literal_utanfor_const():
    for name in ("config_flow.py", "coordinator.py", "sensor.py"):
        src = (_COMPONENT / name).read_text(encoding="utf-8")
        assert '"battery_status"' not in src, f"{name} läser stämpeln med en literal"
