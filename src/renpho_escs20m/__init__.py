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
from .qn.body_metrics import BodyMetrics, Sex, calculate_body_fat
from .qn.protocol import Profile, ProfileResolver, build_user_profile_command
from .scale import AdvertisementScale, GattScale, RenphoScale

# Backward-compatible alias: the QN protocol class shipped previously as
# ``RenphoESCS20MScale``. Keep the old name importable.
RenphoESCS20MScale = RenphoQNScale

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
]
