"""Renpho 0xaabb broadcast-only scale — advertisement variant.

Non-connectable: weight is read straight from the BLE advertisement. There is
no GATT connection, no handshake, no writes, and no impedance/body composition.
The display unit is *observed* from the advertisement (it cannot be set).
"""

from __future__ import annotations

from ..const import WEIGHT_KEY
from ..data import WeightUnit
from ..scale import AdvertisementScale
from .protocol import parse_broadcast

# Transient key used to hand the observed unit from ``_parse`` to
# ``_display_unit_for`` without leaking it into ``measurements``.
_DISPLAY_UNIT_ENTRY = "__display_unit__"


class RenphoAABBScale(AdvertisementScale):
    """Renpho broadcast-only variant (protocol prefix ``0xaabb``).

    Emits ``{"weight": kg}`` on each final (stabilized) reading and reports the
    scale's displayed unit via :attr:`display_unit`. Weight is always in
    kilograms regardless of what the scale's LCD shows.
    """

    _model_name = "Renpho Scale"

    def _parse(
        self, company_id: int, payload: bytearray
    ) -> dict[str, str | float | None] | None:
        reading = parse_broadcast(company_id, payload)
        if reading is None:
            return None
        return {
            WEIGHT_KEY: reading.weight_kg,
            _DISPLAY_UNIT_ENTRY: reading.display_unit,
        }

    def _display_unit_for(
        self, parsed: dict[str, str | float | None]
    ) -> WeightUnit | None:
        unit = parsed.pop(_DISPLAY_UNIT_ENTRY, None)
        return unit if isinstance(unit, WeightUnit) else None
