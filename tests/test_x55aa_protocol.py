"""Tests for the 0x55aa GATT variant's wire protocol (LeFu hardware).

Golden vectors are real notification frames from three independent
units' captures:

- ES-CS20MB1 (FCC ``2A26P-ESCS20MB1``, firmware ``BK_V17_1510``): one
  weigh-in that settled at 61.05 kg, captured with 7 stored offline
  records pending — several of which arrive interleaved with the live
  session.
- R-A012 (FCC ``2A26P-RA012N``): a full session with an empty offline
  store (settling ramp + final at 70.25 kg / 586 ohm).
- ES-26BB-B (HVIN ``ES26BBB``): final at 104.60 kg / 652 ohm.
"""

from __future__ import annotations

import logging

from renpho_escs20m import ScaleProtocol, detect_protocol, is_x55aa_frame
from renpho_escs20m.data import WeightUnit
from renpho_escs20m.x55aa.protocol import (
    MANUFACTURER_ID,
    parse_model_id,
    Frame,
    Measurement,
    checksum,
    is_advertisement,
    iter_frames,
    parse_measurement,
    parse_status,
    parse_stored_record,
    build_display_unit_command,
)

ADDRESS = "CF:E8:FC:05:22:0D"
ADVERTISEMENT = bytes.fromhex("00040003cfe8fc05220d0101")

# ES-CS20MB1 capture (offline store: 7 pending records).
MB1_STATUS = bytes.fromhex("55aa11000501010107001f")
MB1_STATUS_ALT = bytes.fromhex("55aa120005010101070020")
MB1_STORED_16_00 = bytes.fromhex("55aa15000c0000064003570000047e000042")
MB1_STORED_15_10 = bytes.fromhex("55aa15000c000005e60310000002830000a3")
MB1_STORED_60_95 = bytes.fromhex(
    "55aa15000c000017cf03690000026700 00db".replace(" ", "")
)
MB1_STORED_60_90 = bytes.fromhex("55aa15000c000017ca0363000001470000af")
MB1_FINAL = bytes.fromhex("55aa14000701000017d9035664")

MB1_SESSION = [
    MB1_STATUS,
    MB1_STORED_16_00,
    MB1_STATUS_ALT,
    MB1_STORED_15_10,
    MB1_STORED_60_95,
    MB1_STORED_60_90,
    MB1_FINAL,
]

# R-A012 capture (offline store empty; no 0x15 frames in the session).
RA012_STATUS = bytes.fromhex("55aa110005010001000017")
RA012_SETTLING = bytes.fromhex("55aa1400070000001b990000ce")
RA012_FINAL = bytes.fromhex("55aa1400070100001b71024af3")
# Constant non-55aa junk frame observed interleaved on the same
# characteristic on this unit; fails resync and must be skipped.
RA012_JUNK = bytes.fromhex("20a4200020102c002000000000")

# ES-26BB-B capture.
ES26_FINAL = bytes.fromhex("55aa14000701000028dc028cad")

ALL_FRAMES = MB1_SESSION + [RA012_STATUS, RA012_SETTLING, RA012_FINAL, ES26_FINAL]


def _frames(stream: bytes) -> list[Frame]:
    return iter_frames(bytearray(stream))


# ---- framing ---------------------------------------------------------------


def test_checksums_validate_on_every_captured_frame():
    for frame in ALL_FRAMES:
        assert checksum(frame[:-1]) == frame[-1]


def test_frame_size_is_six_plus_the_16_bit_length_on_every_captured_frame():
    for frame in ALL_FRAMES:
        assert len(frame) == 6 + int.from_bytes(frame[3:5], "big")


def test_iter_frames_splits_a_session_stream():
    frames = _frames(b"".join(MB1_SESSION))
    assert [f.cmd for f in frames] == [0x11, 0x15, 0x12, 0x15, 0x15, 0x15, 0x14]


def test_iter_frames_reassembles_across_notification_boundaries():
    buffer = bytearray()
    frames = []
    for byte in b"".join(MB1_SESSION):
        buffer.append(byte)
        frames.extend(iter_frames(buffer))
    assert len(frames) == 7
    assert not buffer


def test_iter_frames_withholds_a_partial_frame():
    buffer = bytearray(MB1_STORED_16_00[:9])
    assert iter_frames(buffer) == []
    assert len(buffer) == 9


def test_iter_frames_resynchronises_after_garbage():
    frames = _frames(RA012_JUNK + RA012_FINAL + RA012_JUNK)
    assert [f.cmd for f in frames] == [0x14]


def test_iter_frames_skips_a_false_sync_with_an_absurd_length():
    # A corrupted stream can contain 55 aa followed by garbage that reads
    # as a huge 16-bit length; parsing must skip it rather than stall
    # waiting for kilobytes that never arrive.
    false_sync = b"\x55\xaa\x14\xff\xff"
    frames = _frames(false_sync + MB1_FINAL)
    assert [f.cmd for f in frames] == [0x14]


