"""Wire protocol for the QN-series Renpho ES-CS20M (extended + basic flavors).

Pure frame parsing / command building — no BLE I/O. Consumed by
:mod:`renpho_escs20m.qn.scale`.
"""

from __future__ import annotations

import dataclasses
import struct
import time
from collections.abc import Awaitable, Callable
from typing import NamedTuple

from .body_metrics import Sex
from ..data import WeightUnit

# --- Wire protocol constants (moved from const.py) -------------------------

_EPOCH_OFFSET = 946656000  # scale's epoch: 2000-01-01 00:00:00 UTC

# Frame opcodes (byte 0).
_OP_MEASUREMENT = 0x10
_OP_UNIT_REQUEST = 0x12
_OP_MEAS_INIT_REQUEST = 0x14
_OP_PRE_MEASUREMENT = 0x21
_OP_STORED_MEASUREMENT = 0x23
_OP_PROFILE_ACK = 0xA1

# Frame length (byte 1) — selects the flavor on the measurement and
# pre-measurement frames.
_LEN_EXTENDED_MEASUREMENT = 0x0E  # 14-byte frame, body fat on-device
_LEN_BASIC_MEASUREMENT = 0x0B  # 11-byte frame, weight + impedance
_LEN_EXTENDED_PRE_MEASUREMENT = 0x05  # scale wants a user-profile reply
_LEN_BASIC_PRE_MEASUREMENT = 0x04  # no reply needed; scale streams on its own
_LEN_STORED_MEASUREMENT = 0x13  # 19-byte stored offline-measurement record

# Per-device byte at frame offset 2 (renpho's is 0xFF).
_DEFAULT_VENDOR_BYTE = 0xFF

# Extended-flavor measurement status (byte 4).
_MEASUREMENT_STATUS_UNSTABLE = 0
_MEASUREMENT_STATUS_STABLE = 1
_MEASUREMENT_STATUS_STABLE_WITH_METRICS = 2

# Basic-flavor measurement status (byte 5). Two-nibble read: low = weight
# committed, high = BIA running.
_BASIC_STATUS_SETTLING = 0x00  # weight not yet committed, no impedance
_BASIC_STATUS_BIA_RUNNING = 0x11  # weight committed, BIA pass in progress
_BASIC_STATUS_FINAL = 0x01  # BIA done, impedance present

# Guest-mode sentinels for bytes 3-5 of the user-profile frame. The scale
# recognizes the session as ephemeral (no slot allocated, nothing stored),
# so the library coexists safely with the official Renpho app.
_GUEST_USER_ID = 0xFE
_GUEST_PAD_HI = 0xFF
_GUEST_PAD_LO = 0xEE
_USER_PROFILE_TRAILER_TAIL = 0x02

# Template for the display-unit command.
CMD_SET_DISPLAY_UNIT = bytes.fromhex("1309ff001000000000")

_DEFAULT_ALGORITHM = 0x04


@dataclasses.dataclass(frozen=True)
class Profile:
    """
    User-profile inputs the scale needs to compute body fat on-device.

    Pass an instance to :class:`~renpho_escs20m.qn.scale.RenphoQNScale`
    to drive the scale's body composition measurement in fixed-user mode, or
    return one from a :data:`ProfileResolver` for user-detection mode.

    Attributes:
        sex: :class:`Sex` enum value.
        age: Integer years. The Renpho app uses birthday-aware age
            (the user's UI age ``N`` if their birthday has occurred this
            calendar year, else ``N-1``); callers who want to match
            that should compute it themselves before constructing the
            profile.
        height_m: Height in metres. The library does **not** truncate
            to whole cm the way the Renpho app does — feeding the
            scale the user's exact height yields slightly
            more precise body fat output. To reproduce the app's
            displayed values exactly, pre-truncate the call site:
            ``height_m = int(actual_cm) / 100``.
        athlete: Switches the scale to its athlete-tuned body fat calculation curve.
        algorithm: Selects the on-device body fat calculation algorithm.
    """

    sex: Sex
    age: int
    height_m: float
    athlete: bool = False
    algorithm: int = _DEFAULT_ALGORITHM


