"""Renpho ES-CS20M BLE client."""

from ._version import __version__, __version_info__
from .body_metrics import BodyMetrics, Sex, calculate_body_fat
from .const import (
    BODY_FAT_KEY,
    RESISTANCE_1_KEY,
    RESISTANCE_2_KEY,
    WEIGHT_KEY,
)
from .scale import (
    BluetoothScanningMode,
    Profile,
    ProfileResolver,
    RenphoESCS20MScale,
    ScaleData,
    WeightUnit,
    build_user_profile_command,
)

__all__ = [
    "__version__",
    "__version_info__",
    "RenphoESCS20MScale",
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
]