def test_iter_frames_drops_a_corrupt_frame_and_recovers():
    corrupt = bytearray(MB1_STORED_16_00)
    corrupt[-1] ^= 0xFF
    frames = _frames(bytes(corrupt) + MB1_FINAL)
    assert [f.cmd for f in frames] == [0x14]


# ---- 0x14 measurement ------------------------------------------------------


def test_final_measurement_decodes_weight_resistance_and_status():
    (frame,) = _frames(MB1_FINAL)
    measurement = parse_measurement(frame)
    assert measurement == Measurement(weight_kg=61.05, resistance=854, status=0x01)
    assert measurement.is_final
    assert not measurement.is_settling


def test_settling_measurement_has_zero_resistance_and_is_not_final():
    (frame,) = _frames(RA012_SETTLING)
    measurement = parse_measurement(frame)
    assert measurement == Measurement(weight_kg=70.65, resistance=0, status=0x00)
    assert measurement.is_settling
    assert not measurement.is_final


def test_es26bbb_golden_final():
    (frame,) = _frames(ES26_FINAL)
    measurement = parse_measurement(frame)
    assert measurement.weight_kg == 104.60
    assert measurement.resistance == 652
    assert measurement.is_final


def test_only_the_observed_statuses_classify():
    """Low nibble = phase (0 settling, 1 final, anything else unclassified);
    bit 4 = the scale's zero-current (pregnancy) mode, a persisted setting
    the app writes via 0x90 — R-A016 probe run, 2026-09-03."""
    assert Measurement(60.0, 0, status=0x00).is_settling
    assert Measurement(60.0, 500, status=0x01).is_final
    assert Measurement(60.0, 0, status=0x10).is_settling
    assert Measurement(60.0, 0, status=0x11).is_final
    for status in (0x00, 0x01):
        assert not Measurement(60.0, 0, status=status).zero_current
    for status in (0x10, 0x11, 0x12):
        assert Measurement(60.0, 0, status=status).zero_current
    for status in (0x02, 0x05, 0x12):
        measurement = Measurement(weight_kg=60.0, resistance=0, status=status)
        assert not measurement.is_final
        assert not measurement.is_settling


def test_ra016_zero_current_capture_frames():
    """R-A016 official-app capture (integration issue #20): the app had put
    the scale in zero-current mode, so settling is 0x10 and the final 0x11,
    both with resistance 0."""
    (settling,) = _frames(bytes.fromhex("55aa14000710000023b9000006"))
    m = parse_measurement(settling)
    assert m == Measurement(weight_kg=91.45, resistance=0, status=0x10)
    assert m.is_settling and m.zero_current and not m.is_final
    (final,) = _frames(bytes.fromhex("55aa14000711000023b9000007"))
    m = parse_measurement(final)
    assert m == Measurement(weight_kg=91.45, resistance=0, status=0x11)
    assert m.is_final and m.zero_current


def test_ra016_normal_mode_final_from_probe_run():
    """Same unit after a 0x90 mode-1 write: plain 0x01 final with impedance."""
    (final,) = _frames(bytes.fromhex("55aa14000701000023b903110b"))
    m = parse_measurement(final)
    assert m == Measurement(weight_kg=91.45, resistance=785, status=0x01)
    assert m.is_final and not m.zero_current


def test_build_display_unit_command_is_the_apps_mode_zero_form():
    """0x90 [unit, 0, 0, 0] — what the app sends every non-R-A016 model, and
    verified on the R-A016 to change the unit without touching the stored
    zero-current mode. kg vector = the R-A012 app capture."""
    assert build_display_unit_command(WeightUnit.KG).hex() == "55aa9000040100000094"
    assert build_display_unit_command(WeightUnit.LB).hex() == "55aa9000040200000095"
    assert build_display_unit_command(WeightUnit.ST_LB).hex() == "55aa9000040300000096"
    assert build_display_unit_command(WeightUnit.ST).hex() == "55aa9000040400000097"


def test_parse_measurement_rejects_other_frames():
    (frame,) = _frames(MB1_STORED_16_00)
    assert parse_measurement(frame) is None


# ---- 0x15 stored records ---------------------------------------------------


def test_stored_record_decodes_weight_resistance_and_age():
    (frame,) = _frames(MB1_STORED_16_00)
    record = parse_stored_record(frame)
    assert record is not None
    assert record.weight_kg == 16.00
    assert record.resistance == 855
    assert record.seconds_ago == 1150


def test_stored_records_arrive_oldest_first():
    stream = b"".join(
        [MB1_STORED_16_00, MB1_STORED_15_10, MB1_STORED_60_95, MB1_STORED_60_90]
    )
    ages = [parse_stored_record(f).seconds_ago for f in _frames(stream)]
    assert ages == [1150, 643, 615, 327]
    assert ages == sorted(ages, reverse=True)


