"""Tests for the raw command builders in :mod:`renpho_escs20m.qn.protocol`.

The library always drives the scale in guest mode, so
:func:`build_user_profile_command` emits ``0xa00d 02`` with the guest
sentinel triplet ``0xFE 0xFF 0xEE`` in bytes 3-5. Byte 10 selects the
BIA algorithm via ``(algorithm + (0x0A if athlete else 0)) & 0xFF``.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from renpho_escs20m import RenphoESCS20MScale
from renpho_escs20m.const import (
    RESISTANCE_1_KEY,
    RESISTANCE_2_KEY,
    WEIGHT_KEY,
)
from renpho_escs20m.data import WeightUnit
from renpho_escs20m.qn.protocol import (
    Profile,
    _EPOCH_OFFSET,
    _MEASUREMENT_STATUS_STABLE,
    _MEASUREMENT_STATUS_STABLE_WITH_METRICS,
    build_end_measurement_command,
    build_extended_stored_measurement_query,
    build_stored_measurement_query,
    build_unit_update_command,
    build_user_profile_command,
    parse_basic_measurement,
    parse_extended_measurement,
    parse_extended_stored_measurement,
    parse_stored_measurement,
)


def _make_scale(
    profile: Profile | AsyncMock | None = None,
    clear_stored_measurements: bool = False,
) -> tuple[RenphoESCS20MScale, MagicMock]:
    callback = MagicMock()
    scanner = MagicMock()
    scale = RenphoESCS20MScale(
        "00:11:22:33:44:55",
        callback,
        WeightUnit.KG,
        profile=profile,
        clear_stored_measurements=clear_stored_measurements,
        bleak_scanner_backend=scanner,
    )
    return scale, callback


def _measurement_payload(status: int) -> bytearray:
    """Build a synthetic measurement frame for resolver/end-command behavior."""
    payload = bytearray(14)
    payload[0] = 0x10
    payload[1] = 0x0E
    payload[2] = 0xFF
    payload[3] = 0xFE
    payload[4] = status
    payload[5:7] = int(74.45 * 100).to_bytes(2, "big")
    if status == _MEASUREMENT_STATUS_STABLE_WITH_METRICS:
        payload[7:9] = (500).to_bytes(2, "big")
        payload[9:11] = (500).to_bytes(2, "big")
        payload[11:13] = (210).to_bytes(2, "big")
    payload[13] = sum(payload[0:13]) & 0xFF
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

    scale._handle_extended_measurement(
        _measurement_payload(_MEASUREMENT_STATUS_STABLE),
        "Renpho ES-CS20M",
        "00:11:22:33:44:55",
    )
    await asyncio.sleep(0)
    scale._resolve_and_send_profile.assert_awaited_once_with(74.45, "00:11:22:33:44:55")

    scale._resolve_and_send_profile.reset_mock()
    scale._handle_extended_measurement(
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

    scale._handle_extended_measurement(
        _measurement_payload(_MEASUREMENT_STATUS_STABLE),
        "Renpho ES-CS20M",
        "00:11:22:33:44:55",
    )
    await asyncio.sleep(0)
    scale._safe_write.assert_not_awaited()

    scale._handle_extended_measurement(
        _measurement_payload(_MEASUREMENT_STATUS_STABLE_WITH_METRICS),
        "Renpho ES-CS20M",
        "00:11:22:33:44:55",
    )
    await asyncio.sleep(0)
    scale._safe_write.assert_awaited_once_with(build_end_measurement_command())
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
    athlete = build_user_profile_command(sex=0, age=43, height_m=1.70, athlete=True)
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
def test_user_profile_command_algorithm_to_flag_byte(algorithm, athlete, expected_byte):
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


def test_parse_extended_measurement_unstable_frame_returns_only_weight():
    """status=0 frame: weight only, no body_fat/resistance fields."""
    # Real unstable frame: 100eff fe 00 1d47 0000 0000 0000 chk
    payload = bytearray.fromhex("100efffe001d4700000000000020")
    out = parse_extended_measurement(payload)
    assert out.weight_kg == 74.95
    assert out.status == 0
    assert out.body_fat is None
    assert out.resistance_1 is None
    assert out.resistance_2 is None


def test_parse_extended_measurement_stable_with_metrics_surfaces_resistance():
    """status=2 frame: resistance_1 and resistance_2 are exposed."""
    # Real frame from validation: M user, weight 74.95 kg, r1=505, r2=503,
    # bf 21.0% (algorithm 0x04 non-athlete).
    payload = bytearray.fromhex("100efffe021d4701f901f700d245")
    out = parse_extended_measurement(payload)
    assert out.weight_kg == 74.95
    assert out.status == 2
    assert out.body_fat == 21.0
    assert out.resistance_1 == 505
    assert out.resistance_2 == 503


def test_parse_extended_measurement_status_2_with_zero_bf_skips_body_fat():
    """A status-2 frame with body_fat=0 means BIA was disabled (algorithm
    0x00 bootstrap profile); no body_fat key, but resistance still
    surfaces if non-zero."""
    # Synthetic: status=2, weight 75.00, r1=500, r2=500, bf=0
    # bytes: 10 0e ff fe 02 1d 4c 01 f4 01 f4 00 00 chk
    body = bytearray.fromhex("100efffe021d4c01f401f40000")
    body.append(sum(body) & 0xFF)
    out = parse_extended_measurement(body)
    assert out.weight_kg == 75.00
    assert out.status == 2
    assert out.body_fat is None
    assert out.resistance_1 == 500
    assert out.resistance_2 == 500


# --- ESCS20MN variant -----------------------------------------------------
# Frames below are real, captured from ESCS20MN hardware.


def test_parse_basic_measurement_decodes_final_frame():
    """Final frame (status 0x01): weight 55.05 kg, r1=508, r2=500."""
    frame = parse_basic_measurement(bytearray.fromhex("100bff15810101fc01f4a3"))
    assert frame.weight_kg == 55.05
    assert frame.status == 0x01
    assert frame.resistance_1 == 508
    assert frame.resistance_2 == 500


def test_parse_basic_measurement_settling_has_no_impedance():
    """Settling frame (status 0x00): weight only, impedance reads as 0."""
    frame = parse_basic_measurement(bytearray.fromhex("100bff1522000000000051"))
    assert frame.weight_kg == 54.10
    assert frame.status == 0x00
    assert frame.resistance_1 == 0
    assert frame.resistance_2 == 0


def _mn_scale() -> tuple[RenphoESCS20MScale, MagicMock]:
    scale, callback = _make_scale()
    scale._safe_write = AsyncMock()
    return scale, callback


@pytest.mark.asyncio
async def test_escs20mn_final_frame_fires_callback_and_ends():
    scale, callback = _mn_scale()
    scale._handle_basic_measurement(
        bytearray.fromhex("100bff15810101fc01f4a3"),
        "QN-Scale",
        "ff:03:00:67:0a:23",
    )
    await asyncio.sleep(0)
    assert callback.call_count == 1
    assert callback.call_args[0][0].measurements == {
        WEIGHT_KEY: 55.05,
        RESISTANCE_1_KEY: 508,
        RESISTANCE_2_KEY: 500,
    }
    scale._safe_write.assert_awaited_once_with(build_end_measurement_command())


@pytest.mark.asyncio
async def test_escs20mn_settling_and_bia_frames_do_not_fire():
    scale, callback = _mn_scale()
    for hx in ("100bff1522000000000051", "100bff15811100000000c1"):
        scale._handle_basic_measurement(bytearray.fromhex(hx), "QN-Scale", "addr")
        await asyncio.sleep(0)
    callback.assert_not_called()
    scale._safe_write.assert_not_awaited()


@pytest.mark.asyncio
async def test_escs20mn_duplicate_final_frame_fires_once():
    scale, callback = _mn_scale()
    frame = bytearray.fromhex("100bff15810101fc01f4a3")
    scale._handle_basic_measurement(frame, "QN-Scale", "addr")
    scale._handle_basic_measurement(frame, "QN-Scale", "addr")
    await asyncio.sleep(0)
    assert callback.call_count == 1
    scale._safe_write.assert_awaited_once_with(build_end_measurement_command())


@pytest.mark.asyncio
async def test_escs20mn_unexpected_status_is_ignored():
    scale, callback = _mn_scale()
    # status 0xAB is not one of the known 0x00 / 0x11 / 0x01 values.
    scale._handle_basic_measurement(
        bytearray.fromhex("100bff1581ab01fc01f400"), "QN-Scale", "addr"
    )
    await asyncio.sleep(0)
    callback.assert_not_called()
    scale._safe_write.assert_not_awaited()


@pytest.mark.asyncio
async def test_escs20mn_profile_request_sends_nothing():
    """The MN variant takes no profile over BLE; the request is a no-op."""
    scale, _ = _mn_scale()
    scale._handle_basic_pre_measurement("addr")
    await asyncio.sleep(0)
    scale._safe_write.assert_not_awaited()


@pytest.mark.asyncio
async def test_notification_handler_replies_to_escs20mn_unit_request():
    scale, _ = _mn_scale()
    scale._notification_handler(
        MagicMock(),
        bytearray.fromhex("1211ff230a670003ff03030500000507cf"),
        "QN-Scale",
        "ff:03:00:67:0a:23",
    )
    await asyncio.sleep(0)
    scale._safe_write.assert_awaited_once()
    assert scale._safe_write.await_args[0][0].hex().startswith("1309ff")


@pytest.mark.asyncio
async def test_notification_handler_replies_to_base_unit_request():
    scale, _ = _mn_scale()
    scale._notification_handler(
        MagicMock(),
        bytearray.fromhex("1212ff0100"),
        "Renpho ES-CS20M",
        "addr",
    )
    await asyncio.sleep(0)
    scale._safe_write.assert_awaited_once()
    assert scale._safe_write.await_args[0][0].hex().startswith("1309ff")


@pytest.mark.asyncio
async def test_other_brand_vendor_byte_is_detected_and_echoed(caplog):
    """A non-renpho QN-Scale with vendor byte 0x15 (not renpho's 0xFF): the
    byte is detected from the wire and echoed back in our replies, and the
    non-renpho byte is surfaced once."""
    caplog.set_level(logging.INFO, logger="renpho_escs20m.scale")
    scale, callback = _mn_scale()

    # Unit request from a non-renpho scale: opcode 0x12, vendor byte 0x15.
    scale._notification_handler(
        MagicMock(), bytearray.fromhex("120f15" + "00" * 12), "QN-Scale", "addr"
    )
    await asyncio.sleep(0)
    assert scale._vendor_byte == 0x15
    set_unit = scale._safe_write.await_args[0][0]
    assert set_unit[2] == 0x15
    assert set_unit.hex().startswith("130915")

    scale._safe_write.reset_mock()

    # Basic-flavor final measurement with vendor byte 0x15:
    # weight 55.05 kg, r1=508, r2=500.
    scale._notification_handler(
        MagicMock(), bytearray.fromhex("100b1515810101fc01f4b9"), "QN-Scale", "addr"
    )
    await asyncio.sleep(0)
    assert callback.call_count == 1
    assert callback.call_args[0][0].measurements == {
        WEIGHT_KEY: 55.05,
        RESISTANCE_1_KEY: 508,
        RESISTANCE_2_KEY: 500,
    }
    # End-measurement echoes the scale's vendor byte (0x15), not renpho's 0xFF.
    assert scale._safe_write.await_args[0][0].hex() == "1f05151049"

    # The non-renpho byte is surfaced exactly once, not re-logged per frame.
    vendor_logs = [r for r in caplog.records if "vendor byte" in r.getMessage()]
    assert len(vendor_logs) == 1


@pytest.mark.asyncio
async def test_pre_measurement_len5_sends_profile_for_renpho():
    """Renpho's extended pre-measurement (21 05 ff) still gets a guest profile."""
    scale, _ = _mn_scale()
    scale._notification_handler(
        MagicMock(), bytearray.fromhex("2105ff0025"), "Renpho ES-CS20M", "addr"
    )
    await asyncio.sleep(0)
    scale._safe_write.assert_awaited_once()
    assert scale._safe_write.await_args[0][0].hex().startswith("a00d02")


@pytest.mark.asyncio
async def test_pre_measurement_len5_skips_profile_for_non_renpho():
    """A non-renpho scale's 21 05 (vendor 0x15) is treated as basic — the
    renpho-specific guest profile is NOT sent."""
    scale, _ = _mn_scale()
    # Unit request first, so the 0x15 vendor byte is captured.
    scale._notification_handler(
        MagicMock(), bytearray.fromhex("120f15" + "00" * 12), "QN-Scale1", "addr"
    )
    # Pre-measurement with length 0x05 but a non-renpho vendor byte.
    scale._notification_handler(
        MagicMock(), bytearray.fromhex("210515013c"), "QN-Scale1", "addr"
    )
    await asyncio.sleep(0)
    sent = [c.args[0].hex() for c in scale._safe_write.call_args_list]
    assert not any(h.startswith("a00d02") for h in sent), sent


# --- Stored offline measurements (22 04 query / 23 13 records) -------------
#
# All frames below are real captured bytes from Renpho-app sessions. The
# scale answers the query with one record per offline reading, newest
# first; delivering a record deletes it from the scale's store.


def test_stored_measurement_query_matches_capture():
    assert build_stored_measurement_query().hex() == "2204ff25"


def test_stored_measurement_query_echoes_vendor_byte():
    cmd = build_stored_measurement_query(0x15)
    assert cmd.hex() == "2204153b"
    assert cmd[-1] == sum(cmd[:-1]) & 0xFF


@pytest.mark.parametrize(
    "hx,count,index,ts_raw,weight,r1,r2",
    [
        ("2313ff04015dcdb6311cd901fc01f00000002e", 4, 1, 0x31B6CD5D, 73.85, 508, 496),
        ("2313ff04023dcdb6311c7001f001f6000000a0", 4, 2, 0x31B6CD3D, 72.80, 496, 502),
        ("2313ff0403e5ccb6311d4201ef01f60000001a", 4, 3, 0x31B6CCE5, 74.90, 495, 502),
        ("2313ff0404cfccb6311d4201fc01f10000000d", 4, 4, 0x31B6CCCF, 74.90, 508, 497),
        ("2313ff0101d4cdb6311d2901f601fa000000f7", 1, 1, 0x31B6CDD4, 74.65, 502, 506),
    ],
)
def test_parse_stored_measurement_decodes_captured_records(
    hx, count, index, ts_raw, weight, r1, r2
):
    frame = parse_stored_measurement(bytearray.fromhex(hx))
    assert frame.count == count
    assert frame.index == index
    assert frame.timestamp == ts_raw + _EPOCH_OFFSET
    assert frame.weight_kg == weight
    assert frame.resistance_1 == r1
    assert frame.resistance_2 == r2


@pytest.mark.parametrize(
    "hx",
    [
        # basic flavor
        "2313ff000094b7b63100000000000000000067",
        # extended flavor (trailing bytes laid out slightly differently,
        # but count=0 at byte 3 is all that matters)
        "2313ff0000004b2e7e3100000000000000005d",
    ],
)
def test_parse_stored_measurement_empty_store(hx):
    """count=0 means the store is empty; the other fields are meaningless."""
    frame = parse_stored_measurement(bytearray.fromhex(hx))
    assert frame.count == 0


def test_extended_stored_measurement_query_matches_capture():
    """Guest-session extended query: payload 00 01 (the guest form)."""
    assert build_extended_stored_measurement_query().hex() == "2206ff000128"


def test_extended_stored_measurement_query_echoes_vendor_byte():
    cmd = build_extended_stored_measurement_query(0x15)
    assert cmd.hex() == "22061500013e"
    assert cmd[-1] == sum(cmd[:-1]) & 0xFF


@pytest.mark.asyncio
async def test_basic_pre_measurement_sends_stored_query_when_enabled():
    """On the basic flavor the query follows the 21 04 pre-measurement
    frame; the meas-init reply alone must not trigger it."""
    scale, _ = _make_scale(clear_stored_measurements=True)
    scale._safe_write = AsyncMock()
    scale._notification_handler(
        MagicMock(), bytearray.fromhex("140bff000001000000001f"), "QN-Scale", "addr"
    )
    await asyncio.sleep(0)
    sent = [c.args[0].hex() for c in scale._safe_write.call_args_list]
    assert any(h.startswith("2008ff") for h in sent), sent
    assert not any(h.startswith("22") for h in sent), sent

    scale._notification_handler(
        MagicMock(), bytearray.fromhex("2104ff0125"), "QN-Scale", "addr"
    )
    await asyncio.sleep(0)
    sent = [c.args[0].hex() for c in scale._safe_write.call_args_list]
    assert "2204ff25" in sent, sent


@pytest.mark.asyncio
async def test_extended_profile_ack_sends_extended_stored_query_when_enabled():
    """On the extended flavor the query follows the a1 profile ack."""
    scale, _ = _make_scale(clear_stored_measurements=True)
    scale._safe_write = AsyncMock()
    scale._notification_handler(
        MagicMock(), bytearray.fromhex("2105ff0126"), "Renpho ES-CS20M", "addr"
    )
    await asyncio.sleep(0)
    sent = [c.args[0].hex() for c in scale._safe_write.call_args_list]
    assert any(h.startswith("a00d02") for h in sent), sent
    assert not any(h.startswith("22") for h in sent), sent

    scale._notification_handler(
        MagicMock(), bytearray.fromhex("a10602fe01a8"), "Renpho ES-CS20M", "addr"
    )
    await asyncio.sleep(0)
    sent = [c.args[0].hex() for c in scale._safe_write.call_args_list]
    assert "2206ff000128" in sent, sent


@pytest.mark.asyncio
async def test_stored_query_sent_once_per_session():
    scale, _ = _make_scale(clear_stored_measurements=True)
    scale._safe_write = AsyncMock()
    for hx in ("2104ff0125", "2104ff0125", "a10602fe01a8"):
        scale._notification_handler(
            MagicMock(), bytearray.fromhex(hx), "QN-Scale", "addr"
        )
        await asyncio.sleep(0)
    sent = [c.args[0].hex() for c in scale._safe_write.call_args_list]
    assert sum(h.startswith("22") for h in sent) == 1, sent


@pytest.mark.asyncio
async def test_stored_query_not_sent_by_default():
    scale, _ = _make_scale()
    scale._safe_write = AsyncMock()
    for hx in (
        "140bff000001000000001f",
        "2104ff0125",
        "2105ff0126",
        "a10602fe01a8",
    ):
        scale._notification_handler(
            MagicMock(), bytearray.fromhex(hx), "QN-Scale", "addr"
        )
        await asyncio.sleep(0)
    sent = [c.args[0].hex() for c in scale._safe_write.call_args_list]
    assert not any(h.startswith("22") for h in sent), sent


@pytest.mark.asyncio
async def test_stored_measurement_frames_never_fire_callback():
    """Drained records are discarded; a live measurement afterwards still
    goes through untouched."""
    scale, callback = _make_scale(clear_stored_measurements=True)
    scale._safe_write = AsyncMock()
    for hx in (
        "2313ff04015dcdb6311cd901fc01f00000002e",
        "2313ff0404cfccb6311d4201fc01f10000000d",
        "2313ff000094b7b63100000000000000000067",
    ):
        scale._notification_handler(MagicMock(), bytearray.fromhex(hx), "QN", "addr")
    await asyncio.sleep(0)
    callback.assert_not_called()
    scale._safe_write.assert_not_awaited()

    # Live basic-flavor final frame still fires the callback normally.
    scale._notification_handler(
        MagicMock(), bytearray.fromhex("100bff15810101fc01f4a3"), "QN", "addr"
    )
    await asyncio.sleep(0)
    assert callback.call_count == 1


@pytest.mark.asyncio
async def test_stored_measurement_unexpected_length_is_ignored():
    scale, callback = _make_scale(clear_stored_measurements=True)
    scale._safe_write = AsyncMock()
    # Truncated 0x23 frame (length byte says 0x13 but payload is short).
    scale._notification_handler(
        MagicMock(), bytearray.fromhex("2313ff0401"), "QN", "addr"
    )
    await asyncio.sleep(0)
    callback.assert_not_called()
    scale._safe_write.assert_not_awaited()


# --- Extended-flavor stored records ----------------------------------------
#
# The extended flavor shifts the record fields by one byte (store-user-index
# at offset 5) and appends the on-device body-fat result at 16-17.


def test_parse_extended_stored_measurement_layout():
    frame = parse_extended_stored_measurement(
        bytearray.fromhex("2313ff0201f04b2e7e311cd901f401f600e617")
    )
    assert frame.count == 2
    assert frame.index == 1
    assert frame.user_index == 0xF0  # record not assigned to a user slot
    assert frame.timestamp == 0x317E2E4B + _EPOCH_OFFSET
    assert frame.weight_kg == 73.85
    assert frame.resistance_1 == 500
    assert frame.resistance_2 == 502
    assert frame.body_fat == 23.0


def test_parse_extended_stored_measurement_zero_body_fat_is_none():
    """A zero body-fat field is reported as None (not 0.0)."""
    frame = parse_extended_stored_measurement(
        bytearray.fromhex("2313ff0202024b2e7e311c7001f001f60000d7")
    )
    assert frame.user_index == 2
    assert frame.weight_kg == 72.80
    assert frame.resistance_1 == 496
    assert frame.resistance_2 == 502
    assert frame.body_fat is None


@pytest.mark.asyncio
async def test_failed_profile_ack_sends_no_stored_query():
    """The extended query follows only a *successful* (status 0x01) ack."""
    scale, _ = _make_scale(clear_stored_measurements=True)
    scale._safe_write = AsyncMock()
    scale._notification_handler(
        MagicMock(), bytearray.fromhex("a10602fe00a7"), "Renpho ES-CS20M", "addr"
    )
    await asyncio.sleep(0)
    sent = [c.args[0].hex() for c in scale._safe_write.call_args_list]
    assert not any(h.startswith("22") for h in sent), sent


@pytest.mark.asyncio
async def test_extended_session_records_are_discarded_without_callback():
    scale, callback = _make_scale(clear_stored_measurements=True)
    scale._safe_write = AsyncMock()
    scale._notification_handler(
        MagicMock(), bytearray.fromhex("a10602fe01a8"), "Renpho ES-CS20M", "addr"
    )
    await asyncio.sleep(0)
    assert scale._stored_records_extended
    for hx in (
        "2313ff0201f04b2e7e311cd901f401f600e617",
        "2313ff0000004b2e7e3100000000000000005d",
    ):
        scale._notification_handler(
            MagicMock(), bytearray.fromhex(hx), "Renpho ES-CS20M", "addr"
        )
    await asyncio.sleep(0)
    callback.assert_not_called()
