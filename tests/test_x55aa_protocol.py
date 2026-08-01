"""Tests for the 0x55aa GATT variant (LeFu hardware).

Golden vectors are real notification frames captured from an ES-CS20M with
HVIN ``ES-CS20MB1`` (FCC ID ``2A26P-ESCS20MB1``), firmware ``BK_V17_1510``,
during a single weigh-in that settled at 60.90 kg.
"""

from __future__ import annotations

from renpho_escs20m import ScaleProtocol, detect_protocol, is_x55aa_frame
from renpho_escs20m.x55aa.protocol import (
    MANUFACTURER_ID,
    Reading,
    aggregate_session,
    checksum,
    iter_frames,
    parse_reading,
)

ADDRESS = "CF:E8:FC:05:22:0D"
ADVERTISEMENT = bytes.fromhex("00040003cfe8fc05220d0101")

STATUS_11 = bytes.fromhex("55aa1100050101010700 1f".replace(" ", ""))
STATUS_12 = bytes.fromhex("55aa120005010101070020")
LIVE_1600 = bytes.fromhex("55aa15000c0000064003570000047e000042")
LIVE_1510 = bytes.fromhex("55aa15000c000005e6031000000283000 0a3".replace(" ", ""))
LIVE_6095 = bytes.fromhex("55aa15000c000017cf036900000267 0000db".replace(" ", ""))
LIVE_6090 = bytes.fromhex("55aa15000c000017ca0363000001470000af")
STABLE_6105 = bytes.fromhex("55aa14000701000017d9035664")

WEIGH_IN = [
    STATUS_11,
    LIVE_1600,
    STATUS_12,
    LIVE_1510,
    LIVE_6095,
    LIVE_6090,
    STABLE_6105,
]
STREAM = b"".join(WEIGH_IN)


def _readings(stream: bytes) -> list[Reading]:
    frames = iter_frames(bytearray(stream))
    return [r for r in (parse_reading(f) for f in frames) if r is not None]


def test_checksums_validate_on_every_captured_frame():
    for frame in WEIGH_IN:
        assert checksum(frame[:-1]) == frame[-1]


def test_iter_frames_splits_the_capture():
    frames = iter_frames(bytearray(STREAM))
    assert [f.type for f in frames] == [0x11, 0x15, 0x12, 0x15, 0x15, 0x15, 0x14]
    assert all(f.flag == 0x00 for f in frames)


def test_iter_frames_reassembles_across_notification_boundaries():
    buffer = bytearray()
    frames = []
    for byte in STREAM:
        buffer.append(byte)
        frames.extend(iter_frames(buffer))
    assert len(frames) == 7
    assert not buffer


def test_iter_frames_withholds_a_partial_frame():
    buffer = bytearray(LIVE_1600[:9])
    assert iter_frames(buffer) == []
    assert len(buffer) == 9


def test_iter_frames_resynchronises_after_garbage():
    frames = iter_frames(bytearray(b"\xde\xad\xbe\xef" + LIVE_1600))
    assert [f.type for f in frames] == [0x15]


def test_iter_frames_drops_a_corrupt_frame_and_recovers():
    corrupt = bytearray(LIVE_1600)
    corrupt[-1] ^= 0xFF
    frames = iter_frames(bytearray(bytes(corrupt) + STABLE_6105))
    assert [f.type for f in frames] == [0x14]


def test_status_frames_are_not_readings():
    frames = iter_frames(bytearray(STATUS_11 + STATUS_12))
    assert [parse_reading(f) for f in frames] == [None, None]


def test_live_frame_decodes_weight_and_resistances():
    (reading,) = _readings(LIVE_1600)
    assert reading == Reading(
        weight_kg=16.00, resistance=855, secondary=1150, final=False
    )


def test_stable_frame_decodes_weight_resistance_and_status():
    (reading,) = _readings(STABLE_6105)
    assert reading == Reading(
        weight_kg=61.05, resistance=854, secondary=None, final=True, status=0x01
    )


def test_weight_ramp_across_the_weigh_in():
    assert [r.weight_kg for r in _readings(STREAM)] == [
        16.00,
        15.10,
        60.95,
        60.90,
        61.05,
    ]


def test_aggregate_prefers_the_stable_frame():
    reading = aggregate_session(_readings(STREAM))
    assert reading is not None
    assert reading.weight_kg == 61.05
    assert reading.resistance == 854
    assert reading.final


def test_aggregate_returns_none_without_readings():
    assert aggregate_session([]) is None


def test_aggregate_uses_the_last_stable_frame_not_the_first():
    """Stable frames converge; the display shows the last one."""
    readings = [
        Reading(60.15, None, None, final=True, status=1),
        Reading(60.10, None, None, final=True, status=1),
        Reading(60.05, None, None, final=True, status=1),
    ]
    reading = aggregate_session(readings)
    assert reading is not None
    assert reading.weight_kg == 60.05


def test_aggregate_ignores_resistance_measured_while_weight_ramped():
    readings = [
        Reading(16.00, 855, 1150, final=False),
        Reading(60.05, None, None, final=True, status=1),
    ]
    reading = aggregate_session(readings)
    assert reading is not None
    assert reading.resistance is None


def test_aggregate_falls_back_to_live_resistance_at_the_settled_weight():
    readings = [
        Reading(16.00, 855, 1150, final=False),
        Reading(60.05, None, None, final=True, status=1),
        Reading(60.05, 612, 300, final=False),
        Reading(60.00, 618, 280, final=False),
        Reading(60.05, 600, 270, final=False),
    ]
    reading = aggregate_session(readings)
    assert reading is not None
    assert reading.weight_kg == 60.05
    assert reading.resistance == 612


def test_aggregate_prefers_stable_resistance_over_live():
    readings = [
        Reading(60.05, 612, 300, final=False),
        Reading(60.05, 845, None, final=True, status=1),
    ]
    reading = aggregate_session(readings)
    assert reading is not None
    assert reading.resistance == 845


def test_aggregate_survives_a_weigh_in_with_no_stable_frame():
    reading = aggregate_session([Reading(59.90, None, None, final=False)])
    assert reading is not None
    assert reading.weight_kg == 59.90


def test_advertisement_mac_echo_matches_the_address():
    assert is_x55aa_frame(ADVERTISEMENT, ADDRESS)


def test_advertisement_mac_echo_rejects_a_different_address():
    assert not is_x55aa_frame(ADVERTISEMENT, "AA:BB:CC:DD:EE:FF")


def test_advertisement_accepts_a_non_mac_address():
    """macOS reports a CoreBluetooth UUID, so the echo cannot be checked."""
    assert is_x55aa_frame(ADVERTISEMENT, "8C4F1B12-3A5E-4C7D-9F01-2B3C4D5E6F70")


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
