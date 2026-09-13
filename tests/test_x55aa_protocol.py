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

import dataclasses
import datetime
import logging

import pytest

from renpho_escs20m import ScaleProtocol, detect_protocol, is_x55aa_frame
from renpho_escs20m.body_metrics import Sex
from renpho_escs20m.data import WeightUnit
from renpho_escs20m.x55aa.protocol import (
    age_on,
    assert_allowed_write,
    build_display_unit_command,
    build_extended_stored_record_ack,
    build_guest_profile_command,
    build_set_time_command,
    checksum,
    CMD_EXTENDED_STORED_RECORD,
    ExtendedMeasurement,
    ExtendedStoredRecord,
    ForbiddenWrite,
    Frame,
    is_advertisement,
    is_extended_model,
    iter_frames,
    KNOWN_BASIC_MODEL_IDS,
    KNOWN_EXTENDED_MODEL_IDS,
    KNOWN_MODEL_IDS,
    MANUFACTURER_ID,
    Measurement,
    parse_extended_measurement,
    parse_extended_stored_record,
    parse_measurement,
    parse_model_id,
    parse_status,
    parse_stored_record,
    SET_TIME_MODEL_IDS,
    strip_fragment_header,
    X55AAProfile,
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


def test_detect_protocol_classifies_extended_flavor_model_ids():
    """Model ids 0x0031 (ESCS20MB2 / Elis 1) and 0x0030 (a sibling revision
    with identical frames) speak the extended flavor and are supported."""
    # 00 04 | model id | MAC echo | firmware bytes
    for adv, mac in (
        ("00040031cfea02079367" "0109", "CF:EA:02:07:93:67"),
        ("00040030cfea011922a4" "0109", "CF:EA:01:19:22:A4"),
    ):
        protocol = detect_protocol(
            "ES-CS20M", {MANUFACTURER_ID: bytes.fromhex(adv)}, mac
        )
        assert protocol is ScaleProtocol.X55AA


def test_detect_protocol_leaves_unsupported_model_ids_unclassified(caplog):
    """A 0x55aa-family unit with a model id we have never captured is left
    unclassified: connecting would hold its BLE link without a reading."""
    adv = bytes.fromhex("00040034cfea02079367" "0109")  # 0x0034: never captured
    with caplog.at_level(logging.WARNING):
        protocol = detect_protocol(
            "ES-CS20M", {MANUFACTURER_ID: adv}, "CF:EA:02:07:93:67"
        )
        assert protocol is None
        protocol = detect_protocol(
            "ES-CS20M", {MANUFACTURER_ID: adv}, "CF:EA:02:07:93:67"
        )
        assert protocol is None
    assert caplog.text.count("unsupported model identifier 0x0034 (52)") == 1


def test_detect_protocol_unsupported_model_id_does_not_fall_through_to_qn():
    """A 0x55aa frame is unambiguous family evidence: a QN-looking name or
    address must not reclassify an unsupported unit as QN."""
    adv = bytes.fromhex("00040035cfea0201c42e0109")
    for name, mac in (
        ("Renpho-Scale", "CF:EA:02:01:C4:2E"),
        ("QN-Scale", "CF:EA:02:01:C4:2E"),
        ("ES-CS20M", "FF:05:00:01:C4:2E"),
    ):
        payload = adv[:4] + bytes(int(o, 16) for o in mac.split(":")) + adv[10:]
        assert detect_protocol(name, {MANUFACTURER_ID: payload}, mac) is None


def test_model_id_registry():
    assert KNOWN_BASIC_MODEL_IDS == {0x0003}
    assert KNOWN_EXTENDED_MODEL_IDS == {0x0030, 0x0031}
    assert KNOWN_MODEL_IDS == KNOWN_BASIC_MODEL_IDS | KNOWN_EXTENDED_MODEL_IDS
    assert SET_TIME_MODEL_IDS == {0x0030, 0x0031}
    assert is_extended_model(0x0031)
    assert is_extended_model(0x0030)
    assert not is_extended_model(0x0003)
    assert not is_extended_model(None)


def test_parse_model_id():
    assert parse_model_id(ADVERTISEMENT) == 0x0003
    assert parse_model_id(b"\x00\x04") is None


# ---- extended flavor: 0x18 final --------------------------------------------

EXT_FINAL = bytes.fromhex("55aa18000d01010100001ec301df00ef00ecc3")
EXT_FINAL_NO_BIA = bytes.fromhex("55aa18000d010102000028aa0000013e000039")
EXT_FINAL_ZERO_CURRENT = bytes.fromhex("55aa18000d110109000027a60000000000000c")


def test_extended_final_decodes_weight_resistance_and_on_device_fields():
    (frame,) = _frames(EXT_FINAL)
    m = parse_extended_measurement(frame)
    assert m == ExtendedMeasurement(
        weight_kg=78.75, resistance=479, status=0x01, slot=1, bmi=23.9, body_fat=23.6
    )
    assert m.is_final and not m.zero_current


def test_extended_final_without_impedance_keeps_the_profile_derived_bmi():
    """Shoes on: the impedance pass yields nothing, but BMI is still computed
    from the profile height. Callers must not read BMI as evidence of BIA."""
    (frame,) = _frames(EXT_FINAL_NO_BIA)
    m = parse_extended_measurement(frame)
    assert (m.weight_kg, m.resistance, m.bmi, m.body_fat) == (104.10, 0, 31.8, 0.0)
    assert m.is_final


def test_extended_final_in_zero_current_mode():
    (frame,) = _frames(EXT_FINAL_ZERO_CURRENT)
    m = parse_extended_measurement(frame)
    assert m.weight_kg == 101.50 and m.resistance == 0 and m.slot == 9
    assert m.is_final and m.zero_current
    assert m.bmi == 0.0
    assert m.body_fat == 0.0


def test_parse_extended_measurement_rejects_other_frames():
    (frame,) = _frames(ES26_FINAL)
    assert parse_extended_measurement(frame) is None  # a 0x14
    short = b"\x55\xaa\x18\x00\x03\x01\x01\x01"
    (frame,) = _frames(short + bytes([checksum(short)]))
    assert parse_extended_measurement(frame) is None


# ---- extended flavor: 0x19 stored record, fragmented on the wire -----------

# Capture bytes: the 2026-09-06 app-sync session delivered three such records.
EXT_STORED = bytes.fromhex(
    "55aa190014" "11020102" "000027a6" "0000" "0000" "0000" "0000" "00000069" "78"
)
# 26 bytes exceed the default ATT MTU, so the scale sends each chunk behind
# a 3-byte header — in that capture, exactly ``AD 00 01`` + 17 bytes and
# ``AF 00 00`` + 9 bytes.
EXT_STORED_FRAG_1 = bytes.fromhex("ad0001") + EXT_STORED[:17]
EXT_STORED_FRAG_2 = bytes.fromhex("af0000") + EXT_STORED[17:]
EXT_STORED_ACK = bytes.fromhex("55aa990001019a")


def test_extended_golden_frames_checksum():
    for f in (EXT_FINAL, EXT_FINAL_NO_BIA, EXT_FINAL_ZERO_CURRENT, EXT_STORED):
        assert checksum(f[:-1]) == f[-1]


def test_extended_stored_record_decodes():
    (frame,) = _frames(EXT_STORED)
    record = parse_extended_stored_record(frame)
    assert record == ExtendedStoredRecord(
        weight_kg=101.50, resistance=0, seconds_ago=105, slot=2, status=0x11
    )
    assert record.zero_current


def test_strip_fragment_header_only_touches_marker_bytes():
    assert strip_fragment_header(EXT_STORED_FRAG_1) == EXT_STORED[:17]
    assert strip_fragment_header(EXT_STORED_FRAG_2) == EXT_STORED[17:]
    assert strip_fragment_header(EXT_FINAL) == EXT_FINAL  # plain frames untouched
    assert strip_fragment_header(b"\xad\x00") == b"\xad\x00"  # too short to be a header


def test_fragmented_stored_record_reassembles_through_iter_frames():
    buf = bytearray()
    buf.extend(strip_fragment_header(EXT_STORED_FRAG_1))
    assert iter_frames(buf) == []  # partial frame withheld
    buf.extend(strip_fragment_header(EXT_STORED_FRAG_2))
    frames = iter_frames(buf)
    assert len(frames) == 1 and frames[0].cmd == CMD_EXTENDED_STORED_RECORD
    assert parse_extended_stored_record(frames[0]).seconds_ago == 105


def test_parse_extended_stored_record_rejects_other_frames():
    (final,) = _frames(EXT_FINAL)
    assert parse_extended_stored_record(final) is None
    (basic_record,) = _frames(MB1_STORED_16_00)  # a basic-flavor 0x15
    assert parse_extended_stored_record(basic_record) is None
    short_payload = EXT_STORED[5:24]  # 19 bytes: one short of the exact length
    short_body = b"\x55\xaa\x19\x00\x13" + short_payload
    (short,) = _frames(short_body + bytes([checksum(short_body)]))
    assert parse_extended_stored_record(short) is None
    long_payload = EXT_STORED[5:25] + b"\x00"  # 21 bytes: one over the exact length
    long_body = b"\x55\xaa\x19\x00\x15" + long_payload
    (long_frame,) = _frames(long_body + bytes([checksum(long_body)]))
    assert parse_extended_stored_record(long_frame) is None


def test_build_extended_stored_record_ack():
    assert build_extended_stored_record_ack() == EXT_STORED_ACK


# ---- extended flavor: guest profile (0x96) ---------------------------------

GUEST_PROFILE_L0G1C5 = bytes.fromhex("55aa96000e1907c6010106a400002288a9ff058c")
GUEST_PROFILE_SURUBUTNA = bytes.fromhex("55aa96000e1907cb090106d600001b35aaff0572")


def test_guest_profile_reproduces_the_apps_frame_byte_for_byte():
    """From a capture of the official app: male, 1 Jan 1990, 170.0 cm,
    last weight 88.40 kg, algorithm 0x03."""
    profile = X55AAProfile(
        sex=Sex.Male,
        birthday=datetime.date(1990, 1, 1),
        height_m=1.70,
        last_weight_kg=88.40,
    )
    assert (
        build_guest_profile_command(profile, fallback_last_weight_kg=70.0)
        == GUEST_PROFILE_L0G1C5
    )


def test_guest_profile_falls_back_when_the_profile_has_no_last_weight():
    profile = X55AAProfile(
        sex=Sex.Male, birthday=datetime.date(1990, 1, 1), height_m=1.70
    )
    assert (
        build_guest_profile_command(profile, fallback_last_weight_kg=88.40)
        == GUEST_PROFILE_L0G1C5
    )


def test_guest_profile_algorithm_0x04_selects_the_other_on_device_method():
    """A second captured profile: same layout, low flag bits 2 → algorithm 0x04."""
    profile = X55AAProfile(
        sex=Sex.Male,
        birthday=datetime.date(1995, 9, 1),
        height_m=1.75,
        algorithm=0x04,
        last_weight_kg=69.65,
    )
    assert (
        build_guest_profile_command(profile, fallback_last_weight_kg=0.0)
        == GUEST_PROFILE_SURUBUTNA
    )


def test_guest_profile_field_encoding():
    profile = X55AAProfile(
        sex=Sex.Female,
        birthday=datetime.date(1988, 12, 6),
        height_m=1.57,
        athlete=True,
        last_weight_kg=84.40,
    )
    frame = build_guest_profile_command(profile, fallback_last_weight_kg=70.0)
    p = frame[5:-1]
    assert len(p) == 14
    assert p[0] == 0x29  # female = 2 in bits 5:4, guest slot 9 in the low nibble
    assert p[1:5] == bytes.fromhex("07c4" "0c" "06")  # 1988-12-06
    assert int.from_bytes(p[5:7], "big") == 1570  # 0.1 cm units
    assert int.from_bytes(p[7:11], "big") == 8440  # 0.01 kg units
    assert p[11] >> 6 == 1  # athlete flag
    assert p[11] & 0x03 == 1  # algorithm 0x03 → method 1
    assert p[12] == 0xFF  # the guest form's value
    assert p[13] == 0x05
    assert checksum(frame[:-1]) == frame[-1]


def test_age_on_rejects_a_birthday_in_the_future():
    with pytest.raises(ValueError):
        age_on(datetime.date(2030, 1, 1), datetime.date(2026, 9, 13))


def test_guest_profile_can_ask_the_scale_to_skip_the_impedance_pass():
    """One byte differs; the scale then returns no resistance and shows weight
    only on its own display."""
    profile = X55AAProfile(
        sex=Sex.Male, birthday=datetime.date(1990, 1, 1), height_m=1.70
    )
    on = build_guest_profile_command(profile, 88.40)
    off = build_guest_profile_command(profile, 88.40, measure_impedance=False)
    assert on == GUEST_PROFILE_L0G1C5
    assert off[:-2] == on[:-2]
    assert on[-2] == 0x05 and off[-2] == 0x06
    assert checksum(off[:-1]) == off[-1]


def test_guest_profile_rejects_unknown_algorithm_and_bad_height():
    good = X55AAProfile(sex=Sex.Male, birthday=datetime.date(1990, 1, 1), height_m=1.70)
    with pytest.raises(ValueError):
        build_guest_profile_command(dataclasses.replace(good, algorithm=0x00), 70.0)
    with pytest.raises(ValueError):
        build_guest_profile_command(dataclasses.replace(good, height_m=0.0), 70.0)
    with pytest.raises(ValueError):
        build_guest_profile_command(
            dataclasses.replace(good, last_weight_kg=-1.0), 70.0
        )
    with pytest.raises(ValueError):
        build_guest_profile_command(good, fallback_last_weight_kg=-1.0)


def test_age_on_is_birthday_aware():
    b = datetime.date(1990, 6, 15)
    assert age_on(b, datetime.date(2026, 6, 14)) == 35
    assert age_on(b, datetime.date(2026, 6, 15)) == 36
    assert age_on(b, datetime.date(2026, 12, 31)) == 36
    leap = datetime.date(2000, 2, 29)
    assert age_on(leap, datetime.date(2026, 2, 28)) == 25
    assert (
        age_on(leap, datetime.date(2026, 3, 1)) == 26
    )  # birthday rolls to Mar 1 in common years


# ---- set-time (0x97 sub-op 1) and the write allow-list ---------------------

SET_TIME_L0G1C5 = bytes.fromhex("55aa9700090100006a8084dd0002ed")


def test_set_time_reproduces_the_apps_frame():
    """From a capture: 2026-08-15T15:25:17Z from a phone in UTC+2."""
    assert (
        build_set_time_command(0x6A8084DD, utc_offset_seconds=7200) == SET_TIME_L0G1C5
    )
    assert build_set_time_command(0x6A8084DD, 7200)[5:-1] == bytes.fromhex(
        "01" "00006a8084dd" "00" "02"
    )


def test_set_time_encodes_negative_and_fractional_offsets():
    p = build_set_time_command(0x6A8084DD, utc_offset_seconds=-5 * 3600)[5:-1]
    assert p[7:9] == bytes([1, 5])
    p = build_set_time_command(0x6A8084DD, utc_offset_seconds=5 * 3600 + 1800)[5:-1]
    assert p[7:9] == bytes([0, 5])  # whole hours, like the app


def test_set_time_rejects_out_of_range():
    with pytest.raises(ValueError):
        build_set_time_command(-1, 0)
    with pytest.raises(ValueError):
        build_set_time_command(1 << 48, 0)
    with pytest.raises(ValueError):
        build_set_time_command(0x6A8084DD, 7200 * 1000)  # a milliseconds mistake
    assert (
        build_set_time_command(1786807517.9, 7200) == SET_TIME_L0G1C5
    )  # float coercion


def test_write_allow_list_accepts_what_the_library_sends():
    for frame in (
        build_display_unit_command(WeightUnit.KG),
        build_guest_profile_command(
            X55AAProfile(Sex.Male, datetime.date(1990, 1, 1), 1.70), 70.0
        ),
        build_set_time_command(0x6A8084DD, 7200),
        build_extended_stored_record_ack(),
        bytes.fromhex("55aa950001" "0196"),
    ):
        assert_allowed_write(frame)  # no exception


def test_write_allow_list_refuses_destructive_frames():
    # 0x90 with a mode byte: 1/2 overwrite the scale's persisted zero-current
    # setting, and mode 2 has no known exit on the extended flavor.
    for mode in (1, 2, 3):
        head = bytes([0x55, 0xAA, 0x90, 0x00, 0x04, 0x01, 0x00, mode, 0x00])
        with pytest.raises(ValueError):
            assert_allowed_write(head + bytes([checksum(head)]))
    mode2 = bytes([0x55, 0xAA, 0x90, 0x00, 0x04, 0x01, 0x00, 2, 0x00])
    with pytest.raises(ForbiddenWrite):
        assert_allowed_write(mode2 + bytes([checksum(mode2)]))
    # 0x97 in any form other than the 9-byte set-time payload.
    head = bytes.fromhex("55aa970002" "1001")
    with pytest.raises(ValueError):
        assert_allowed_write(head + bytes([checksum(head)]))
    # 0x96 addressing a registered slot.
    registered = bytearray(GUEST_PROFILE_L0G1C5)
    registered[5] = 0x11  # slot 1
    registered[-1] = checksum(registered[:-1])
    with pytest.raises(ValueError):
        assert_allowed_write(bytes(registered))
    # A guest profile whose trailer byte is neither form we send.
    odd_trailer = bytearray(GUEST_PROFILE_L0G1C5)
    odd_trailer[18] = 0x09
    odd_trailer[-1] = checksum(odd_trailer[:-1])
    with pytest.raises(ValueError):
        assert_allowed_write(bytes(odd_trailer))
    # An ack opcode carrying some other payload.
    head = bytes.fromhex("55aa990001" "02")
    with pytest.raises(ValueError):
        assert_allowed_write(head + bytes([checksum(head)]))
    # Anything not on the list.
    head = bytes.fromhex("55aa910001" "01")
    with pytest.raises(ValueError):
        assert_allowed_write(head + bytes([checksum(head)]))
    # Not a frame at all.
    with pytest.raises(ValueError):
        assert_allowed_write(b"\x01\x02\x03")
    # Deciding-condition cases.
    with pytest.raises(ValueError):  # right length, wrong magic
        assert_allowed_write(b"\x01\x02\x03\x04\x05\x06")
    head = bytes.fromhex("55aa900005" "0100000000")
    with pytest.raises(ValueError):  # 0x90 with a 5-byte payload, mode byte 0
        assert_allowed_write(head + bytes([checksum(head)]))
    guest_wrong_mask = bytearray(GUEST_PROFILE_L0G1C5)
    guest_wrong_mask[17] = 0x01  # payload[12]: not the guest form's 0xFF
    guest_wrong_mask[-1] = checksum(guest_wrong_mask[:-1])
    with pytest.raises(ValueError):
        assert_allowed_write(bytes(guest_wrong_mask))
    settings_head = bytes.fromhex("55aa970002" "1001")
    settings_frame = settings_head + bytes([checksum(settings_head)])
    with pytest.raises(ValueError):  # an allowed frame with a forbidden one appended
        assert_allowed_write(build_extended_stored_record_ack() + settings_frame)
