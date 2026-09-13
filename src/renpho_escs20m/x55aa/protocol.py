"""Wire protocol for the ``0x55aa`` GATT variant (LeFu hardware).

Frame layout, checksum-verified against captures from four independent
units (ES-CS20MB1, R-A012, ES-26BB-B, ESCS20MB2)::

    55 AA | command | length (uint16 BE) | payload[length] | checksum

``checksum`` is the low byte of the sum of every preceding byte; the
total frame size is ``6 + length``.

Frame ``0x14`` — measurement (7-byte payload)
    ===========  =================================================================
    ``[0]``      status. Low nibble: ``0`` settling, ``1`` final; any
                 other value is unclassified. Bit 4 (``0x10``) set: the
                 scale is in its zero-current (pregnancy) mode — a
                 setting the Renpho app stores on the scale, under which
                 the bioimpedance pass is skipped; ``0x10``/``0x11`` are
                 ordinary settling/final frames with resistance 0
                 (R-A016, capture + probe run).
    ``[1:5]``    weight, uint32 big-endian, 0.01 kg
    ``[5:7]``    resistance, uint16 big-endian, ohms — zero on every
                 settling frame, populated on the final after the
                 scale's bioimpedance pass.
    ===========  =================================================================

Frame ``0x15`` — stored (offline) record (12- or 14-byte payload)
    ``[0:4]`` weight, ``[4:6]`` resistance, ``[6:10]`` seconds since the
    weigh-in, ``[10:12]`` reserved; the 14-byte form adds ``[12]`` = the
    zero-current flag of that weigh-in (R-A016).

Frame ``0x18`` — final measurement, extended flavor (13-byte payload)
    ===========  =================================================================
    ``[0]``      status. ``0x01`` final; ``0x11`` final in zero-current
                 (pregnancy) mode.
    ``[1]``      unattributed; reads 0x01 in every capture
    ``[2]``      user slot echo — the latest profile write, not
                 necessarily the profile the on-device math used
    ``[3:7]``    weight, uint32 big-endian, 0.01 kg
    ``[7:9]``    resistance, uint16 big-endian, ohms — zero when the
                 impedance pass yielded nothing (e.g. shoes on)
    ``[9:11]``   BMI, uint16 big-endian, ×10 — computed on-device from
                 whatever profile was held at commit time — zero when
                 none was, independently of impedance
    ``[11:13]``  body fat percent, uint16 big-endian, ×10 — zero when
                 there was no impedance to compute it from
    ===========  =================================================================

Frame ``0x19`` — stored (offline) record, extended flavor (20-byte payload;
fragmented when it exceeds the ATT MTU)
    ===========  =================================================================
    ``[0]``      status. Bit 4 (``0x10``) set: recorded in zero-current
                 (pregnancy) mode.
    ``[1:3]``    unattributed
    ``[3]``      user slot
    ``[4:8]``    weight, uint32 big-endian, 0.01 kg
    ``[8:10]``   resistance, uint16 big-endian, ohms
    ``[10:12]``  BMI, uint16 big-endian, ×10
    ``[12:14]``  body fat percent, uint16 big-endian, ×10
    ``[14:16]``  unattributed
    ``[16:20]``  seconds since the weigh-in, uint32 big-endian
    ===========  =================================================================

Command ``0x90`` — display unit (4-byte payload ``[unit, 0, mode, 0]``)
    ``unit`` 1=kg 2=lb 3=st:lb 4=st. ``mode`` 0 leaves the scale's stored
    zero-current setting untouched (verified on the R-A016: the unit
    changed, the mode stayed) and is what the app itself sends in captured
    R-A012 sessions; 1/2 would overwrite that setting, so the library
    never sends them.

Command ``0x97`` — set time (9-byte payload)
    Sent only in its sub-op 1 (set-clock) form: byte 0 is the sub-op, bytes
    1-6 are Unix seconds UTC big-endian, byte 7 is the time-zone sign (0
    ahead of UTC, 1 behind), and byte 8 is the offset in whole hours.
    The scale presumably derives a user's age from the profile's date of
    birth and this clock.

Command ``0x96`` — guest profile (14-byte payload)
    Sent only in its guest form: user slot 9, outside the range of
    registered slots the scale persists, with byte 12 set to ``0xFF``. Byte
    0 packs sex (bits 5:4, 1=male 2=female) with the slot number (bits
    3:0). Bytes 1-2 are the birth year, big-endian; byte 3 the birth
    month; byte 4 the birth day. Bytes 5-6 are height in units of 0.1 cm,
    big-endian. Bytes 7-10 are the user's last known weight in units of
    0.01 kg, big-endian. Byte 11 is ``0xA8`` plus a low-two-bit selector
    for the on-device body-fat algorithm.
"""

