"""Advertisement-based scale protocol detection.

QN frame (company ID 65535)::

    [0:2]  model identifier, 16-bit big-endian (the app's "InternalModel")
    [2:5]  varies per advertisement
    [5:11] device MAC address, little-endian

AABB broadcast frame (company IDs in :data:`AABB_COMPANY_IDS`)::

    [0:2]  0xAABB magic
    [2:8]  device MAC address, forward byte order
    [8:]   protocol payload (see ``xaabb.protocol``)

Both frame families ride the same generic 0xFFFF (65535) company ID — QN
and AABB are disambiguated by the 0xAABB magic prefix, not by company ID.

Company ID 65535 is a catch-all used by many vendors, so for QN frames the
embedded MAC is validated against the device address before the identifier
is trusted; AABB frames validate their (forward-order) MAC echo the same
way. The model identifier is constant across a unit's advertisements;
identifiers MUST be compared as the full 16-bit value.
"""

from __future__ import annotations

import fnmatch
import logging
from enum import StrEnum

from .xaabb.protocol import SUPPORTED_COMPANY_IDS as AABB_COMPANY_IDS

_LOGGER = logging.getLogger(__name__)

QN_MANUFACTURER_ID = 65535

_QN_MODEL_START = 0  # BE16 at bytes 0:2 (the app's InternalModel)
_QN_MAC_SLICE = slice(5, 11)  # little-endian echo
_AABB_MAGIC = b"\xaa\xbb"
_AABB_MAC_SLICE = slice(2, 8)  # FORWARD byte order echo


class ScaleProtocol(StrEnum):
    """Supported scale protocols."""

    QN = "qn"
    AABB = "aabb"


def _mac_bytes(address: str) -> bytes | None:
    """Forward-order bytes of a colon-separated MAC, or None if not a MAC."""
    octets = address.split(":")
    if len(octets) != 6:
        return None  # e.g. macOS CoreBluetooth UUID
    try:
        return bytes(int(o, 16) for o in octets)
    except ValueError:
        return None


def parse_qn_model_code(payload: bytes) -> int | None:
    """Return the model identifier from a QN (65535) payload.

    ``payload`` is the manufacturer-data *value* (company ID stripped).
    The identifier is the first two bytes, big-endian. Returns None if too short.
    """
    if len(payload) < _QN_MODEL_START + 2:
        return None
    return int.from_bytes(payload[_QN_MODEL_START : _QN_MODEL_START + 2], "big")


def is_qn_frame(payload: bytes, address: str | None = None) -> bool:
    """Return True if ``payload`` has the QN frame shape.

    When ``address`` is a real MAC, the little-endian echo at bytes 5-11
    must match it.
    """
    if len(payload) < _QN_MAC_SLICE.stop:
        return False
    if address:
        mac = _mac_bytes(address)
        if mac is not None and payload[_QN_MAC_SLICE] != mac[::-1]:
            return False
    return True


def is_aabb_frame(payload: bytes, address: str | None = None) -> bool:
    """Return True if ``payload`` has the AABB broadcast frame shape.

    Checks the 0xAABB magic and, when ``address`` is a real MAC, the
    forward-order echo at bytes 2-8.
    """
    if len(payload) < _AABB_MAC_SLICE.stop or payload[:2] != _AABB_MAGIC:
        return False
    if address:
        mac = _mac_bytes(address)
        if mac is not None and payload[_AABB_MAC_SLICE] != mac:
            return False
    return True


# Model identifiers (payload bytes 0:2 big-endian)
# Unknown variants are covered by FALLBACK_MATCHERS and reported via the log below.
# Add new identifiers as units are reported.
KNOWN_QN_SCALE_IDENTIFIERS: frozenset[int] = frozenset(
    {
        0x095B,  # "Renpho-Scale", FF:04:00 OUI
        0x099B,  # "QN-Scale", FF:04:00 OUI
        0x09E9,  # "QN-Scale", FF:03:00 OUI
        0x0216,  # "QN-Scale", D8:0B:CB OUI
    }
)

# (company_id, identifier) pairs already reported via the fallback-path log.
_reported_identifiers: set[tuple[int, int]] = set()

# Fallback matchers, checked when no frame evidence classifies the device.
# Patterns are matched case-insensitively against the advertised local name
# and, if given, the device address.
FALLBACK_MATCHERS: list[tuple[ScaleProtocol, str]] = [
    (ScaleProtocol.QN, "Renpho-Scale*"),
    (ScaleProtocol.QN, "QN-Scale*"),
    (ScaleProtocol.QN, "FF:03:00:*"),
    (ScaleProtocol.QN, "FF:04:00:*"),
]


def detect_protocol(
    local_name: str | None,
    manufacturer_data: dict[int, bytes] | None,
    address: str | None = None,
) -> ScaleProtocol | None:
    """Classify an advertisement; return None if it is not a known scale.

    Frame evidence is authoritative: a valid AABB frame or a QN frame with
    a known identifier classifies without any name. Name/address matchers
    are a fallback.
    """
    manufacturer_data = manufacturer_data or {}

    for company in AABB_COMPANY_IDS:
        payload = manufacturer_data.get(company)
        if payload is not None and is_aabb_frame(payload, address):
            return ScaleProtocol.AABB

    qn_code = None
    payload = manufacturer_data.get(QN_MANUFACTURER_ID)
    if payload is not None and is_qn_frame(payload, address):
        qn_code = parse_qn_model_code(payload)
        if qn_code is not None and qn_code in KNOWN_QN_SCALE_IDENTIFIERS:
            return ScaleProtocol.QN

    for protocol, pattern in FALLBACK_MATCHERS:
        for candidate in (local_name, address):
            if candidate and fnmatch.fnmatch(candidate.lower(), pattern.lower()):
                if (
                    qn_code is not None
                    and (QN_MANUFACTURER_ID, qn_code) not in _reported_identifiers
                ):
                    # A scale whose identifier isn't in the registry yet —
                    # every such report lets us extend KNOWN_QN_SCALE_IDENTIFIERS.
                    _reported_identifiers.add((QN_MANUFACTURER_ID, qn_code))
                    _LOGGER.warning(
                        "Detected likely %s scale via fallback matcher %r with "
                        "unrecognized model identifier %d — please report this "
                        "identifier so it can be added to the registry.",
                        protocol.value,
                        pattern,
                        qn_code,
                    )
                return protocol
    return None
