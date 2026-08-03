"""Tests for advertisement-based protocol detection."""

import logging

from renpho_escs20m import detection as detection_module
from renpho_escs20m.detection import (
    QN_MANUFACTURER_ID,
    ScaleProtocol,
    detect_protocol,
    has_obfuscated_resistance,
    is_aabb_frame,
    is_qn_frame,
    parse_qn_model_code,
)

QN = QN_MANUFACTURER_ID

QN_095B_A = bytes.fromhex("095b01000001aa130004ff6008030001")
QN_095B_B = bytes.fromhex("095b01000001aa130004ff6008040001")
QN_09E9 = bytes.fromhex("09e900000003aa670003ff")
QN_0C77 = bytes.fromhex("0c7701001427fb0a0005ff6108020017")
RMSB01_ADDR = "FF:05:00:0A:FB:27"
AABB_A = bytes.fromhex("aabbed673b1aaa0608a1056a40b64b64225c1c4c4403951b")
AABB_B = bytes.fromhex("aabbed673b1aaa062da7056affffff0420391c4c4403941b")
AABB_ADDR = "ED:67:3B:1A:AA:06"
FOREIGN_QN = bytes.fromhex("012601000607aa0b44ac04")
JUNK_65535 = bytes.fromhex("1601ff806e64645f99c40bbb00431b4691dabb0043d3cf92")
SHORT_65535 = bytes.fromhex("f3a407e0aa09")


def test_protocol_values_are_stable():
    assert ScaleProtocol.QN.value == "qn"
    assert ScaleProtocol.AABB.value == "aabb"


def test_parse_qn_model_code():
    assert parse_qn_model_code(QN_095B_A) == 0x095B
    assert parse_qn_model_code(QN_09E9) == 0x09E9
    assert parse_qn_model_code(FOREIGN_QN) == 0x0126
    assert parse_qn_model_code(b"\x09") is None  # too short


def test_is_qn_frame_mac_echo():
    assert is_qn_frame(QN_095B_A, "FF:04:00:13:AA:01")
    assert not is_qn_frame(QN_095B_A, "AA:BB:CC:DD:EE:FF")
    assert not is_qn_frame(JUNK_65535, "CF:FC:CA:1C:AA:08")
    # CoreBluetooth UUID address: echo check skipped, structure still gates
    assert is_qn_frame(QN_095B_A, "12345678-1234-1234-1234-123456789abc")


def test_is_aabb_frame_forward_mac_echo():
    assert is_aabb_frame(AABB_A, AABB_ADDR)
    assert is_aabb_frame(AABB_B, AABB_ADDR)
    assert not is_aabb_frame(AABB_A, "AA:BB:CC:DD:EE:FF")
    assert not is_aabb_frame(QN_095B_A, "FF:04:00:13:AA:01")


def test_detect_known_qn_identifiers():
    assert (
        detect_protocol("QN-Scale", {QN: QN_09E9}, "FF:03:00:67:AA:03")
        == ScaleProtocol.QN
    )
    # Identifier alone suffices — no name needed (passive scans).
    assert (
        detect_protocol(None, {QN: QN_095B_A}, "FF:04:00:13:AA:01") == ScaleProtocol.QN
    )


def test_detect_aabb_by_frame():
    aabb_company = next(iter(detection_module.AABB_COMPANY_IDS))
    assert (
        detect_protocol(None, {aabb_company: AABB_A}, AABB_ADDR) == ScaleProtocol.AABB
    )


def test_foreign_qn_devices_rejected_without_name_match():
    # Unknown identifier + no matching name: not classified.
    assert detect_protocol(None, {QN: FOREIGN_QN}, "04:AC:44:0B:AA:07") is None
    assert detect_protocol(None, {QN: JUNK_65535}, "CF:FC:CA:1C:AA:08") is None
    assert detect_protocol("SomeHeadphones", {76: b"\x00" * 8}) is None


def test_name_fallback_classifies_qn():
    # Shipped behavior preserved: matching names classify as QN even
    # without a recognized identifier.
    assert detect_protocol("Renpho-Scale", {}) == ScaleProtocol.QN
    assert detect_protocol("QN-Scale1", {}) == ScaleProtocol.QN
    # Address-as-name fallback
    assert detect_protocol("FF:03:00:12:34:56", {}) == ScaleProtocol.QN
    assert detect_protocol(None, {}, address="FF:04:00:12:34:56") == ScaleProtocol.QN


def test_rmsb01_oui_fallback_classifies_qn():
    # Renpho R-MSB01's confirmed OUI. Address-only match, no name and no
    # manufacturer data — the state a passive scanner sees, since the model
    # identifier travels in the scan response (see below).
    assert detect_protocol(None, {}, address="FF:05:00:0A:FB:27") == ScaleProtocol.QN


def test_rmsb01_scan_response_classifies_by_identifier():
    """Real R-MSB01 scan-response payload: identifier 0x0C77 plus the
    little-endian MAC echo that validates it against the device address."""
    assert parse_qn_model_code(QN_0C77) == 0x0C77
    assert is_qn_frame(QN_0C77, RMSB01_ADDR)
    assert detect_protocol(None, {QN: QN_0C77}, address=RMSB01_ADDR) == ScaleProtocol.QN


def test_rmsb01_resistance_flagged_as_obfuscated():
    # By address prefix — what a passive scanner leaves us with.
    assert has_obfuscated_resistance("FF:05:00:0A:FB:27")
    # By model identifier, when an active scan makes it available.
    assert has_obfuscated_resistance(None, model_code=0x0C77)


def test_other_models_report_raw_resistance():
    assert not has_obfuscated_resistance("FF:04:00:12:34:56")
    assert not has_obfuscated_resistance(None, model_code=0x095B)
    assert not has_obfuscated_resistance(None)


def test_unrecognized_identifier_logged_once(caplog):
    detection_module._reported_identifiers.clear()
    with caplog.at_level(logging.INFO, logger="renpho_escs20m.detection"):
        # Foreign QN frame + matching name: classified by fallback, and the
        # unknown identifier is reported (once) for registry growth.
        assert (
            detect_protocol("QN-Scale1", {QN: FOREIGN_QN}, "04:AC:44:0B:AA:07")
            == ScaleProtocol.QN
        )
        detect_protocol("QN-Scale1", {QN: FOREIGN_QN}, "04:AC:44:0B:AA:07")
    assert caplog.text.count("unrecognized model identifier 294") == 1  # 0x0126


def test_public_api_exports():
    import renpho_escs20m as lib

    assert lib.ScaleProtocol is ScaleProtocol
    assert lib.detect_protocol is detect_protocol
    assert set(lib.SCALE_CLASSES) == set(ScaleProtocol)
    assert lib.SCALE_CLASSES[ScaleProtocol.QN] is lib.RenphoQNScale
    assert lib.SCALE_CLASSES[ScaleProtocol.AABB] is lib.RenphoAABBScale
    for name in lib.__all__:
        assert hasattr(lib, name), f"__all__ exports missing attribute: {name}"