# Async callback the library invokes once per session, in user-detection
# mode, with the first stable weight reading. Should return the
# :class:`Profile` for whichever user is on the scale, or ``None`` to
# leave the scale running on the bootstrap profile (no body fat will be
# produced — only weight will stream).
ProfileResolver = Callable[[float], Awaitable[Profile | None]]


# Profile sent when no real profile is available yet — i.e. weight-only
# mode (no profile ever) and the initial reply in user-detection mode
# (overridden once the resolver completes). The scale will not
# start a measurement at all without a profile reply, so we always send
# one. ``algorithm=0x00`` tells the scale "no body fat calculation".
_BOOTSTRAP_PROFILE = Profile(
    sex=Sex.Male,
    age=0,
    height_m=0,
    athlete=False,
    algorithm=0x00,
)


def _coerce_user_profile_height_mm(value: int) -> int:
    height_mm = int(value)
    if height_mm < 0 or height_mm > 0xFFFF:
        raise ValueError("profile height must be in the range 0..65535")
    return height_mm


def _height_m_to_mm(height_m: float) -> int:
    """Convert ``height_m`` (metres) to the integer-mm value the BLE
    profile frame requires.

    Rounds to the nearest mm. The library does **not** truncate to
    whole cm the way the Renpho app does — the scale firmware accepts
    any uint16 mm value, and feeding it the user's exact height gives
    more precise body fat output. Callers who want to match the app's
    displayed values exactly can pre-truncate themselves:
    ``height_m = int(actual_cm) / 100``.
    """
    return _coerce_user_profile_height_mm(int(round(float(height_m) * 1000)))


_WEIGHT_UNIT_TO_BYTE: dict[WeightUnit, int] = {
    WeightUnit.KG: 0x01,
    WeightUnit.LB: 0x02,
    WeightUnit.ST_LB: 0x08,
    WeightUnit.ST: 0x10,
}


def build_unit_update_command(
    desired_unit: WeightUnit, vendor_byte: int = _DEFAULT_VENDOR_BYTE
) -> bytearray:
    """
    Build the display-unit update command.

    Maps :class:`WeightUnit` onto the scale's raw unit byte:

    ============== =====
     ``WeightUnit``  byte
    ============== =====
    ``KG``          ``0x01``
    ``LB``          ``0x02``
    ``ST_LB``       ``0x08``  (stones + pounds remainder)
    ``ST``          ``0x10``  (stones only)
    ============== =====

    ``vendor_byte`` is the byte at offset 2 the scale uses (renpho uses ``0xFF``);
    it is echoed back so the command is accepted by the scale.
    """
    unit_byte = _WEIGHT_UNIT_TO_BYTE.get(WeightUnit(desired_unit), 0x01)
    payload = bytearray(CMD_SET_DISPLAY_UNIT)
    payload[2] = vendor_byte
    payload[3] = unit_byte
    payload[8] = sum(payload[0:8]) & 0xFF
    return payload


def build_measurement_initiation_command(
    vendor_byte: int = _DEFAULT_VENDOR_BYTE,
) -> bytearray:
    """Build the initiation command with current timestamp and checksum.

    ``vendor_byte`` is echoed at offset 2 (see
    :func:`build_unit_update_command`).
    """
    cmd = bytearray(8)
    cmd[0:3] = bytes([0x20, 0x08, vendor_byte])
    ts = int(time.time()) - _EPOCH_OFFSET
    struct.pack_into("<I", cmd, 3, ts)
    cmd[7] = sum(cmd[0:7]) & 0xFF
    return cmd


def build_end_measurement_command(vendor_byte: int = _DEFAULT_VENDOR_BYTE) -> bytearray:
    cmd = bytearray([0x1F, 0x05, vendor_byte, 0x10])
    cmd.append(sum(cmd) & 0xFF)
    return cmd


def build_stored_measurement_query(
    vendor_byte: int = _DEFAULT_VENDOR_BYTE,
) -> bytearray:
    """Build the basic-flavor stored-measurement query (``22 04``).

    The scale answers with one ``23 13`` record per offline reading (or a
    single ``count=0`` frame when the store is empty) — see
    :func:`parse_stored_measurement`. Delivering a record deletes it from
    the scale's store; there is no separate delete command.
    """
    cmd = bytearray([0x22, 0x04, vendor_byte])
    cmd.append(sum(cmd) & 0xFF)
    return cmd


