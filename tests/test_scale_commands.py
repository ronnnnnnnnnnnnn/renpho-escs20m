"""Tests for the raw command builders in :mod:`renpho_escs20m.scale`.

The library always drives the scale in guest mode, so
:func:`build_user_profile_command` emits ``0xa00d 02`` with the guest
sentinel triplet ``0xFE 0xFF 0xEE`` in bytes 3-5. Byte 10 selects the
BIA algorithm via ``(algorithm + (0x0A if athlete else 0)) & 0xFF``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from renpho_escs20m.const import (
    BODY_FAT_KEY,
    RESISTANCE_1_KEY,
    RESISTANCE_2_KEY,
    WEIGHT_KEY,
)
from renpho_escs20m.scale import (
    CMD_END_MEASUREMENT,
    _MEASUREMENT_STATUS_STABLE,
    _MEASUREMENT_STATUS_STABLE_WITH_METRICS,
    Profile,
    WeightUnit,
    RenphoESCS20MScale,
    build_unit_update_command,
    build_user_profile_command,
    parse_weight,
)


def _make_scale(profile: Profile | AsyncMock | None = None) -> tuple[RenphoESCS20MScale, MagicMock]:
    callback = MagicMock()
    scanner = MagicMock()
    scale = RenphoESCS20MScale(
        "00:11:22:33:44:55",
        callback,
        WeightUnit.KG,
        profile=profile,
        bleak_scanner_backend=scanner,
    )
    return scale, callback


def _measurement_payload(status: int) -> bytearray:
    """Build a synthetic measurement frame for resolver/end-command behavior."""
    payload = bytearray(13)
    payload[0] = 0x10
    payload[1] = 0x0e
    payload[2] = 0xff
    payload[3] = 0xfe
    payload[4] = status
    payload[5:7] = int(74.45 * 100).to_bytes(2, "big")
    if status == _MEASUREMENT_STATUS_STABLE_WITH_METRICS:
        payload[7:9] = (500).to_bytes(2, "big")
        payload[9:11] = (500).to_bytes(2, "big")
        payload[11:13] = (210).to_bytes(2, "big")
    return payload


@pytest.mark.asyncio
async def test_handle_measurement_only_resolves_profile_on_stable():
    scale = RenphoESCS20MScale(
        "00:11:22:33:44:55",
        MagicMock(),
        WeightUnit.KG,
        profile=AsyncMock(return_value=None),
        bleak_scanner_backend=MagicMock(),
    )
    scale._resolve_and_send_profile = AsyncMock()

    scale._handle_measurement(
        _measurement_payload(_MEASUREMENT_STATUS_STABLE),
        "Renpho ES-CS20M",
        "00:11:22:33:44:55",
    )
    await asyncio.sleep(0)
    scale._resolve_and_send_profile.assert_awaited_once_with(74.45, "00:11:22:33:44:55")

    scale._resolve_and_send_profile.reset_mock()
    scale._handle_measurement(
        _measurement_payload(_MEASUREMENT_STATUS_STABLE_WITH_METRICS),
        "Renpho ES-CS20M",
        "00:11:22:33:44:55",
    )
    await asyncio.sleep(0)
    scale._resolve_and_send_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_measurement_only_sends_end_measurement_for_stable_with_metrics():
    scale, callback = _make_scale()
    scale._safe_write = AsyncMock()

    scale._handle_measurement(
        _measurement_payload(_MEASUREMENT_STATUS_STABLE),
        "Renpho ES-CS20M",
        "00:11:22:33:44:55",
    )
    await asyncio.sleep(0)
    scale._safe_write.assert_not_awaited()

    scale._handle_measurement(
        _measurement_payload(_MEASUREMENT_STATUS_STABLE_WITH_METRICS),
        "Renpho ES-CS20M",
        "00:11:22:33:44:55",
    )
    await asyncio.sleep(0)
    scale._safe_write.assert_awaited_once_with(CMD_END_MEASUREMENT)
    assert callback.call_count == 1


def _cmd_hex(cmd: bytearray) -> str:
    return cmd.hex()


def test_user_profile_command_default_algorithm_is_0x04_non_athlete():
    """Default ``algorithm=0x04`` (Deurenberg-family BIA) → byte 10 = 0x04."""
    cmd = build_user_profile_command(sex=0, age=43, height_m=1.70)
    assert cmd[10] == 0x04


def test_user_profile_command_uses_guest_sentinels():
    """Bytes 3-5 are always the fixed guest sentinel ``FE FF EE``."""
    cmd = build_user_profile_command(sex=0, age=43, height_m=1.70)
    assert cmd[0:3] == bytearray([0xA0, 0x0D, 0x02])
    assert cmd[3:6] == bytearray([0xFE, 0xFF, 0xEE])
    assert cmd[11] == 0x02
    assert cmd[-1] == sum(cmd[:-1]) & 0xFF


def test_user_profile_command_athlete_adds_0x0a():
    non_athlete = build_user_profile_command(
        sex=0, age=43, height_m=1.70, athlete=False
    )
    athlete = build_user_profile_command(
        sex=0, age=43, height_m=1.70, athlete=True
    )
    # Default algorithm 0x04 → 0x04 / 0x0E
    assert non_athlete[10] == 0x04
    assert athlete[10] == 0x0E
    assert athlete[10] - non_athlete[10] == 0x0A
    # Every other byte except the checksum is identical between the two
    # frames.
    assert non_athlete[:10] == athlete[:10]
    assert non_athlete[11] == athlete[11]
    # Checksum differs by +0x0A (mod 256).
    assert (athlete[-1] - non_athlete[-1]) % 256 == 0x0A


@pytest.mark.parametrize(
    "algorithm,athlete,expected_byte",
    [
        # algorithm=0x00 disables on-device BIA; the scale streams
        # weight only (no stable-with-metrics frame). Used by the
        # library's bootstrap profile in weight-only and pre-resolution
        # detection mode.
        (0x00, False, 0x00),
        (0x00, True, 0x0A),
        # Bytes Renpho's app actually emits.
        (0x03, False, 0x03),
        (0x03, True, 0x0D),
        (0x04, False, 0x04),
        (0x04, True, 0x0E),
        # Other single-frequency variants the firmware accepts but
        # Renpho's app never selects.
        (0x01, False, 0x01),
        (0x02, False, 0x02),
        (0x05, False, 0x05),
        (0x06, False, 0x06),
        # Athlete bit just adds 0x0A regardless of the base byte.
        (0x01, True, 0x0B),
        (0x06, True, 0x10),
    ],
)
def test_user_profile_command_algorithm_to_flag_byte(
    algorithm, athlete, expected_byte
):
    """Byte 10 = (algorithm + (0x0A if athlete else 0)) & 0xFF."""
    cmd = build_user_profile_command(
        sex=0, age=43, height_m=1.70, athlete=athlete, algorithm=algorithm
    )
    assert cmd[10] == expected_byte


@pytest.mark.parametrize(
    "height_m,expected_h_hi,expected_h_lo",
    [
        # Whole-cm round-trips exactly: 1.70 m → 1700 mm = 0x06A4
        (1.70, 0x06, 0xA4),
        # Float-precision edge: 1.69 m → 1690 mm = 0x069A (must not become 1689)
        (1.69, 0x06, 0x9A),
        # Sub-cm precision is preserved (the library deliberately does
        # NOT truncate the way the Renpho app does).
        (1.707, 0x06, 0xAB),  # 1707 mm = 0x06AB
        # Sub-mm rounds to nearest mm
        (1.7074, 0x06, 0xAB),  # 1707.4 mm → 1707
        (1.7076, 0x06, 0xAC),  # 1707.6 mm → 1708
        # Larger and smaller values
        (1.85, 0x07, 0x3A),  # 1850 mm
        (1.45, 0x05, 0xAA),  # 1450 mm
    ],
)
def test_user_profile_command_height_m_preserves_user_precision(
    height_m, expected_h_hi, expected_h_lo
):
    cmd = build_user_profile_command(sex=0, age=43, height_m=height_m)
    assert cmd[8] == expected_h_hi
    assert cmd[9] == expected_h_lo


def test_user_profile_command_matches_observed_guest_capture():
    """Match the exact bytes captured from a real Renpho-app guest session.

    Source: full BLE capture of a Tourist-Mode measurement, profile:
    male, 43yo, 170cm, non-athlete, algorithm 0x03 (Kyle/Segal-family
    BIA — what Renpho's app sends to non-NA users).
    """
    cmd = build_user_profile_command(
        sex=0, age=43, height_m=1.70, athlete=False, algorithm=0x03
    )
    assert _cmd_hex(cmd) == "a00d02feffee002b06a4030274"


@pytest.mark.parametrize(
    "unit,expected_byte",
    [
        (WeightUnit.KG, 0x01),
        (WeightUnit.LB, 0x02),
        (WeightUnit.ST_LB, 0x08),
        (WeightUnit.ST, 0x10),
    ],
)
def test_unit_update_command_maps_each_weight_unit(unit, expected_byte):
    cmd = build_unit_update_command(unit)
    assert cmd[3] == expected_byte
    assert cmd[8] == sum(cmd[0:8]) & 0xFF


def test_parse_weight_unstable_frame_returns_only_weight():
    """status=0 frame: weight only, no body_fat/resistance fields."""
    # Real unstable frame: 100eff fe 00 1d47 0000 0000 0000 chk
    payload = bytearray.fromhex("100efffe001d4700000000000020")
    out = parse_weight(payload)
    assert out[WEIGHT_KEY] == 74.95
    assert BODY_FAT_KEY not in out
    assert RESISTANCE_1_KEY not in out
    assert RESISTANCE_2_KEY not in out


def test_parse_weight_stable_with_metrics_surfaces_resistance():
    """status=2 frame: resistance_1 and resistance_2 are exposed."""
    # Real frame from validation: M user, weight 74.95 kg, r1=505, r2=503,
    # bf 21.0% (algorithm 0x04 non-athlete).
    payload = bytearray.fromhex("100efffe021d4701f901f700d245")
    out = parse_weight(payload)
    assert out[WEIGHT_KEY] == 74.95
    assert out[BODY_FAT_KEY] == 21.0
    assert out[RESISTANCE_1_KEY] == 505
    assert out[RESISTANCE_2_KEY] == 503


def test_parse_weight_status_2_with_zero_bf_skips_body_fat():
    """A status-2 frame with body_fat=0 means BIA was disabled (algorithm
    0x00 bootstrap profile); no body_fat key, but resistance still
    surfaces if non-zero."""
    # Synthetic: status=2, weight 75.00, r1=500, r2=500, bf=0
    # bytes: 10 0e ff fe 02 1d 4c 01 f4 01 f4 00 00 chk
    body = bytearray.fromhex("100efffe021d4c01f401f40000")
    body.append(sum(body) & 0xFF)
    out = parse_weight(body)
    assert out[WEIGHT_KEY] == 75.00
    assert BODY_FAT_KEY not in out
    assert out[RESISTANCE_1_KEY] == 500
    assert out[RESISTANCE_2_KEY] == 500