from __future__ import annotations

import dataclasses
import datetime
from collections.abc import Awaitable, Callable
from typing import NamedTuple

from ..body_metrics import Sex
from ..data import WeightUnit


class ForbiddenWrite(ValueError):
    """A frame this library must never send (see assert_allowed_write)."""


MANUFACTURER_ID = 0x1A10

SUPPORTED_COMPANY_IDS = frozenset([MANUFACTURER_ID])

ADV_PREFIX = b"\x00\x04"
ADV_MODEL_SLICE = slice(2, 4)  # model identifier, 16-bit big-endian
ADV_MAC_SLICE = slice(4, 10)
_MIN_ADV_LEN = ADV_MAC_SLICE.stop

# Model identifiers observed in advertisements, by flavor.
#
# Basic (weight + impedance on 0x14; no profile): every captured unit
# advertises 0x0003 (ES-CS20MB1, R-A012, R-A016, ES-26BB-B).
#
# Extended (on-device body composition on 0x18; takes a guest profile):
# 0x0031 (49) is the ES-CS20M HVIN ESCS20MB2 / Elis 1 (three captured units);
# 0x0030 (48) is a sibling revision seen on one unit with identical frames.
KNOWN_BASIC_MODEL_IDS: frozenset[int] = frozenset({0x0003})
KNOWN_EXTENDED_MODEL_IDS: frozenset[int] = frozenset({0x0030, 0x0031})
KNOWN_MODEL_IDS: frozenset[int] = KNOWN_BASIC_MODEL_IDS | KNOWN_EXTENDED_MODEL_IDS

# Models on which the set-time command's 9-byte epoch layout is
# capture-verified. Kept as a per-model allow-list rather than a flavor
# property: the clock is state the scale persists, so a layout that has not
# been seen on a given model is not worth guessing at.
SET_TIME_MODEL_IDS: frozenset[int] = frozenset({0x0030, 0x0031})


def is_extended_model(model_id: int | None) -> bool:
    """True for advertised model ids known to speak the extended flavor."""
    return model_id in KNOWN_EXTENDED_MODEL_IDS


# ---- guest profile (extended flavor) ----------------------------------------

# The profile frame addresses a user slot. A registered slot creates or
# overwrites a user the scale persists — exactly what coexistence with the
# official app forbids. Slot 9 with byte 12 set to 0xFF is the guest form:
# captures show the official app sending it for a guest weigh-in, re-sent
# every session rather than managed as a slot, and the scale answers it
# with full on-device body composition and slot 9 echoed in the final.
GUEST_SLOT = 9
_GUEST_EXIST_MASK = 0xFF
# Byte 11 is 0xA8 in every captured profile once its low two bits are
# masked off, and those two bits select the scale's body-fat algorithm:
# 1 reproduces this library's algorithm 0x03 and 2 its 0x04, cross-checked
# against thirteen on-device finals from four units.
_PROFILE_FLAGS_BASE = 0xA8
# The same byte for a profile asking for the athlete curve.
_PROFILE_FLAGS_BASE_ATHLETE = 0x68
_ALGORITHM_TO_METHOD: dict[int, int] = {0x03: 1, 0x04: 2}
# Byte 13 is 0x05 in every captured profile. One capture used 0x06, where
# the scale skipped its impedance pass and returned zero resistance and
# zero body fat (BMI was still computed) — never what this library wants.
_PROFILE_TRAILER = 0x05
# The same byte with the impedance pass switched off. One capture used it: the
# scale returned zero resistance and zero body fat, and its own display showed
# weight only (BMI is still computed on the wire, and discarded). Verified on a
# single unit, where the next profile carrying 0x05 restored normal behaviour
# immediately, both mid-session and in a following session. The official app
# sends this form for its hold-baby flow.
_PROFILE_TRAILER_NO_IMPEDANCE = 0x06
_SEX_TO_WIRE: dict[Sex, int] = {Sex.Male: 1, Sex.Female: 2}

