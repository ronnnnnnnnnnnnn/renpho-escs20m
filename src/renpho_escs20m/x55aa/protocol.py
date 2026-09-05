"""Wire protocol for the ``0x55aa`` GATT variant (LeFu hardware).

Frame layout, checksum-verified against captures from four independent
units (ES-CS20MB1, R-A012, ES-26BB-B, ESCS20MB2)::

    55 AA | command | length (uint16 BE) | payload[length] | checksum

``checksum`` is the low byte of the sum of every preceding byte; the
total frame size is ``6 + length``.

Frame ``0x14`` — measurement (7-byte payload)
    ========  =========================================================
    ``[0]``   status. Low nibble: ``0`` settling, ``1`` final; any other
              value is unclassified. Bit 4 (``0x10``) set: the scale is in its
              zero-current (pregnancy) mode — a setting the Renpho app
              stores on the scale, under which the bioimpedance pass is
              skipped; ``0x10``/``0x11`` are ordinary settling/final
              frames with resistance 0 (R-A016, capture + probe run).
    ``[1:5]`` weight, uint32 big-endian, 0.01 kg
    ``[5:7]`` resistance, uint16 big-endian, ohms — zero on every
              settling frame, populated on the final after the scale's
              bioimpedance pass.
    ========  =========================================================

Frame ``0x15`` — stored (offline) record (12- or 14-byte payload)
    ``[0:4]`` weight, ``[4:6]`` resistance, ``[6:10]`` seconds since the
    weigh-in, ``[10:12]`` reserved; the 14-byte form adds ``[12]`` = the
    zero-current flag of that weigh-in (R-A016).

Command ``0x90`` — display unit (4-byte payload ``[unit, 0, mode, 0]``)
    ``unit`` 1=kg 2=lb 3=st:lb 4=st. ``mode`` 0 leaves the scale's stored
    zero-current setting untouched (verified on the R-A016: the unit
    changed, the mode stayed) and is what the app itself sends in captured
    R-A012 sessions; 1/2 would overwrite that setting, so the library
    never sends them.
"""

from __future__ import annotations

from typing import NamedTuple

from ..data import WeightUnit

MANUFACTURER_ID = 0x1A10

SUPPORTED_COMPANY_IDS = frozenset([MANUFACTURER_ID])

ADV_PREFIX = b"\x00\x04"
ADV_MODEL_SLICE = slice(2, 4)  # model identifier, 16-bit big-endian
ADV_MAC_SLICE = slice(4, 10)
_MIN_ADV_LEN = ADV_MAC_SLICE.stop

# Model identifiers observed in advertisements. All confirmed basic-flavor
# units (ES-CS20MB1, R-A012, ES-26BB-B) advertise 0x0003; extended-flavor
# units (e.g. HVIN ESCS20MB2) advertise other values and speak frames this
# client does not parse yet, so they are deliberately not classified —
# connecting would hold the scale's BLE link away from the official app
# without ever producing a reading.
KNOWN_BASIC_MODEL_IDS: frozenset[int] = frozenset({0x0003})

_MAGIC = b"\x55\xaa"
_HEADER_LEN = 5

CMD_ACK = 0x10
CMD_STATUS = 0x11
CMD_STATUS_ALT = 0x12
CMD_MEASUREMENT = 0x14
CMD_STORED_RECORD = 0x15
CMD_SET_DISPLAY_UNIT = 0x90

_LEN_MEASUREMENT = 7
# Stored-record payloads are 12 bytes on the wire; the first 10 carry the
# fields parsed here, the rest is reserved.
_MIN_LEN_STORED_RECORD = 10
_LEN_STATUS = 5

# Sanity bound on the 16-bit length field: no frame of this protocol comes
# close (the longest observed payload is 13 bytes), so anything larger is a
# false sync match in a corrupted stream — without this cap such a match
# would stall parsing while iter_frames waits for kilobytes that never come.
_MAX_PAYLOAD_LEN = 64

_WEIGHT_SCALE = 100.0

STATUS_SETTLING = 0x00
STATUS_FINAL = 0x01
STATUS_ZERO_CURRENT = 0x10  # bit 4: zero-current (pregnancy) mode active
_STATUS_PHASE_MASK = 0x0F

_DISPLAY_UNIT_MODE_KEEP = 0x00


_WIRE_UNITS: dict[int, WeightUnit] = {
    1: WeightUnit.KG,
    2: WeightUnit.LB,
    3: WeightUnit.ST_LB,
    4: WeightUnit.ST,
}
_UNIT_CODES: dict[WeightUnit, int] = {unit: code for code, unit in _WIRE_UNITS.items()}


class Frame(NamedTuple):
    """One validated ``0x55aa`` frame: command byte and raw payload."""

    cmd: int
    payload: bytes


class Measurement(NamedTuple):
    """Decoded ``0x14`` measurement frame."""

    weight_kg: float
    resistance: int
    status: int

    @property
    def is_final(self) -> bool:
        return (self.status & _STATUS_PHASE_MASK) == STATUS_FINAL

    @property
    def is_settling(self) -> bool:
        return (self.status & _STATUS_PHASE_MASK) == STATUS_SETTLING

    @property
    def zero_current(self) -> bool:
        """True when the scale weighed in its zero-current (pregnancy) mode."""
        return bool(self.status & STATUS_ZERO_CURRENT)