def test_parse_stored_record_long_form_carries_the_mode_flag():
    """R-A016 (probe run 2026-09-03): a 14-byte payload whose byte 12 is the
    zero-current flag of the stored weigh-in; the 12-byte form has none."""
    (frame,) = _frames(bytes.fromhex("55aa15000e000023b9000000000020000001001f"))
    record = parse_stored_record(frame)
    assert (record.weight_kg, record.resistance, record.seconds_ago) == (91.45, 0, 32)
    assert record.mode_flag == 1
    (short,) = _frames(MB1_STORED_16_00)
    assert parse_stored_record(short).mode_flag is None


def test_parse_stored_record_rejects_other_frames():
    (frame,) = _frames(MB1_FINAL)
    assert parse_stored_record(frame) is None


# ---- 0x11 status -----------------------------------------------------------


def test_status_decodes_power_unit_count_and_battery():
    (frame,) = _frames(MB1_STATUS)
    status = parse_status(frame)
    assert status is not None
    assert status.power_on
    assert status.display_unit is WeightUnit.KG
    assert status.stored_count == 7
    assert status.byte4 == 0


def test_status_unit_mapping():
    for wire, expected in (
        (1, WeightUnit.KG),
        (2, WeightUnit.LB),
        (3, WeightUnit.ST_LB),
        (4, WeightUnit.ST),
        (0, None),
        (9, None),
    ):
        payload = bytes([1, wire, 1, 0, 0])
        status = parse_status(Frame(0x11, payload))
        assert status.display_unit == expected


def test_parse_status_ignores_the_0x12_variant():
    (frame,) = _frames(MB1_STATUS_ALT)
    assert parse_status(frame) is None


# ---- advertisement / detection ---------------------------------------------


def test_advertisement_mac_echo_matches_the_address():
    assert is_advertisement(ADVERTISEMENT, ADDRESS)


def test_advertisement_rejects_a_different_address():
    assert not is_x55aa_frame(ADVERTISEMENT, "AA:BB:CC:DD:EE:FF")


def test_advertisement_accepts_a_non_mac_address():
    """macOS reports a CoreBluetooth UUID, so the echo cannot be checked."""
    assert is_x55aa_frame(ADVERTISEMENT, "8C4F1B12-3A5E-4C7D-9F01-2B3C4D5E6F70")


def test_advertisement_requires_the_constant_prefix():
    assert not is_advertisement(b"\x10\x1a" + ADVERTISEMENT[2:], ADDRESS)


def test_advertisement_rejects_a_short_payload():
    assert not is_x55aa_frame(ADVERTISEMENT[:6], ADDRESS)


def test_detect_protocol_classifies_the_advertisement():
    protocol = detect_protocol("ES-CS20M", {MANUFACTURER_ID: ADVERTISEMENT}, ADDRESS)
    assert protocol is ScaleProtocol.X55AA


def test_detect_protocol_ignores_a_foreign_address():
    protocol = detect_protocol(
        "ES-CS20M", {MANUFACTURER_ID: ADVERTISEMENT}, "AA:BB:CC:DD:EE:FF"
    )
    assert protocol is None


def test_detect_protocol_leaves_unsupported_model_ids_unclassified(caplog):
    """Extended-flavor units (other model ids) speak frames this client
    does not parse; classifying them would hold their BLE link without
    ever producing a reading."""
    # Real extended-flavor advertisements: model ids 0x0031 and 0x0030.
    for adv, mac in (
        ("00040031cfea0201c42e0109", "CF:EA:02:01:C4:2E"),
        ("00040030cfea011922a40109", "CF:EA:01:19:22:A4"),
    ):
        with caplog.at_level(logging.WARNING):
            protocol = detect_protocol(
                "ES-CS20M", {MANUFACTURER_ID: bytes.fromhex(adv)}, mac
            )
        assert protocol is None
    assert sum("unsupported model identifier" in r.message for r in caplog.records) == 2
    assert "0x0031 (49)" in caplog.text


def test_detect_protocol_unsupported_model_id_does_not_fall_through_to_qn():
    """A 0x55aa frame is unambiguous family evidence: a QN-looking name or
    address must not reclassify an unsupported unit as QN."""
    adv = bytes.fromhex("00040031cfea0201c42e0109")
    for name, mac in (
        ("Renpho-Scale", "CF:EA:02:01:C4:2E"),
        ("QN-Scale", "CF:EA:02:01:C4:2E"),
        ("ES-CS20M", "FF:05:00:01:C4:2E"),
    ):
        payload = adv[:4] + bytes(int(o, 16) for o in mac.split(":")) + adv[10:]
        assert detect_protocol(name, {MANUFACTURER_ID: payload}, mac) is None


def test_parse_model_id():
    assert parse_model_id(ADVERTISEMENT) == 0x0003
    assert parse_model_id(b"\x00\x04") is None