def build_extended_stored_measurement_query(
    vendor_byte: int = _DEFAULT_VENDOR_BYTE,
) -> bytearray:
    """Build the extended-flavor stored-measurement query (``22 06``).

    The extended flavor uses a six-byte query whose payload varies with
    the user context; ``00 01`` is the guest-session form, which is the
    only mode this library drives. Answered like the basic query — see
    :func:`build_stored_measurement_query`.
    """
    cmd = bytearray([0x22, 0x06, vendor_byte, 0x00, 0x01])
    cmd.append(sum(cmd) & 0xFF)
    return cmd


def build_user_profile_command(
    sex: int,
    age: int,
    height_m: float,
    athlete: bool = False,
    algorithm: int = _DEFAULT_ALGORITHM,
) -> bytearray:
    """
    Build the 13-byte guest-mode user-profile command the scale expects
    in response to its profile request.

    The flag byte at offset 10 encodes both the body fat calculation algorithm selector
    and the athlete bit:

        flag = (algorithm + (0x0A if athlete else 0)) & 0xFF

    See :class:`Profile` for the meaning of the algorithm values and
    the athlete bit. ``algorithm=0x00`` disables body fat calculation.

    Height is given in metres and is rounded to the nearest mm before
    sending. The library does **not** truncate to whole cm the way the
    Renpho app does — the scale accepts any uint16 mm value.
    """
    height_mm = _height_m_to_mm(height_m)
    flag_byte = (int(algorithm) + (0x0A if athlete else 0)) & 0xFF
    payload = bytearray(
        [
            0xA0,
            0x0D,
            0x02,
            _GUEST_USER_ID,
            _GUEST_PAD_HI,
            _GUEST_PAD_LO,
            sex & 0xFF,
            age & 0xFF,
            (height_mm >> 8) & 0xFF,
            height_mm & 0xFF,
            flag_byte,
            _USER_PROFILE_TRAILER_TAIL,
        ]
    )
    payload.append(sum(payload) & 0xFF)
    return payload


def _build_command_for_profile(profile: Profile) -> bytearray:
    return build_user_profile_command(
        sex=int(profile.sex),
        age=profile.age,
        height_m=profile.height_m,
        athlete=profile.athlete,
        algorithm=profile.algorithm,
    )


class _ExtendedFrame(NamedTuple):
    """Decoded extended-flavor measurement frame fields."""

    weight_kg: float
    status: int
    body_fat: float | None
    resistance_1: int | None
    resistance_2: int | None


def parse_extended_measurement(payload: bytearray) -> _ExtendedFrame:
    """
    Parse a live measurement notification.

    Returns a _ExtendedFrame with decoded values. Resistance can be fed
    to :func:`renpho_escs20m.qn.body_metrics.calculate_body_fat` to compute
    body fat retroactively when the user identity is known later than
    the measurement (e.g. after a slow user-detection lookup).
    """
    status = payload[4]
    weight = int.from_bytes(payload[5:7], "big")
    weight_kg = round(float(weight) / 100, 2)

    body_fat = None
    r1 = None
    r2 = None

    if status == _MEASUREMENT_STATUS_STABLE_WITH_METRICS:
        bf_raw = int.from_bytes(payload[11:13], "big")
        if bf_raw:
            body_fat = round(float(bf_raw) / 10, 1)
        r1_raw = int.from_bytes(payload[7:9], "big")
        r2_raw = int.from_bytes(payload[9:11], "big")
        if r1_raw or r2_raw:
            r1 = r1_raw
            r2 = r2_raw

    return _ExtendedFrame(
        weight_kg=weight_kg,
        status=status,
        body_fat=body_fat,
        resistance_1=r1,
        resistance_2=r2,
    )


class _StoredFrame(NamedTuple):
    """Decoded stored offline-measurement record fields."""

    count: int
    index: int
    timestamp: int
    weight_kg: float
    resistance_1: int
    resistance_2: int


