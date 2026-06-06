"""Constants for the Renpho ES-CS20M scale."""

NOTIFY_CHARACTERISTIC_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
COMMAND_CHARACTERISTIC_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"

# Standard BLE SIG characteristics on the scale's Device Information services.
BATTERY_LEVEL_CHARACTERISTIC_UUID = "00002a19-0000-1000-8000-00805f9b34fb"
FIRMWARE_REVISION_CHARACTERISTIC_UUID = "00002a26-0000-1000-8000-00805f9b34fb"

WEIGHT_KEY = "weight"
BODY_FAT_KEY = "body_fat"
RESISTANCE_1_KEY = "resistance_1"
RESISTANCE_2_KEY = "resistance_2"

CMD_SET_DISPLAY_UNIT = bytes.fromhex("1309ff001000000000")
CMD_END_MEASUREMENT = bytes.fromhex("1f05ff1033")

# --- Wire protocol (internal; consumed by scale.py) -----------------------

_EPOCH_OFFSET = 946656000  # scale's epoch: 2000-01-01 00:00:00 UTC

# Frame opcodes (byte 0).
_OP_MEASUREMENT = 0x10
_OP_UNIT_REQUEST = 0x12
_OP_MEAS_INIT_REQUEST = 0x14
_OP_PRE_MEASUREMENT = 0x21
_OP_PROFILE_ACK = 0xA1

# Frame length (byte 1) — selects the flavor on the measurement and
# pre-measurement frames.
_LEN_EXTENDED_MEASUREMENT = 0x0E  # 14-byte frame, body fat on-device
_LEN_BASIC_MEASUREMENT = 0x0B  # 11-byte frame, weight + impedance
_LEN_EXTENDED_PRE_MEASUREMENT = 0x05  # scale wants a user-profile reply
_LEN_BASIC_PRE_MEASUREMENT = 0x04  # no reply needed; scale streams on its own

# Per-device byte at frame offset 2 (renpho's is 0xFF).
_DEFAULT_VENDOR_BYTE = 0xFF

# Extended-flavor measurement status (byte 4).
_MEASUREMENT_STATUS_UNSTABLE = 0
_MEASUREMENT_STATUS_STABLE = 1
_MEASUREMENT_STATUS_STABLE_WITH_METRICS = 2

# Basic-flavor measurement status (byte 5). Two-nibble read: low = weight
# committed, high = BIA running.
_BASIC_STATUS_SETTLING = 0x00  # weight not yet committed, no impedance
_BASIC_STATUS_BIA_RUNNING = 0x11  # weight committed, BIA pass in progress
_BASIC_STATUS_FINAL = 0x01  # BIA done, impedance present

# Guest-mode sentinels for bytes 3-5 of the user-profile frame. The scale
# recognizes the session as ephemeral (no slot allocated, nothing stored),
# so the library coexists safely with the official Renpho app.
_GUEST_USER_ID = 0xFE
_GUEST_PAD_HI = 0xFF
_GUEST_PAD_LO = 0xEE
_USER_PROFILE_TRAILER_TAIL = 0x02

__all__ = [
    "NOTIFY_CHARACTERISTIC_UUID",
    "COMMAND_CHARACTERISTIC_UUID",
    "WEIGHT_KEY",
    "BODY_FAT_KEY",
    "RESISTANCE_1_KEY",
    "RESISTANCE_2_KEY",
    "CMD_SET_DISPLAY_UNIT",
    "CMD_END_MEASUREMENT",
]
