"""Renpho ES-CS20M BLE scale implementation."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import platform
import struct
import time
from collections.abc import Awaitable, Callable
from enum import IntEnum, StrEnum
from typing import Any

from bleak import BleakClient
from bleak.assigned_numbers import AdvertisementDataType
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import (
    AdvertisementData,
    BaseBleakScanner,
    get_platform_scanner_backend_type,
)
from bleak_retry_connector import establish_connection

from .body_metrics import Sex
from .const import (
    BATTERY_LEVEL_CHARACTERISTIC_UUID,
    BODY_FAT_KEY,
    CMD_END_MEASUREMENT,
    CMD_SET_DISPLAY_UNIT,
    COMMAND_CHARACTERISTIC_UUID,
    FIRMWARE_REVISION_CHARACTERISTIC_UUID,
    NOTIFY_CHARACTERISTIC_UUID,
    RESISTANCE_1_KEY,
    RESISTANCE_2_KEY,
    WEIGHT_KEY,
)

_LOGGER = logging.getLogger(__name__)

_SYSTEM = platform.system()
_IS_LINUX = _SYSTEM == "Linux"
_IS_MACOS = _SYSTEM == "Darwin"

if _IS_LINUX:
    from bleak.args.bluez import BlueZScannerArgs, OrPattern

    _PASSIVE_OR_PATTERNS = [
        OrPattern(0, AdvertisementDataType.FLAGS, b"\x02"),
        OrPattern(0, AdvertisementDataType.FLAGS, b"\x06"),
        OrPattern(0, AdvertisementDataType.FLAGS, b"\x1a"),
    ]
    _PASSIVE_SCANNER_ARGS = BlueZScannerArgs(or_patterns=_PASSIVE_OR_PATTERNS)


_EPOCH_OFFSET = 946656000

_DEVICE_METADATA_READ_TIMEOUT_SECONDS = 1.0

_MEASUREMENT_STATUS_UNSTABLE = 0
_MEASUREMENT_STATUS_STABLE = 1
_MEASUREMENT_STATUS_STABLE_WITH_METRICS = 2

_STATE_UNIT_SET = 1
_STATE_MEASUREMENT_INIT = 2
_STATE_USER_PROFILE = 4
_STATE_PROFILE_RESOLVING = 8

_DEFAULT_ALGORITHM = 0x04

# Guest-mode sentinels for bytes 3-5 of the user-profile frame. The
# scale firmware uses these to recognize the session as ephemeral —
# no slot is allocated, nothing is stored, and this library coexists
# safely on the same scale as the official Renpho app.
_GUEST_USER_ID = 0xFE
_GUEST_PAD_HI = 0xFF
_GUEST_PAD_LO = 0xEE

_USER_PROFILE_TRAILER_TAIL = 0x02


class BluetoothScanningMode(StrEnum):
    PASSIVE = "passive"
    ACTIVE = "active"


class WeightUnit(IntEnum):
    """
    Display weight unit shown on the scale.

    Values are library-level identifiers, not the raw bytes exchanged over
    BLE. The scale's command/response protocol encodes units as a single
    byte with the following mapping:

    ====== ================================================================
     byte   meaning
    ====== ================================================================
    ``1``   kilograms
    ``2``   pounds
    ``8``   stones + pounds (weight shown as e.g. ``12 st 4.6 lb``)
    ``16``  stones only (weight shown as e.g. ``12.6 st``)
    ====== ================================================================

    :class:`WeightUnit` values map onto those bytes via
    :func:`build_unit_update_command`.
    """

    KG = 0
    LB = 1
    ST = 2
    ST_LB = 3


@dataclasses.dataclass(frozen=True)
class Profile:
    """
    User-profile inputs the scale needs to compute body fat on-device.

    Pass an instance to :class:`RenphoESCS20MScale` to drive the scale's
    body composition measurement in fixed-user mode, or return one from a :data:`ProfileResolver`
    for user-detection mode.

    Attributes:
        sex: :class:`Sex` enum value.
        age: Integer years. The Renpho app uses birthday-aware age
            (the user's UI age ``N`` if their birthday has occurred this
            calendar year, else ``N-1``); callers who want to match
            that should compute it themselves before constructing the
            profile.
        height_m: Height in metres. The library does **not** truncate
            to whole cm the way the Renpho app does — feeding the
            scale the user's exact height yields slightly
            more precise body fat output. To reproduce the app's
            displayed values exactly, pre-truncate the call site:
            ``height_m = int(actual_cm) / 100``.
        athlete: Switches the scale to its athlete-tuned body fat calculation curve.
        algorithm: Selects the on-device body fat calculation algorithm.
    """

    sex: Sex
    age: int
    height_m: float
    athlete: bool = False
    algorithm: int = _DEFAULT_ALGORITHM


# Async callback the library invokes once per session, in user-detection
# mode, with the first stable weight reading. Should return the
# :class:`Profile` for whichever user is on the scale, or ``None`` to
# leave the scale running on the bootstrap profile (no body fat will be
# produced — only weight will stream).
ProfileResolver = Callable[[float], Awaitable[Profile | None]]


# Profile sent when no real profile is available yet — i.e. weight-only
# mode (no profile ever) and the initial reply in user-detection mode
# (overridden once the resolver completes). The scale will not
# start a measurement at all without a profile reply, so we always send
# one. ``algorithm=0x00`` tells the scale "no body fat calculation".
_BOOTSTRAP_PROFILE = Profile(
    sex=Sex.Male,
    age=0,
    height_m=0,
    athlete=False,
    algorithm=0x00,
)


@dataclasses.dataclass
class ScaleData:
    """
    Parsed scale measurement payload.

    Attributes:
        name: Advertised name of the scale.
        address: Bluetooth address of the scale.
        display_unit: Unit to be used for the scale display.
        measurements: Parsed measurement values. Supported keys are
            ``weight`` (kg) and ``body_fat`` (%) when reported.
    """

    name: str = ""
    address: str = ""
    display_unit: WeightUnit = WeightUnit.KG
    measurements: dict[str, str | float | None] = dataclasses.field(
        default_factory=dict
    )


def _coerce_user_profile_height_mm(value: int) -> int:
    height_mm = int(value)
    if height_mm < 0 or height_mm > 0xFFFF:
        raise ValueError("profile height must be in the range 0..65535")
    return height_mm


def _height_m_to_mm(height_m: float) -> int:
    """Convert ``height_m`` (metres) to the integer-mm value the BLE
    profile frame requires.

    Rounds to the nearest mm. The library does **not** truncate to
    whole cm the way the Renpho app does — the scale firmware accepts
    any uint16 mm value, and feeding it the user's exact height gives
    more precise body fat output. Callers who want to match the app's
    displayed values exactly can pre-truncate themselves:
    ``height_m = int(actual_cm) / 100``.
    """
    return _coerce_user_profile_height_mm(int(round(float(height_m) * 1000)))


_WEIGHT_UNIT_TO_BYTE: dict[WeightUnit, int] = {
    WeightUnit.KG: 0x01,
    WeightUnit.LB: 0x02,
    WeightUnit.ST_LB: 0x08,
    WeightUnit.ST: 0x10,
}


def build_unit_update_command(desired_unit: WeightUnit) -> bytearray:
    """
    Build the display-unit update command.

    Maps :class:`WeightUnit` onto the scale's raw unit byte:

    ============== =====
     ``WeightUnit``  byte
    ============== =====
    ``KG``          ``0x01``
    ``LB``          ``0x02``
    ``ST_LB``       ``0x08``  (stones + pounds remainder)
    ``ST``          ``0x10``  (stones only)
    ============== =====
    """
    unit_byte = _WEIGHT_UNIT_TO_BYTE.get(WeightUnit(desired_unit), 0x01)
    payload = bytearray(CMD_SET_DISPLAY_UNIT)
    payload[3] = unit_byte
    payload[8] = sum(payload[0:8]) & 0xFF
    return payload


def build_measurement_initiation_command() -> bytearray:
    """Build the initiation command with current timestamp and checksum."""
    cmd = bytearray(8)
    cmd[0:3] = b"\x20\x08\xff"
    ts = int(time.time()) - _EPOCH_OFFSET
    struct.pack_into("<I", cmd, 3, ts)
    cmd[7] = sum(cmd[0:7]) & 0xFF
    return cmd


def build_user_profile_command(
    sex: int,
    age: int,
    height_m: float,
    athlete: bool = False,
    algorithm: int = _DEFAULT_ALGORITHM,
) -> bytearray:
    """
    Build the 13-byte guest-mode user-profile command the scale expects
    in response to its profile request.

    The flag byte at offset 10 encodes both the body fat calculation algorithm selector
    and the athlete bit:

        flag = (algorithm + (0x0A if athlete else 0)) & 0xFF

    See :class:`Profile` for the meaning of the algorithm values and
    the athlete bit. ``algorithm=0x00`` disables body fat calculation.

    Height is given in metres and is rounded to the nearest mm before
    sending. The library does **not** truncate to whole cm the way the
    Renpho app does — the scale accepts any uint16 mm value.
    """
    height_mm = _height_m_to_mm(height_m)
    flag_byte = (int(algorithm) + (0x0A if athlete else 0)) & 0xFF
    payload = bytearray(
        [
            0xA0,
            0x0D,
            0x02,
            _GUEST_USER_ID,
            _GUEST_PAD_HI,
            _GUEST_PAD_LO,
            sex & 0xFF,
            age & 0xFF,
            (height_mm >> 8) & 0xFF,
            height_mm & 0xFF,
            flag_byte,
            _USER_PROFILE_TRAILER_TAIL,
        ]
    )
    payload.append(sum(payload) & 0xFF)
    return payload


def _build_command_for_profile(profile: Profile) -> bytearray:
    return build_user_profile_command(
        sex=int(profile.sex),
        age=profile.age,
        height_m=profile.height_m,
        athlete=profile.athlete,
        algorithm=profile.algorithm,
    )


def parse_weight(payload: bytearray) -> dict[str, int | float | None]:
    """
    Parse a live measurement notification.

    Returns a dict with ``weight`` (kg), and on stable-with-metrics
    frames also ``body_fat`` (%) and the two impedance readings
    (``resistance_1``, ``resistance_2``, in ohms). Resistance can be fed
    to :func:`renpho_escs20m.body_metrics.calculate_body_fat` to compute
    body fat retroactively when the user identity is known later than
    the measurement (e.g. after a slow user-detection lookup).
    """
    data: dict[str, int | float | None] = {}
    status = payload[4]
    weight = int.from_bytes(payload[5:7], "big")
    data[WEIGHT_KEY] = round(float(weight) / 100, 2)

    if status == _MEASUREMENT_STATUS_STABLE_WITH_METRICS and len(payload) >= 13:
        body_fat = int.from_bytes(payload[11:13], "big")
        if body_fat:
            data[BODY_FAT_KEY] = round(float(body_fat) / 10, 1)
        r1 = int.from_bytes(payload[7:9], "big")
        r2 = int.from_bytes(payload[9:11], "big")
        if r1 or r2:
            data[RESISTANCE_1_KEY] = r1
            data[RESISTANCE_2_KEY] = r2
    return data


async def _read_device_metadata(
    client: BleakClient,
) -> tuple[int | None, str | None]:
    """Read battery level and firmware revision from the scale.

    Each read is independent and best-effort: any failure (characteristic
    absent, slow response, BLE error, decode error, or empty payload returns
    ``None`` for that value without raising. Returns
    ``(battery_level, firmware_revision)``.

    The two reads run concurrently to minimize connect-path latency. The helper
    applies a short timeout per field so unsupported or slow characteristics do
    not block startup.
    """
    async def read_battery() -> int | None:
        try:
            char = client.services.get_characteristic(
                BATTERY_LEVEL_CHARACTERISTIC_UUID
            )
            if char is not None:
                data = await asyncio.wait_for(
                    client.read_gatt_char(char),
                    timeout=_DEVICE_METADATA_READ_TIMEOUT_SECONDS,
                )
                if not data:
                    return None
                return data[0]
        except Exception:
            _LOGGER.debug(
                "ES-CS20M failed to read battery level", exc_info=True
            )
        return None

    async def read_firmware() -> str | None:
        try:
            char = client.services.get_characteristic(
                FIRMWARE_REVISION_CHARACTERISTIC_UUID
            )
            if char is not None:
                data = await asyncio.wait_for(
                    client.read_gatt_char(char),
                    timeout=_DEVICE_METADATA_READ_TIMEOUT_SECONDS,
                )
                return data.decode("utf-8").strip(" \t\n\r\x00") or None
        except Exception:
            _LOGGER.debug(
                "ES-CS20M failed to read firmware revision", exc_info=True
            )
        return None

    battery, firmware = await asyncio.gather(read_battery(), read_firmware())
    return battery, firmware


class RenphoESCS20MScale:
    """
    Renpho ES-CS20M BLE scale.

    Manages the BLE connection lifecycle and handles the handshake/measurement
    flow for the ES-CS20M (QN-series) protocol.

    The scale is always driven in *guest mode*: it does not allocate a
    persistent slot or store the reading. This simplifies the protocol handshake,
    and prevents this library from clobbering or evicting any user the
    official Renpho app may have registered on the same scale.

    The scale will not start a measurement without a profile
    reply, so the library always sends one. The ``profile`` argument
    selects one of three operating modes:

    - :class:`Profile` instance — *fixed-user mode*: the profile is sent
      immediately, exactly as the Renpho app does for a known user.
    - :data:`ProfileResolver` callable — *user-detection mode*: a
      bootstrap profile (``algorithm=0x00``, no body fat calculation) is sent immediately
      so the measurement starts; on the first stable weight frame the
      library calls the resolver with the weight, then writes the
      returned profile, overriding the bootstrap so the scale runs body fat calculation
      against the resolved profile. The resolver needs to return faster than
      the scale's internal body fat calculation commit window (~2s after the first
      stable frame).
    - ``None`` (default) — *weight-only mode*: the bootstrap profile (algorithm=0x00, no body fat calculation) is
      sent and never overridden; the scale streams weight only.
    """

    def __init__(
        self,
        address: str,
        notification_callback: Callable[[ScaleData], None],
        display_unit: WeightUnit = WeightUnit.KG,
        *,
        profile: Profile | ProfileResolver | None = None,
        scanning_mode: BluetoothScanningMode = BluetoothScanningMode.ACTIVE,
        adapter: str | None = None,
        bleak_scanner_backend: BaseBleakScanner | None = None,
        cooldown_seconds: int = 0,
        logger: logging.Logger | None = None,
    ) -> None:
        self._logger = logger or _LOGGER
        self._logger.info("Initializing RenphoESCS20MScale for address: %s", address)

        self.address = address
        self._client: BleakClient | None = None
        self._initializing: bool = False
        self._notification_callback = notification_callback
        self._cooldown_seconds = cooldown_seconds
        self._cooldown_end_time: float = 0
        self._display_unit: WeightUnit = WeightUnit(display_unit)
        self._state_mask = 0
        self._battery_level: int | None = None
        self._firmware_revision: str | None = None

        if profile is None:
            self._fixed_profile: Profile | None = None
            self._profile_resolver: ProfileResolver | None = None
        elif isinstance(profile, Profile):
            self._fixed_profile = profile
            self._profile_resolver = None
        elif callable(profile):
            self._fixed_profile = None
            self._profile_resolver = profile
        else:
            raise TypeError(
                "profile must be a Profile, an async ProfileResolver, or None; "
                f"got {type(profile).__name__}"
            )

        # In detection mode this holds the in-flight resolver task so we
        # can cancel it if the BLE session ends before the resolver
        # returns. ``None`` outside of detection mode and between
        # sessions.
        self._resolver_task: asyncio.Task | None = None

        if bleak_scanner_backend is None:
            scanner_kwargs: dict[str, Any] = {
                "detection_callback": self._advertisement_callback,
                "service_uuids": None,
                "scanning_mode": BluetoothScanningMode.ACTIVE,
                "bluez": {},
                "cb": {},
            }
            if _IS_LINUX:
                if adapter:
                    scanner_kwargs["adapter"] = adapter
                if scanning_mode == BluetoothScanningMode.PASSIVE:
                    scanner_kwargs["bluez"] = _PASSIVE_SCANNER_ARGS
                    scanner_kwargs["scanning_mode"] = BluetoothScanningMode.PASSIVE
            elif _IS_MACOS:
                scanner_kwargs["cb"] = {"use_bdaddr": True}
            PlatformBleakScanner, _ = get_platform_scanner_backend_type()
            self._scanner = PlatformBleakScanner(**scanner_kwargs)
        else:
            self._scanner = bleak_scanner_backend
            self._scanner.register_detection_callback(self._advertisement_callback)
        self._lock = asyncio.Lock()

    @property
    def battery_level(self) -> int | None:
        """Last successfully-read battery level.

        Normally 0-100 (percent), per the BLE SIG definition. Out-of-range
        values (e.g. 255) are passed through unmodified rather than clamped
        or rejected, so a misbehaving firmware can surface here for the
        consumer to handle — don't assume the value is always within range.

        ``None`` until the first successful read on the first connection.
        Persists across disconnects: a transient read failure does not
        clobber a previously-cached value.
        """
        return self._battery_level

    @property
    def firmware_revision(self) -> str | None:
        """Last successfully-read firmware revision string, stripped of
        whitespace and null bytes.

        ``None`` until the first successful read on the first connection,
        or if the device's firmware-revision characteristic returned an
        empty/whitespace-only payload. Persists across disconnects: a
        transient read failure does not clobber a previously-cached value.
        """
        return self._firmware_revision

    @property
    def display_unit(self) -> WeightUnit:
        return self._display_unit

    @display_unit.setter
    def display_unit(self, value: WeightUnit) -> None:
        if value is None:
            raise ValueError("display_unit cannot be None")
        self._display_unit = WeightUnit(value)

    async def _populate_device_metadata(self, client: BleakClient) -> None:
        """Read device metadata via :func:`_read_device_metadata` and update
        the cached attributes.

        Conditional assignment: if a read returns ``None`` (transient
        failure or characteristic absent), the prior cached value is
        preserved rather than clobbered.
        """
        battery, firmware = await _read_device_metadata(client)
        if battery is not None:
            self._battery_level = battery
        if firmware is not None:
            self._firmware_revision = firmware

    async def async_start(self) -> None:
        """Start BLE scanning and begin listening for the target scale."""
        self._logger.debug("Starting RenphoESCS20MScale scanner for %s", self.address)
        try:
            async with self._lock:
                await self._scanner.start()
        except Exception as ex:
            self._logger.error("Failed to start scanner: %s", ex)
            raise

    async def async_stop(self) -> None:
        """Stop BLE scanning."""
        self._logger.debug("Stopping RenphoESCS20MScale scanner for %s", self.address)
        try:
            async with self._lock:
                await self._scanner.stop()
        except Exception as ex:
            self._logger.error("Failed to stop scanner: %s", ex)
            raise

    def _unavailable_callback(self, _: BleakClient) -> None:
        self._logger.debug("Scale disconnected")
        self._cooldown_end_time = time.time() + self._cooldown_seconds
        self._client = None
        if self._resolver_task is not None and not self._resolver_task.done():
            self._resolver_task.cancel()
        self._resolver_task = None

    async def _advertisement_callback(
        self, ble_device: BLEDevice, _: AdvertisementData
    ) -> None:
        if ble_device.address != self.address:
            return

        if self._cooldown_seconds > 0 and time.time() < self._cooldown_end_time:
            self._logger.debug(
                "Ignoring advertisement during cooldown (ends at %s)",
                self._cooldown_end_time,
            )
            return

        async with self._lock:
            if self._client is not None or self._initializing:
                return
            self._initializing = True

        try:
            try:
                self._logger.debug("Connecting to scale: %s", self.address)
                self._client = await establish_connection(
                    BleakClient,
                    ble_device,
                    self.address,
                    self._unavailable_callback,
                )
                self._logger.debug("Connected to scale: %s", self.address)
            except Exception as ex:
                self._logger.exception(
                    "Could not connect to scale: %s(%s)", type(ex), ex.args
                )
                self._client = None
                return

            if not self._client or not self._client.is_connected:
                self._logger.error("Client not connected, skipping setup")
                return

            await self._start_scale_session(ble_device)
        finally:
            self._initializing = False

    async def _start_scale_session(self, ble_device: BLEDevice) -> None:
        client = self._client
        if client is None:
            return
        self._state_mask = 0
        try:
            self._logger.debug(
                "ES-CS20M starting session for device %s (%s)",
                ble_device.name,
                ble_device.address,
            )
            await self._populate_device_metadata(client)
            if weight_char := client.services.get_characteristic(
                NOTIFY_CHARACTERISTIC_UUID
            ):
                await client.start_notify(
                    weight_char,
                    lambda char, data: self._notification_handler(
                        char, data, ble_device.name, ble_device.address
                    ),
                )
            else:
                self._logger.error("ES-CS20M notification characteristic not found")
                return
        except Exception as ex:
            self._logger.exception("%s(%s)", type(ex), ex.args)
            self._client = None

    def _notification_handler(
        self, _: BleakGATTCharacteristic, payload: bytearray, name: str, address: str
    ) -> None:
        self._logger.debug("ES-CS20M RX payload: %s", payload.hex())

        if len(payload) >= 7 and payload[0:3] == b"\x10\x0e\xff":
            self._handle_measurement(payload, name, address)
            return

        prefix = bytes(payload[0:3])

        if prefix == b"\x12\x12\xff":
            if not self._state_mask & _STATE_UNIT_SET:
                self._state_mask |= _STATE_UNIT_SET
                self._logger.debug(
                    "ES-CS20M unit negotiation frame received from %s. Scheduling update.",
                    address,
                )
                cmd = build_unit_update_command(self.display_unit)
                asyncio.create_task(self._safe_write(cmd), name="escs20m-unit-update")
        elif prefix == b"\x14\x0b\xff":
            if not self._state_mask & _STATE_MEASUREMENT_INIT:
                self._state_mask |= _STATE_MEASUREMENT_INIT
                self._logger.debug(
                    "ES-CS20M measurement initiation requested by %s. Sending timestamp.",
                    address,
                )
                cmd = build_measurement_initiation_command()
                asyncio.create_task(
                    self._safe_write(cmd), name="escs20m-measurement-init"
                )
        elif prefix == b"\x21\x05\xff":
            # Profile request from the scale. Respond directly with a
            # guest-mode user-profile command; the scale will not start
            # a measurement without one:
            #   - fixed-Profile mode: send the caller's Profile;
            #   - user-detection mode: send the bootstrap Profile
            #     (algorithm=0x00, no body fat calculation) so the measurement starts;
            #     when the first stable weight arrives the resolver
            #     fires and overrides this with the resolved Profile;
            #   - weight-only mode: send the bootstrap Profile and
            #     leave the scale running with body fat calculation disabled.
            if not self._state_mask & _STATE_USER_PROFILE:
                self._state_mask |= _STATE_USER_PROFILE
                if self._fixed_profile is not None:
                    profile_to_send = self._fixed_profile
                    log_what = "fixed profile"
                elif self._profile_resolver is not None:
                    profile_to_send = _BOOTSTRAP_PROFILE
                    log_what = (
                        "bootstrap profile (algorithm=0x00); resolved profile "
                        "will follow on first stable weight (detection mode)"
                    )
                else:
                    profile_to_send = _BOOTSTRAP_PROFILE
                    log_what = (
                        "bootstrap profile (algorithm=0x00; weight-only mode — "
                        "scale will not produce body fat)"
                    )
                self._logger.debug(
                    "ES-CS20M profile requested by %s. Sending %s.",
                    address,
                    log_what,
                )
                cmd = _build_command_for_profile(profile_to_send)
                asyncio.create_task(
                    self._safe_write(cmd), name="escs20m-user-profile"
                )
        elif prefix == b"\xa1\x06\x02":
            # Scale ack of the user-profile command. Nothing more to
            # send — the scale will start broadcasting measurement
            # frames once the user steps on it.
            self._logger.debug("ES-CS20M user profile acknowledged by %s", address)
        else:
            self._logger.debug("ES-CS20M ignoring unrecognized payload: %s", payload.hex())

    def _handle_measurement(
        self, payload: bytearray, name: str, address: str
    ) -> None:
        if payload[3] != _GUEST_USER_ID:
            # The library always drives the scale in guest mode, so the
            # scale should echo our guest sentinel (0xFE) on every
            # measurement frame. Anything else means firmware behaviour
            # has shifted under us; warn loudly on every offending frame.
            self._logger.warning(
                "ES-CS20M frame from %s carries non-guest user_id 0x%02x; "
                "library expects 0xFE.",
                address,
                payload[3],
            )

        status = payload[4]

        if status == _MEASUREMENT_STATUS_UNSTABLE:
            self._logger.debug(
                "ES-CS20M unstable measurement received from %s",
                address,
            )
            return

        if status not in (
            _MEASUREMENT_STATUS_STABLE,
            _MEASUREMENT_STATUS_STABLE_WITH_METRICS,
        ):
            self._logger.debug(
                "ES-CS20M measurement with unknown status received from %s: %s",
                address,
                payload.hex(),
            )
            return

        self._logger.debug(
            "ES-CS20M stable weight received from %s status=%s",
            address,
            status,
        )

        # Detection-mode trigger: on the first stable frame, hand the
        # weight to the resolver and write the returned profile so the
        # scale can run body fat calculation before producing its stable-with-metrics
        # frame.
        if (
            self._profile_resolver is not None
            and not self._state_mask & _STATE_PROFILE_RESOLVING
        ):
            self._state_mask |= _STATE_PROFILE_RESOLVING
            weight_kg = round(int.from_bytes(payload[5:7], "big") / 100, 2)
            self._resolver_task = asyncio.create_task(
                self._resolve_and_send_profile(weight_kg, address),
                name="escs20m-resolve-profile",
            )

        if status == _MEASUREMENT_STATUS_STABLE_WITH_METRICS:
            self._logger.debug(
                "ES-CS20M measurement appears final. Scheduling measurement "
                "end command."
            )
            asyncio.create_task(
                self._safe_write(CMD_END_MEASUREMENT),
                name="escs20m-end-measurement",
            )

        data = parse_weight(payload)
        scale_data = ScaleData()
        scale_data.name = name
        scale_data.address = address
        scale_data.display_unit = self.display_unit
        scale_data.measurements = data

        self._notification_callback(scale_data)

    async def _resolve_and_send_profile(
        self, weight_kg: float, address: str
    ) -> None:
        try:
            profile = await self._profile_resolver(weight_kg)
        except asyncio.CancelledError:
            self._logger.debug(
                "ES-CS20M profile resolver cancelled for %s (session ended).",
                address,
            )
            raise
        except Exception as ex:
            self._logger.exception(
                "ES-CS20M profile resolver raised for %s at weight=%s: %s",
                address,
                weight_kg,
                ex,
            )
            return
        if profile is None:
            self._logger.debug(
                "ES-CS20M profile resolver returned None for %s at "
                "weight=%s; leaving bootstrap profile in place (scale "
                "will not produce body fat).",
                address,
                weight_kg,
            )
            return
        self._logger.debug(
            "ES-CS20M profile resolved for %s at weight=%s; overriding "
            "bootstrap profile.",
            address,
            weight_kg,
        )
        await self._safe_write(_build_command_for_profile(profile))

    async def _safe_write(self, data: bytearray) -> None:
        if not self._client:
            self._logger.warning("ES-CS20M cannot send command; no active client")
            return
        if not (
            command_char := self._client.services.get_characteristic(
                COMMAND_CHARACTERISTIC_UUID
            )
        ):
            self._logger.warning("ES-CS20M command characteristic not found, skipping write")
            return
        try:
            await self._client.write_gatt_char(command_char, data)
            self._logger.debug("ES-CS20M TX payload: %s", data.hex())
        except Exception as ex:
            self._logger.error("ES-CS20M failed to send command %s: %s", data.hex(), ex)
            self._state_mask = 0