# The default: all but one captured extended-flavor profile selected it, and
# the library reproduces its on-device values with algorithm 0x03.
X55AA_DEFAULT_ALGORITHM = 0x03


@dataclasses.dataclass(frozen=True)
class X55AAProfile:
    """User-profile inputs for the extended ``0x55aa`` flavor.

    Same inputs as the QN :class:`~renpho_escs20m.Profile`, except the wire
    carries a full date of birth rather than an age, so ``birthday`` replaces
    ``age``. The library derives a birthday-aware age from it when needed
    (see ``age_on``), matching the official app.

    Attributes:
        sex: :class:`~renpho_escs20m.Sex`.
        birthday: Date of birth. Sent as year/month/day; the scale computes
            the age itself from its own clock.
        height_m: Height in metres; sent at 0.1 cm resolution.
        athlete: Requests the scale's athlete body-fat curve.
        algorithm: ``0x03`` or ``0x04``, the same ids as the QN client and
            :func:`~renpho_escs20m.calculate_body_fat`.
        last_weight_kg: The user's previous reading, for the profile frame's
            mandatory last-known-weight field (the official app sends
            exactly this). ``None`` lets the client substitute the weight
            settling on the scale, or a neutral constant the client
            supplies before any weight is known. No effect on the reported
            measurement has been observed either way.
    """

    sex: Sex
    birthday: datetime.date
    height_m: float
    athlete: bool = False
    algorithm: int = X55AA_DEFAULT_ALGORITHM
    last_weight_kg: float | None = None


# Async callback invoked once per weigh-in with the settled weight; returns
# the profile of whoever is on the scale, or None to fall back to the
# library's placeholder profile (weight and impedance are still delivered).
X55AAProfileResolver = Callable[[float], Awaitable[X55AAProfile | None]]


def age_on(birthday: datetime.date, today: datetime.date) -> int:
    """Whole years from ``birthday`` to ``today``, decremented if the birthday
    has not yet occurred in ``today``'s year.

    Raises ``ValueError`` if ``birthday`` is after ``today``: a negative age
    is not an answer to the question, and it would reach
    :func:`~renpho_escs20m.calculate_body_fat` as a plausible-looking number.
    """
    if birthday > today:
        raise ValueError(f"birthday {birthday} is after {today}")
    years = today.year - birthday.year
    if (today.month, today.day) < (birthday.month, birthday.day):
        years -= 1
    return years


_MAGIC = b"\x55\xaa"
_HEADER_LEN = 5

CMD_ACK = 0x10
CMD_STATUS = 0x11
CMD_STATUS_ALT = 0x12
CMD_MEASUREMENT = 0x14
CMD_STORED_RECORD = 0x15
CMD_PROFILE_ACK = 0x16  # ack of 0x96; payload differs per model, so it is not parsed
CMD_SETTINGS_ACK = 0x17  # ack of 0x97
CMD_EXTENDED_MEASUREMENT = 0x18
CMD_EXTENDED_STORED_RECORD = 0x19
CMD_SET_DISPLAY_UNIT = 0x90
# 0x95 — one ack clears the scale's entire offline store; opt-in only.
# 0x96 — a registered slot persists a user on the scale; only the guest
# form (slot 9) may ever be sent.
# 0x97 — only the set-time form may ever be sent. Byte 0 selects what the
# rest of the payload means; other values of it have been probed on a
# model-49 unit and answered with an ack indistinguishable from the set-time
# ack, with no observable change, so what they do is unknown.
CMD_STORED_RECORD_ACK = 0x95
CMD_USER_PROFILE = 0x96
CMD_SETTINGS = 0x97

# opt-in only: delete scope on the scale is unverified
CMD_EXTENDED_STORED_RECORD_ACK = 0x99
# The only stored-record ack payload any capture contains, so the allow-list
# below refuses anything else rather than guess at what it would mean.
_STORED_RECORD_ACK_PAYLOAD = b"\x01"

_SETTINGS_SET_TIME = 0x01
_LEN_SET_TIME = 9
_LEN_DISPLAY_UNIT = 4
_LEN_USER_PROFILE = 14

_LEN_MEASUREMENT = 7
_LEN_EXTENDED_MEASUREMENT = 13
# Stored-record payloads are 12 bytes on the wire; the first 10 carry the
# fields parsed here, the rest is reserved.
_MIN_LEN_STORED_RECORD = 10
_LEN_EXTENDED_STORED_RECORD = 20
_LEN_STATUS = 5

