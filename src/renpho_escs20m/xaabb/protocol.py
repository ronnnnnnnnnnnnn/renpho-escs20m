"""Wire protocol for the 0xaabb broadcast-only Renpho variant.

Everything is carried in a non-connectable BLE advertisement's manufacturer
data (company id ``0xFFFF``). The 24-byte payload (indexed from the ``aa bb``
prefix)::

    0..1    prefix  aa bb
    2..7    scale MAC (== advertiser address)
    8..11   unix timestamp (little-endian)
    12..14  per-user id (ff ff ff until identified)
    15      status: bit0 committed, bits1-2 unit (kg=1/lb=2), bit5 stable,
            bit6 provisional/held
    16      reserved (0x00)
    17..18  weight, little-endian uint16, 0.01 kg (ALWAYS kg regardless of
            the display unit)
    19..21  fixed signature (byte 20 is a per-device constant)
    22..23  rolling counter

The scale does no impedance/BIA and no body composition — weight only.
"""

from __future__ import annotations

from typing import NamedTuple

from ..data import WeightUnit

# Manufacturer-data company id these advertisements use (generic / unassigned).
MANUFACTURER_ID = 0xFFFF

_MAGIC = b"\xaa\xbb"

# Need bytes up to index 18 (weight high byte).
_MIN_PAYLOAD_LEN = 19

# A reading is final only when BOTH the stable bit (0x20) and the committed
# bit (0x01) are set. Requiring the committed bit rejects the ``0x64``
# provisional/held frames, which set 0x20 but appear mid-settling.
_FINAL_MASK = 0x21

# Display unit lives in status byte 15, bits 1-2: (byte15 >> 1) & 0x07.
_UNIT_BY_CODE = {1: WeightUnit.KG, 2: WeightUnit.LB}


class BroadcastReading(NamedTuple):
    weight_kg: float
    display_unit: WeightUnit | None


def decode_display_unit(status_byte: int) -> WeightUnit | None:
    """Return the display unit the scale's LCD is showing, or ``None`` if the
    unit code is not one we recognise."""
    return _UNIT_BY_CODE.get((status_byte >> 1) & 0x07)


def is_final(status_byte: int) -> bool:
    return (status_byte & _FINAL_MASK) == _FINAL_MASK


def parse_broadcast(company_id: int, payload: bytearray) -> BroadcastReading | None:
    """Parse one manufacturer-data entry.

    Returns a :class:`BroadcastReading` only for a **final** frame of this
    protocol; ``None`` for anything else (wrong company id, wrong protocol
    magic, a short payload, or a non-final/settling frame).

    Device identity is the caller's concern: the advertisement is already
    filtered to the target address before this runs, and the payload's embedded
    MAC (bytes 2-7) is that same address — so it needs no re-check here.
    """
    if company_id != MANUFACTURER_ID:
        return None
    if len(payload) < _MIN_PAYLOAD_LEN:
        return None
    if bytes(payload[0:2]) != _MAGIC:
        return None
    status = payload[15]
    if not is_final(status):
        return None
    weight_kg = int.from_bytes(payload[17:19], "little") / 100
    if weight_kg <= 0:
        return None
    return BroadcastReading(round(weight_kg, 2), decode_display_unit(status))