class StoredRecord(NamedTuple):
    """Decoded ``0x15`` stored (offline) record."""

    weight_kg: float
    resistance: int
    seconds_ago: int
    mode_flag: int | None = None  # 14-byte form only: 1 = zero-current weigh-in


class DeviceStatus(NamedTuple):
    """Decoded ``0x11`` device-status frame."""

    power_on: bool
    display_unit: WeightUnit | None
    byte2: int  # unattributed; reads 1 on every unit captured
    stored_count: int
    byte4: int  # unattributed; 0 on most units, a constant 1 on the R-A016 — not a battery level


def checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def _build_frame(cmd: int, payload: bytes) -> bytes:
    body = _MAGIC + bytes([cmd]) + len(payload).to_bytes(2, "big") + payload
    return body + bytes([checksum(body)])


def build_display_unit_command(unit: WeightUnit) -> bytes:
    """``0x90 [unit, 0, 0, 0]`` — set the display unit, leave the mode alone."""
    return _build_frame(
        CMD_SET_DISPLAY_UNIT,
        bytes([_UNIT_CODES[WeightUnit(unit)], 0, _DISPLAY_UNIT_MODE_KEEP, 0]),
    )


def is_advertisement(payload: bytes, address: str | None = None) -> bool:
    """Return True if ``payload`` has the 0x55aa advertisement shape.

    Requires the constant ``00 04`` prefix; when ``address`` is a real
    MAC, the forward-order echo at bytes 4-10 must match it.
    """
    if len(payload) < _MIN_ADV_LEN or not payload.startswith(ADV_PREFIX):
        return False
    if address:
        # A non-MAC address (e.g. a macOS CoreBluetooth UUID) doesn't split
        # into six octets and skips the echo check entirely; a six-part
        # address that isn't valid hex is likewise accepted unchecked.
        octets = address.split(":")
        if len(octets) == 6:
            try:
                mac = bytes(int(o, 16) for o in octets)
            except ValueError:
                return True
            return payload[ADV_MAC_SLICE] == mac
    return True


def parse_model_id(payload: bytes) -> int | None:
    """Return the advertisement's model identifier, or None if too short."""
    if len(payload) < ADV_MODEL_SLICE.stop:
        return None
    return int.from_bytes(payload[ADV_MODEL_SLICE], "big")


def iter_frames(buffer: bytearray) -> list[Frame]:
    """Extract every complete, valid frame from ``buffer``, consuming it.

    Notifications do not align with frame boundaries, so this
    resynchronises on the magic and leaves a partial trailing frame in
    the buffer for the next call. A frame whose checksum fails is
    skipped by re-scanning from the next byte.
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

        length = int.from_bytes(buffer[3:5], "big")
        if length > _MAX_PAYLOAD_LEN:
            del buffer[:2]
            continue
        total = _HEADER_LEN + length + 1
        if len(buffer) < total:
            return frames

        raw = bytes(buffer[:total])
        if checksum(raw[:-1]) != raw[-1]:
            del buffer[:2]
            continue

        del buffer[:total]
        frames.append(Frame(raw[2], raw[_HEADER_LEN:-1]))


def parse_measurement(frame: Frame) -> Measurement | None:
    """Decode a ``0x14`` measurement frame; ``None`` for anything else."""
    if frame.cmd != CMD_MEASUREMENT or len(frame.payload) < _LEN_MEASUREMENT:
        return None
    payload = frame.payload
    return Measurement(
        weight_kg=round(int.from_bytes(payload[1:5], "big") / _WEIGHT_SCALE, 2),
        resistance=int.from_bytes(payload[5:7], "big"),
        status=payload[0],
    )


def parse_stored_record(frame: Frame) -> StoredRecord | None:
    """Decode a ``0x15`` stored offline record; ``None`` for anything else."""
    if frame.cmd != CMD_STORED_RECORD or len(frame.payload) < _MIN_LEN_STORED_RECORD:
        return None
    payload = frame.payload
    return StoredRecord(
        weight_kg=round(int.from_bytes(payload[0:4], "big") / _WEIGHT_SCALE, 2),
        resistance=int.from_bytes(payload[4:6], "big"),
        seconds_ago=int.from_bytes(payload[6:10], "big"),
        mode_flag=payload[12] if len(payload) > 12 else None,
    )


def parse_status(frame: Frame) -> DeviceStatus | None:
    """Decode a ``0x11`` device-status frame; ``None`` for anything else."""
    if frame.cmd != CMD_STATUS or len(frame.payload) < _LEN_STATUS:
        return None
    payload = frame.payload
    return DeviceStatus(
        power_on=payload[0] == 1,
        display_unit=_WIRE_UNITS.get(payload[1]),
        byte2=payload[2],
        stored_count=payload[3],
        byte4=payload[4],
    )
