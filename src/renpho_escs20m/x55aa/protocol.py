"""Wire protocol for the 0x55aa GATT variant (LeFu hardware).

Frame layout, checksum-verified against live captures::

    55 AA | type | flag | len | payload[len] | checksum

``checksum`` is the low byte of the sum of every preceding byte. ``flag`` has
only ever been observed as 0x00.

Measurement payloads::

    type 0x15 (live, 12 bytes)
        [0:4]   weight, uint32 big-endian, units of 0.01 kg
        [4:6]   resistance, uint16 big-endian, ohms
        [6:8]   zero
        [8:10]  secondary value, uint16 big-endian
        [10:12] zero

    type 0x14 (stable, 7 bytes)
        [0]     status
        [1:5]   weight, uint32 big-endian, units of 0.01 kg
        [5:7]   resistance, uint16 big-endian, ohms

Types 0x11 and 0x12 carry a 5-byte status payload that is not decoded.

A weigh-in emits *several* stable frames as the reading converges; the last
one matches the scale's display. Resistance reads 0 until the bioimpedance
pass completes, which happens seconds after the weight settles and after a
pause in the notification stream. :func:`aggregate_session` reduces one
weigh-in's readings to the single result the scale settled on.

The advertisement carries the device MAC in forward byte order and is used
only for detection; measurements arrive over GATT.
"""

from __future__ import annotations

from collections.abc import Sequence
from statistics import median
from typing import NamedTuple

MANUFACTURER_ID = 0x1A10

SUPPORTED_COMPANY_IDS = frozenset([MANUFACTURER_ID])

ADV_MAC_SLICE = slice(4, 10)
_MIN_ADV_LEN = ADV_MAC_SLICE.stop

_MAGIC = b"\x55\xaa"
_HEADER_LEN = 5
_TYPE_LIVE = 0x15
_TYPE_STABLE = 0x14
_LEN_LIVE = 10
_LEN_STABLE = 7

_WEIGHT_SCALE = 100.0

# Live frames report a resistance while weight is still ramping; it bears no
# relation to the settled value and must not reach the caller.
_WEIGHT_TOLERANCE_RATIO = 0.01
_WEIGHT_TOLERANCE_FLOOR = 0.5


class Frame(NamedTuple):
    type: int
    flag: int
    payload: bytes


class Reading(NamedTuple):
    weight_kg: float
    resistance: int | None
    secondary: int | None
    final: bool
    status: int | None = None


def checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def is_advertisement(payload: bytes, address: str | None = None) -> bool:
    """Return True if ``payload`` has the 0x55aa advertisement shape.

    When ``address`` is a real MAC, the forward-order echo at bytes 4-10 must
    match it.
    """
    if len(payload) < _MIN_ADV_LEN:
        return False
    if address:
        octets = address.split(":")
        if len(octets) == 6:
            try:
                mac = bytes(int(o, 16) for o in octets)
            except ValueError:
                return True
            return payload[ADV_MAC_SLICE] == mac
    return True


def iter_frames(buffer: bytearray) -> list[Frame]:
    """Extract every complete, valid frame from ``buffer``, consuming it.

    Notifications do not align with frame boundaries, so this resynchronises
    on the magic and leaves a partial trailing frame for the next call.
    """
    frames: list[Frame] = []

    while True:
        start = buffer.find(_MAGIC)
        if start == -1:
            del buffer[: max(0, len(buffer) - 1)]
            return frames
        if start:
            del buffer[:start]

        if len(buffer) < _HEADER_LEN:
            return frames

        total = _HEADER_LEN + buffer[4] + 1
        if len(buffer) < total:
            return frames

        raw = bytes(buffer[:total])
        if checksum(raw[:-1]) != raw[-1]:
            del buffer[:2]
            continue

        del buffer[:total]
        frames.append(Frame(raw[2], raw[3], raw[_HEADER_LEN:-1]))


def parse_reading(frame: Frame) -> Reading | None:
    """Decode a measurement frame; ``None`` for any other frame type."""
    payload = frame.payload

    if frame.type == _TYPE_LIVE and len(payload) >= _LEN_LIVE:
        return Reading(
            weight_kg=round(int.from_bytes(payload[0:4], "big") / _WEIGHT_SCALE, 2),
            resistance=int.from_bytes(payload[4:6], "big") or None,
            secondary=int.from_bytes(payload[8:10], "big") or None,
            final=False,
        )

    if frame.type == _TYPE_STABLE and len(payload) >= _LEN_STABLE:
        return Reading(
            weight_kg=round(int.from_bytes(payload[1:5], "big") / _WEIGHT_SCALE, 2),
            resistance=int.from_bytes(payload[5:7], "big") or None,
            secondary=None,
            final=True,
            status=payload[0],
        )

    return None


def aggregate_session(readings: Sequence[Reading]) -> Reading | None:
    """Reduce one weigh-in's readings to the result the scale settled on.

    Weight comes from the last stable frame, which is what the scale
    displays, falling back to live frames for a weigh-in that produced none.
    Resistance comes from the most recent stable frame carrying one; failing
    that, from the median of live-frame resistances recorded at essentially
    the final weight.
    """
    if not readings:
        return None

    finals = [r for r in readings if r.final]
    lives = [r for r in readings if not r.final]
    settled = finals[-1] if finals else lives[-1]
    weight = settled.weight_kg

    resistance = next((r.resistance for r in reversed(finals) if r.resistance), None)
    if resistance is None:
        tolerance = max(_WEIGHT_TOLERANCE_FLOOR, weight * _WEIGHT_TOLERANCE_RATIO)
        near = [
            r.resistance
            for r in lives
            if r.resistance and abs(r.weight_kg - weight) <= tolerance
        ]
        if near:
            resistance = int(median(near))

    return Reading(
        weight_kg=weight,
        resistance=resistance,
        secondary=next((r.secondary for r in reversed(lives) if r.secondary), None),
        final=True,
        status=settled.status,
    )