# A frame longer than one notification is split by the extended flavor into
# chunks behind a 3-byte header. Captures show 0xAD on the first chunk and
# 0xAF on the last, each followed by two bytes counting the chunks left; the
# only frame long enough to fragment needs two chunks, so no third marker has
# been seen. 0xAE is treated as one too, so a longer frame would reassemble
# rather than corrupt the stream. The checksum covers the reassembled frame.
_FRAGMENT_MARKERS = frozenset({0xAD, 0xAE, 0xAF})
_FRAGMENT_HEADER_LEN = 3

# Sanity bound on the 16-bit length field: no frame of this protocol comes
# close (the longest observed payload is 20 bytes), so anything larger is a
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


class ExtendedMeasurement(NamedTuple):
    """Decoded ``0x18`` final frame (extended flavor).

    ``bmi`` and ``body_fat`` are the scale's own numbers, computed from
    whatever profile it held when the measurement committed. Consumers
    should not report these: a profile written after the commit does not
    recompute them, so they are only trustworthy when the caller knows its
    profile preceded the weigh-in. ``slot`` echoes the latest profile
    write, not the profile the math used.
    """

    weight_kg: float
    resistance: int
    status: int
    slot: int
    bmi: float
    body_fat: float

    @property
    def is_final(self) -> bool:
        return (self.status & _STATUS_PHASE_MASK) == STATUS_FINAL

    @property
    def zero_current(self) -> bool:
        return bool(self.status & STATUS_ZERO_CURRENT)


class StoredRecord(NamedTuple):
    """Decoded ``0x15`` stored (offline) record."""

    weight_kg: float
    resistance: int
    seconds_ago: int
    mode_flag: int | None = None  # 14-byte form only: 1 = zero-current weigh-in


class ExtendedStoredRecord(NamedTuple):
    """Decoded ``0x19`` stored (offline) record, extended flavor.

    The on-device BMI/body-fat bytes are deliberately not decoded here:
    drained records are discarded, and the values are the scale's own,
    computed from whatever profile it held at the time.
    """

    weight_kg: float
    resistance: int
    seconds_ago: int
    slot: int
    status: int

    @property
    def zero_current(self) -> bool:
        return bool(self.status & STATUS_ZERO_CURRENT)


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


def build_guest_profile_command(
    profile: X55AAProfile,
    fallback_last_weight_kg: float,
    *,
    measure_impedance: bool = True,
) -> bytes:
    """``0x96`` in the guest form (slot 9), 14-byte payload.

    Layout: ``[0]`` sex (bits 5:4) | slot (bits 3:0) · ``[1:3]`` birth year
    BE · ``[3]`` month · ``[4]`` day · ``[5:7]`` height ×10 cm BE · ``[7:11]``
    last known weight ×100 kg BE · ``[11]`` flags, the low two bits
    selecting the body-fat algorithm · ``[12]`` ``0xFF`` for a guest ·
    ``[13]`` ``0x05``.

    ``measure_impedance=False`` asks the scale to skip its bioimpedance
    pass. The final then carries no resistance, and the scale's own display
    shows weight only instead of body composition derived from this profile.

    The last-known-weight field takes ``profile.last_weight_kg`` when set,
    else ``fallback_last_weight_kg`` (the client passes the settled weight
    when it has one, otherwise a neutral constant).
    """
    last_weight_kg = (
        profile.last_weight_kg
        if profile.last_weight_kg is not None
        else fallback_last_weight_kg
    )
    try:
        method = _ALGORITHM_TO_METHOD[profile.algorithm]
    except KeyError:
        raise ValueError(
            f"unsupported algorithm 0x{profile.algorithm:02x}; expected 0x03 or 0x04"
        ) from None
    height_tenth_cm = int(round(profile.height_m * 1000))
    if not 0 < height_tenth_cm <= 0xFFFF:
        raise ValueError("profile height must be in the range 0.0005..65.535 m")
    weight_cg = int(round(last_weight_kg * 100))
    if not 0 <= weight_cg <= 0xFFFFFFFF:
        raise ValueError("last weight must be in the range 0..42949672.95 kg")
    # datetime.date.year is already constrained to 1..9999; no extra guard needed.
    year = profile.birthday.year
    base = _PROFILE_FLAGS_BASE_ATHLETE if profile.athlete else _PROFILE_FLAGS_BASE
    flags = base | method
    payload = (
        bytes([(_SEX_TO_WIRE[Sex(profile.sex)] << 4) | GUEST_SLOT])
        + year.to_bytes(2, "big")
        + bytes([profile.birthday.month, profile.birthday.day])
        + height_tenth_cm.to_bytes(2, "big")
        + weight_cg.to_bytes(4, "big")
        + bytes(
            [
                flags,
                _GUEST_EXIST_MASK,
                _PROFILE_TRAILER
                if measure_impedance
                else _PROFILE_TRAILER_NO_IMPEDANCE,
            ]
        )
    )
    return _build_frame(CMD_USER_PROFILE, payload)


