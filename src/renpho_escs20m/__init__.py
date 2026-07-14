"""Renpho BLE scale client."""

from ._version import __version__, __version_info__
from .xaabb import RenphoAABBScale
from .const import (
    BODY_FAT_KEY,
    RESISTANCE_1_KEY,
    RESISTANCE_2_KEY,
    WEIGHT_KEY,
)
from .data import BluetoothScanningMode, ScaleData, WeightUnit
from .qn import RenphoQNScale
from .body_metrics import BodyMetrics, Sex, calculate_body_fat
from .qn.protocol import Profile, ProfileResolver, build_user_profile_command
from .scale import AdvertisementScale, GattScale, RenphoScale
from .detection import (
    KNOWN_QN_SCALE_IDENTIFIERS,
    QN_MANUFACTURER_ID,
    ScaleProtocol,
    detect_protocol,
    is_aabb_frame,
    is_qn_frame,
    parse_qn_model_code,
)

# Backward-compatible alias: the QN protocol class shipped previously as
# ``RenphoESCS20MScale``. Keep the old name importable.
RenphoESCS20MScale = RenphoQNScale

# Protocol -> concrete client class. detection.py stays import-light, so
# this map lives here where the classes are already imported.
SCALE_CLASSES: dict[ScaleProtocol, type[RenphoScale]] = {
    ScaleProtocol.QN: RenphoQNScale,
    ScaleProtocol.AABB: RenphoAABBScale,
}

__all__ = [
    "__version__",
    "__version_info__",
    "RenphoScale",
    "GattScale",
    "AdvertisementScale",
    "RenphoQNScale",
    "RenphoESCS20MScale",
    "RenphoAABBScale",
    "Profile",
    "ProfileResolver",
    "BluetoothScanningMode",
    "ScaleData",
    "WeightUnit",
    "BodyMetrics",
    "Sex",
    "calculate_body_fat",
    "build_user_profile_command",
    "WEIGHT_KEY",
    "BODY_FAT_KEY",
    "RESISTANCE_1_KEY",
    "RESISTANCE_2_KEY",
    "KNOWN_QN_SCALE_IDENTIFIERS",
    "QN_MANUFACTURER_ID",
    "SCALE_CLASSES",
    "ScaleProtocol",
    "detect_protocol",
    "is_aabb_frame",
    "is_qn_frame",
    "parse_qn_model_code",
]
