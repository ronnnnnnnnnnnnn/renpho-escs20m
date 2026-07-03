"""Shared data types for the Renpho scale library.

These are transport- and protocol-agnostic: every scale class produces a
:class:`ScaleData`, accepts a :class:`WeightUnit`, and is configured with a
:class:`BluetoothScanningMode`.
"""

from __future__ import annotations

import dataclasses
from enum import IntEnum, StrEnum


class BluetoothScanningMode(StrEnum):
    PASSIVE = "passive"
    ACTIVE = "active"


class WeightUnit(IntEnum):
    """
    Display weight unit shown on the scale.

    Values are library-level identifiers, not the raw bytes exchanged over
    BLE. The QN protocol's command/response encodes units as a single byte
    with the following mapping:

    ====== ================================================================
     byte   meaning
    ====== ================================================================
    ``1``   kilograms
    ``2``   pounds
    ``8``   stones + pounds (weight shown as e.g. ``12 st 4.6 lb``)
    ``16``  stones only (weight shown as e.g. ``12.6 st``)
    ====== ================================================================

    :class:`WeightUnit` values map onto those bytes via
    :func:`renpho_escs20m.qn.protocol.build_unit_update_command`.
    """

    KG = 0
    LB = 1
    ST = 2
    ST_LB = 3


@dataclasses.dataclass
class ScaleData:
    """
    Parsed scale measurement payload.

    Attributes:
        name: Advertised name of the scale.
        address: Bluetooth address of the scale.
        display_unit: Unit to be used for the scale display.
        measurements: Parsed measurement values. Supported keys are
            ``weight`` (kg) and, depending on the variant, ``body_fat`` (%)
            and ``resistance_1``/``resistance_2`` when reported.
    """

    name: str = ""
    address: str = ""
    display_unit: WeightUnit = WeightUnit.KG
    measurements: dict[str, str | float | None] = dataclasses.field(
        default_factory=dict
    )