def parse_stored_measurement(payload: bytearray) -> _StoredFrame:
    """Decode a stored offline-measurement record (``23 13``, 19 bytes).

    The scale sends one record per offline reading in response to the
    ``22 04`` query, newest first. Layout::

        0..2    prefix 23 13 <vendor>
        3       count — total records in this batch (0 = store empty)
        4       index — 1-based position of this record in the batch
        5..8    timestamp, little-endian uint32, seconds since
                2000-01-01 00:00:00 UTC
        9..10   weight, big-endian uint16, 0.01 kg
        11..12  resistance 1
        13..14  resistance 2
        15..17  reserved (0x00)
        18      checksum

    ``timestamp`` is returned as unix seconds. When ``count == 0`` the
    store is empty and the remaining fields are meaningless (bytes 5-8
    carry an uninterpreted varying value) — callers must not read them.
    """
    return _StoredFrame(
        count=payload[3],
        index=payload[4],
        timestamp=int.from_bytes(payload[5:9], "little") + _EPOCH_OFFSET,
        weight_kg=round(int.from_bytes(payload[9:11], "big") / 100, 2),
        resistance_1=int.from_bytes(payload[11:13], "big"),
        resistance_2=int.from_bytes(payload[13:15], "big"),
    )


class _ExtendedStoredFrame(NamedTuple):
    """Decoded extended-flavor stored offline-measurement record fields."""

    count: int
    index: int
    user_index: int
    timestamp: int
    weight_kg: float
    resistance_1: int
    resistance_2: int
    body_fat: float | None


def parse_extended_stored_measurement(payload: bytearray) -> _ExtendedStoredFrame:
    """Decode an extended-flavor stored record (``23 13``, 19 bytes).

    The extended flavor inserts a store-user-index byte at offset 5
    (``0xF0`` = record not assigned to a user slot) and appends the
    on-device body-fat result, shifting the shared fields by one byte
    relative to :func:`parse_stored_measurement`::

        0..2    prefix 23 13 <vendor>
        3       count — total records in this batch (0 = store empty)
        4       index — 1-based position of this record in the batch
        5       store user index (0xF0 = unassigned)
        6..9    timestamp, little-endian uint32, seconds since
                2000-01-01 00:00:00 UTC
        10..11  weight, big-endian uint16, 0.01 kg
        12..13  resistance 1
        14..15  resistance 2
        16..17  body fat, big-endian uint16, 0.1 %
        18      checksum

    As with the basic record, when ``count == 0`` the store is empty
    and the remaining fields must not be read.
    """
    bf_raw = int.from_bytes(payload[16:18], "big")
    return _ExtendedStoredFrame(
        count=payload[3],
        index=payload[4],
        user_index=payload[5],
        timestamp=int.from_bytes(payload[6:10], "little") + _EPOCH_OFFSET,
        weight_kg=round(int.from_bytes(payload[10:12], "big") / 100, 2),
        resistance_1=int.from_bytes(payload[12:14], "big"),
        resistance_2=int.from_bytes(payload[14:16], "big"),
        body_fat=round(bf_raw / 10, 1) if bf_raw else None,
    )


class _BasicFrame(NamedTuple):
    """Decoded basic-flavor measurement frame fields."""

    weight_kg: float
    status: int
    resistance_1: int
    resistance_2: int


def parse_basic_measurement(payload: bytearray) -> _BasicFrame:
    """Decode a basic-flavor measurement frame (``10 0b``, 11 bytes).

    Layout (no guest-id byte — shifted left vs the 14-byte extended frame)::

        0..2   prefix 10 0b <vendor>
        3..4   weight, big-endian uint16, 0.01 kg
        5      status: 0x00 settling, 0x11 stable + BIA running, 0x01 final
        6..7   resistance 1 (only meaningful on the 0x01 final frame)
        8..9   resistance 2 (only meaningful on the 0x01 final frame)
        10     checksum

    The caller validates length (>= 10 bytes) and dispatches on ``status``.
    Resistance is only meaningful on the final (``0x01``) frame; on
    settling / BIA-running frames it reads as 0.
    """
    return _BasicFrame(
        weight_kg=round(int.from_bytes(payload[3:5], "big") / 100, 2),
        status=payload[5],
        resistance_1=int.from_bytes(payload[6:8], "big"),
        resistance_2=int.from_bytes(payload[8:10], "big"),
    )