def build_extended_stored_record_ack() -> bytes:
    """``0x99 [01]`` — acknowledge one delivered ``0x19`` stored record.

    The payload carries no record or slot reference; the official app sends
    one per record on receipt. Whether the scale deletes that record or the
    whole store is not distinguishable from captures, so callers gate it
    behind an opt-in.
    """
    return _build_frame(CMD_EXTENDED_STORED_RECORD_ACK, _STORED_RECORD_ACK_PAYLOAD)


def build_set_time_command(unix_seconds: int, utc_offset_seconds: int) -> bytes:
    """``0x97`` sub-op 1: set the scale's clock.

    Payload: ``01`` · Unix seconds, 6 bytes BE (UTC) · time-zone sign
    (``0`` = ahead of UTC, ``1`` = behind) · offset in whole hours. This is
    the form the official app writes first on every connection to model ids
    0x30/0x31; other models of the same generation take a different body under
    the same sub-op, so callers gate this on :data:`SET_TIME_MODEL_IDS`.

    The scale presumably derives a user's age from the profile's date of
    birth and this clock (the app always sets it, so the unset case has
    never been observed); sending it keeps the on-device body-fat display
    consistent.
    """
    unix_seconds = int(unix_seconds)
    if not 0 <= unix_seconds < 1 << 48:
        raise ValueError("unix_seconds must fit in 48 bits")
    hours = abs(int(utc_offset_seconds)) // 3600
    if hours > 14:
        raise ValueError(
            "utc offset must be within ±14 hours (seconds, not milliseconds)"
        )
    payload = (
        bytes([_SETTINGS_SET_TIME])
        + unix_seconds.to_bytes(6, "big")
        + bytes([0 if utc_offset_seconds >= 0 else 1, hours])
    )
    return _build_frame(CMD_SETTINGS, payload)


def assert_allowed_write(frame: bytes) -> None:
    """Raise :class:`ForbiddenWrite` unless ``frame`` is one this library may send.

    Defence in depth for the client's writer: the builders above cannot
    produce a forbidden frame, but nothing else should slip through either —
    including a forbidden frame riding behind an allowed one in the same
    buffer, which the length-field check below catches. Forbidden by
    policy: ``0x90`` with a non-zero mode byte (writes a setting the scale
    persists; mode 2 has no known exit on the extended flavor), ``0x97``
    other than the set-time form (other values of its leading byte have
    been probed on these units without an observable effect, so what they
    do is unknown), and ``0x96`` addressing a registered slot (persists a
    user on the scale).
    """
    if len(frame) < _HEADER_LEN + 1 or frame[:2] != _MAGIC:
        raise ForbiddenWrite("not a 0x55aa frame")
    if len(frame) != _HEADER_LEN + int.from_bytes(frame[3:5], "big") + 1:
        raise ForbiddenWrite("frame length does not match its length field")
    cmd, payload = frame[2], frame[_HEADER_LEN:-1]
    if cmd == CMD_SET_DISPLAY_UNIT:
        if len(payload) != _LEN_DISPLAY_UNIT or payload[2] != _DISPLAY_UNIT_MODE_KEEP:
            raise ForbiddenWrite(
                "0x90 must carry mode byte 0 (modes 1-3 write a setting the "
                "scale persists; never sent)"
            )
    elif cmd == CMD_SETTINGS:
        if len(payload) != _LEN_SET_TIME or payload[0] != _SETTINGS_SET_TIME:
            raise ForbiddenWrite("0x97: only the 9-byte set-time form may be sent")
    elif cmd == CMD_USER_PROFILE:
        if (
            len(payload) != _LEN_USER_PROFILE
            or (payload[0] & 0x0F) != GUEST_SLOT
            or payload[12] != _GUEST_EXIST_MASK
        ):
            raise ForbiddenWrite("0x96 must be the guest form (slot 9, mask 0xFF)")
        if payload[13] not in (_PROFILE_TRAILER, _PROFILE_TRAILER_NO_IMPEDANCE):
            # The byte that decides whether the scale runs its impedance pass.
            # Only the two forms captures contain are ever sent; what another
            # value would do is unknown, and this is the wrong place to find out.
            raise ForbiddenWrite(
                f"0x96 trailer 0x{payload[13]:02x} is not one of the known forms"
            )
    elif cmd in (CMD_STORED_RECORD_ACK, CMD_EXTENDED_STORED_RECORD_ACK):
        if payload != _STORED_RECORD_ACK_PAYLOAD:
            raise ForbiddenWrite(
                f"0x{cmd:02x} must carry payload 01; no other ack form has been seen"
            )
    else:
        raise ForbiddenWrite(f"command 0x{cmd:02x} is not on this library's allow-list")


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


