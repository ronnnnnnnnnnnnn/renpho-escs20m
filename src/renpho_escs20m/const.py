"""Constants for the Renpho ES-CS20M scale."""

# QN-Scale hardware ships two GATT transports for the same wire protocol.
# The renpho ES-CS20M uses the FFF0 service (preferred when present):
NOTIFY_CHARACTERISTIC_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
COMMAND_CHARACTERISTIC_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"

# Other QN scales (e.g. Arboleaf CS20M) expose the FFE0 service
# instead. Pre-measurement and stored-record frames arrive as indications
# on FFE2; set-time and stored-query commands go to FFE4, the rest to FFE3.
FFE0_NOTIFY_CHARACTERISTIC_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
FFE0_INDICATE_CHARACTERISTIC_UUID = "0000ffe2-0000-1000-8000-00805f9b34fb"
FFE0_COMMAND_CHARACTERISTIC_UUID = "0000ffe3-0000-1000-8000-00805f9b34fb"
FFE0_ALT_COMMAND_CHARACTERISTIC_UUID = "0000ffe4-0000-1000-8000-00805f9b34fb"

# A third, older QN transport seen on the Renpho R-A012/R-A020 "Body Fat
# Scale" line (not the ES-CS20M/Elis line the two transports above target):
# a single custom service whose 16-bit UUID alias (0x1a10) matches the
# company ID in this scale's BLE advertisement manufacturer data. It speaks
# a distinct, undocumented wire format with no opcode/length framing in
# common with FFF0/FFE0 -- see qn/protocol.py:parse_legacy_measurement.
# Hardware-verified on one R-A012 unit. The service also has a write
# characteristic (0x2a11) -- present in the GATT table but its command
# format is unknown; an attempt to capture the official Renpho app's BLE
# traffic to it (Android Bluetooth HCI snoop log) came up empty, most
# likely because the phone's snoop ring buffer is too small to retain a
# short-lived connection amid unrelated background BLE traffic. The scale
# streams weight on its own without anything being written to it, so
# LEGACY_COMMAND_CHARACTERISTIC_UUID isn't defined here -- add it if/when
# someone works out what it's for (it may unlock on-device body-fat
# calculation the way the profile exchange does on the other transports).
LEGACY_SERVICE_UUID = "00001a10-0000-1000-8000-00805f9b34fb"
LEGACY_NOTIFY_CHARACTERISTIC_UUID = "00002a10-0000-1000-8000-00805f9b34fb"

# Standard BLE SIG characteristics on the scale's Device Information services.
BATTERY_LEVEL_CHARACTERISTIC_UUID = "00002a19-0000-1000-8000-00805f9b34fb"
FIRMWARE_REVISION_CHARACTERISTIC_UUID = "00002a26-0000-1000-8000-00805f9b34fb"

WEIGHT_KEY = "weight"
BODY_FAT_KEY = "body_fat"
RESISTANCE_1_KEY = "resistance_1"
RESISTANCE_2_KEY = "resistance_2"

# Body-composition metrics some scales compute on-device and stream after
# the final measurement. Only models that send the extended-metrics frames
# report these; everywhere else the caller derives its own from body fat
# (see :mod:`renpho_escs20m.body_metrics`). Masses are kg, percentages are
# percent, BMR is kcal/day, and visceral fat, body age, body score and body
# shape are the scale's own unitless ratings.
BMI_KEY = "bmi"
BODY_WATER_KEY = "body_water"
MUSCLE_MASS_KEY = "muscle_mass"
VISCERAL_FAT_KEY = "visceral_fat"
BODY_AGE_KEY = "body_age"
BMR_KEY = "bmr"
PROTEIN_KEY = "protein"
BONE_MASS_KEY = "bone_mass"
FAT_FREE_MASS_KEY = "fat_free_mass"
SUBCUTANEOUS_FAT_KEY = "subcutaneous_fat"
SKELETAL_MUSCLE_KEY = "skeletal_muscle"
BODY_SCORE_KEY = "body_score"
BODY_SHAPE_KEY = "body_shape"

__all__ = [
    "NOTIFY_CHARACTERISTIC_UUID",
    "COMMAND_CHARACTERISTIC_UUID",
    "FFE0_NOTIFY_CHARACTERISTIC_UUID",
    "FFE0_INDICATE_CHARACTERISTIC_UUID",
    "FFE0_COMMAND_CHARACTERISTIC_UUID",
    "FFE0_ALT_COMMAND_CHARACTERISTIC_UUID",
    "LEGACY_SERVICE_UUID",
    "LEGACY_NOTIFY_CHARACTERISTIC_UUID",
    "BATTERY_LEVEL_CHARACTERISTIC_UUID",
    "FIRMWARE_REVISION_CHARACTERISTIC_UUID",
    "WEIGHT_KEY",
    "BODY_FAT_KEY",
    "RESISTANCE_1_KEY",
    "RESISTANCE_2_KEY",
    "BMI_KEY",
    "BODY_WATER_KEY",
    "MUSCLE_MASS_KEY",
    "VISCERAL_FAT_KEY",
    "BODY_AGE_KEY",
    "BMR_KEY",
    "PROTEIN_KEY",
    "BONE_MASS_KEY",
    "FAT_FREE_MASS_KEY",
    "SUBCUTANEOUS_FAT_KEY",
    "SKELETAL_MUSCLE_KEY",
    "BODY_SCORE_KEY",
    "BODY_SHAPE_KEY",
]