def strip_fragment_header(payload: bytes) -> bytes:
    """Return ``payload`` without its 3-byte fragment header, if it has one.

    Assumes a notification is either a fragment (marker-first) or begins a
    frame; fragments are assumed to arrive in order and un-interleaved (the
    header's two trailing bytes are not checked). If either assumption
    fails, the reassembled bytes fail the checksum and ``iter_frames``
    resyncs, costing that one frame.
    """
    if len(payload) >= _FRAGMENT_HEADER_LEN and payload[0] in _FRAGMENT_MARKERS:
        return payload[_FRAGMENT_HEADER_LEN:]
    return payload


def iter_frames(buffer: bytearray) -> list[Frame]:
    """Extract every complete, valid frame from ``buffer``, consuming it.

    Notifications need not align with frame boundaries, so this
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


def parse_extended_measurement(frame: Frame) -> ExtendedMeasurement | None:
    """Decode a ``0x18`` final frame; ``None`` for anything else.

    Payload: ``[0]`` status (``0x01`` final, ``0x11`` final in zero-current
    mode) · ``[1]`` unattributed; reads 0x01 in every capture · ``[2]`` user
    slot echo · ``[3:7]`` weight uint32 BE 0.01 kg · ``[7:9]`` resistance
    uint16 BE Ω · ``[9:11]`` BMI ×10 · ``[11:13]`` body fat % ×10.
    """
    if (
        frame.cmd != CMD_EXTENDED_MEASUREMENT
        or len(frame.payload) < _LEN_EXTENDED_MEASUREMENT
    ):
        return None
    payload = frame.payload
    return ExtendedMeasurement(
        weight_kg=round(int.from_bytes(payload[3:7], "big") / _WEIGHT_SCALE, 2),
        resistance=int.from_bytes(payload[7:9], "big"),
        status=payload[0],
        slot=payload[2],
        bmi=round(int.from_bytes(payload[9:11], "big") / 10, 1),
        body_fat=round(int.from_bytes(payload[11:13], "big") / 10, 1),
    )


def parse_extended_stored_record(frame: Frame) -> ExtendedStoredRecord | None:
    """Decode a ``0x19`` stored record; ``None`` for anything else.

    Payload: ``[0]`` status (bit 4 = recorded in zero-current mode) · ``[1]``
    ``[2]`` unattributed · ``[3]`` user slot · ``[4:8]`` weight uint32 BE
    0.01 kg · ``[8:10]`` resistance · ``[10:12]`` BMI ×10 · ``[12:14]`` body
    fat ×10 · ``[14:16]`` unattributed · ``[16:20]`` seconds since the weigh-in.
    """
    # Exact length only. These offsets are validated against one captured
    # layout; a record of another length has never been captured, and reading
    # it with these offsets would most likely yield a plausible but wrong age
    # rather than an obvious error. An unknown length must fail loudly (the
    # handler logs it) instead.
    if (
        frame.cmd != CMD_EXTENDED_STORED_RECORD
        or len(frame.payload) != _LEN_EXTENDED_STORED_RECORD
    ):
        return None
    payload = frame.payload
    return ExtendedStoredRecord(
        weight_kg=round(int.from_bytes(payload[4:8], "big") / _WEIGHT_SCALE, 2),
        resistance=int.from_bytes(payload[8:10], "big"),
        seconds_ago=int.from_bytes(payload[16:20], "big"),
        slot=payload[3],
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
