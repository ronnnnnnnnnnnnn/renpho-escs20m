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
from typing import Any, NamedTuple

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
    CMD_SET_DISPLAY_UNIT,
    COMMAND_CHARACTERISTIC_UUID,
    FIRMWARE_REVISION_CHARACTERISTIC_UUID,
    NOTIFY_CHARACTERISTIC_UUID,
    RESISTANCE_1_KEY,
    RESISTANCE_2_KEY,
    WEIGHT_KEY,
    _BASIC_STATUS_BIA_RUNNING,
    _BASIC_STATUS_FINAL,
    _BASIC_STATUS_SETTLING,
    _DEFAULT_VENDOR_BYTE,
    _EPOCH_OFFSET,
    _GUEST_PAD_HI,
    _GUEST_PAD_LO,
    _GUEST_USER_ID,
    _LEN_BASIC_MEASUREMENT,
    _LEN_EXTENDED_MEASUREMENT,
    _LEN_EXTENDED_PRE_MEASUREMENT,
    _MEASUREMENT_STATUS_STABLE,
    _MEASUREMENT_STATUS_STABLE_WITH_METRICS,
    _MEASUREMENT_STATUS_UNSTABLE,
    _OP_MEAS_INIT_REQUEST,
    _OP_MEASUREMENT,
    _OP_PRE_MEASUREMENT,
    _OP_PROFILE_ACK,
    _OP_UNIT_REQUEST,
    _USER_PROFILE_TRAILER_TAIL,
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


_DEVICE_METADATA_READ_TIMEOUT_SECONDS = 1.0

_STATE_UNIT_SET = 1
_STATE_MEASUREMENT_INIT = 2
_STATE_USER_PROFILE = 4
_STATE_PROFILE_RESOLVING = 8
# Set once the basic-flavor final (status 0x01) measurement frame has been
# handled, so a repeated final frame does not fire the callback twice.
_STATE_BASIC_FINAL = 16

_DEFAULT_ALGORITHM = 0x04


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


def build_unit_update_command(
    desired_unit: WeightUnit, vendor_byte: int = _DEFAULT_VENDOR_BYTE
) -> bytearray:
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

    ``vendor_byte`` is the byte at offset 2 the scale uses (renpho uses ``0xFF``); 
    it is echoed back so the command is accepted by the scale.
    """
    unit_byte = _WEIGHT_UNIT_TO_BYTE.get(WeightUnit(desired_unit), 0x01)
    payload = bytearray(CMD_SET_DISPLAY_UNIT)
    payload[2] = vendor_byte
    payload[3] = unit_byte
    payload[8] = sum(payload[0:8]) & 0xFF
    return payload


def build_measurement_initiation_command(
    vendor_byte: int = _DEFAULT_VENDOR_BYTE,
) -> bytearray:
    """Build the initiation command with current timestamp and checksum.

    ``vendor_byte`` is echoed at offset 2 (see
    :func:`build_unit_update_command`).
    """
    cmd = bytearray(8)
    cmd[0:3] = bytes([0x20, 0x08, vendor_byte])
    ts = int(time.time()) - _EPOCH_OFFSET
    struct.pack_into("<I", cmd, 3, ts)
    cmd[7] = sum(cmd[0:7]) & 0xFF
    return cmd


def build_end_measurement_command(vendor_byte: int = _DEFAULT_VENDOR_BYTE) -> bytearray:
    cmd = bytearray([0x1F, 0x05, vendor_byte, 0x10])
    cmd.append(sum(cmd) & 0xFF)
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


class _ExtendedFrame(NamedTuple):
    """Decoded extended-flavor measurement frame fields."""

    weight_kg: float
    status: int
    body_fat: float | None
    resistance_1: int | None
    resistance_2: int | None


def parse_extended_measurement(payload: bytearray) -> _ExtendedFrame:
    """
    Parse a live measurement notification.

    Returns a _ExtendedFrame with decoded values. Resistance can be fed
    to :func:`renpho_escs20m.body_metrics.calculate_body_fat` to compute
    body fat retroactively when the user identity is known later than
    the measurement (e.g. after a slow user-detection lookup).
    """
    status = payload[4]
    weight = int.from_bytes(payload[5:7], "big")
    weight_kg = round(float(weight) / 100, 2)

    body_fat = None
    r1 = None
    r2 = None

    if status == _MEASUREMENT_STATUS_STABLE_WITH_METRICS:
        bf_raw = int.from_bytes(payload[11:13], "big")
        if bf_raw:
            body_fat = round(float(bf_raw) / 10, 1)
        r1_raw = int.from_bytes(payload[7:9], "big")
        r2_raw = int.from_bytes(payload[9:11], "big")
        if r1_raw or r2_raw:
            r1 = r1_raw
            r2 = r2_raw

    return _ExtendedFrame(
        weight_kg=weight_kg,
        status=status,
        body_fat=body_fat,
        resistance_1=r1,
        resistance_2=r2,
    )


class _BasicFrame(NamedTuple):
    """Decoded basic-flavor measurement frame fields."""

    weight_kg: float
    status: int
    resistance_1: int
    resistance_2: int


def parse_basic_measurement(payload: bytearray) -> _BasicFrame:
    """Decode a basic-flavor measurement frame (``10 0b``, 11 bytes).

    Layout (no guest-id byte — shifted left vs the 14-byte extended frame)::

        0..2   prefix 10 0b <vendor>
        3..4   weight, big-endian uint16, 0.01 kg
        5      status: 0x00 settling, 0x11 stable + BIA running, 0x01 final
        6..7   resistance 1 (only meaningful on the 0x01 final frame)
        8..9   resistance 2 (only meaningful on the 0x01 final frame)
        10     checksum

    The caller validates length (>= 10 bytes) and dispatches on ``status``.
    Resistance is only meaningful on the final (``0x01``) frame; on
    settling / BIA-running frames it reads as 0.
    """
    return _BasicFrame(
        weight_kg=round(int.from_bytes(payload[3:5], "big") / 100, 2),
        status=payload[5],
        resistance_1=int.from_bytes(payload[6:8], "big"),
        resistance_2=int.from_bytes(payload[8:10], "big"),
    )


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
            char = client.services.get_characteristic(BATTERY_LEVEL_CHARACTERISTIC_UUID)
            if char is not None:
                data = await asyncio.wait_for(
                    client.read_gatt_char(char),
                    timeout=_DEVICE_METADATA_READ_TIMEOUT_SECONDS,
                )
                if not data:
                    return None
                return data[0]
        except Exception:
            _LOGGER.debug("ES-CS20M failed to read battery level", exc_info=True)
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
            _LOGGER.debug("ES-CS20M failed to read firmware revision", exc_info=True)
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
        max_connect_attempts: int = 2,
        logger: logging.Logger | None = None,
    ) -> None:
        if max_connect_attempts < 1:
            raise ValueError(
                f"max_connect_attempts must be >= 1; got {max_connect_attempts}"
            )

        self._logger = logger or _LOGGER
        self._logger.info("Initializing RenphoESCS20MScale for address: %s", address)

        self.address = address
        self._client: BleakClient | None = None
        self._initializing: bool = False
        self._notification_callback = notification_callback
        self._cooldown_seconds = cooldown_seconds
        self._cooldown_end_time: float = 0
        self._max_connect_attempts = max_connect_attempts
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

        self._bg_tasks: set[asyncio.Task] = set()

        # Per-device frame byte (offset 2), detected per-session from the wire
        # and echoed back in our replies; defaults to renpho's 0xFF.
        self._vendor_byte: int = _DEFAULT_VENDOR_BYTE

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

    def _fire_and_forget(self, coro: Awaitable[Any], name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    @property
    def battery_level(self) -> int | None:
        """Last successfully-read battery level.

        Normally 0-100 (percent), per the BLE SIG definition. Out-of-range
        values (e.g. 255) are passed through unmodified rather than clamped
        or rejected, so a misbehaving firmware can surface here for the
        consumer to handle — don't assume the value is always within range.

        Possible reliability caveat: on at least one observed unit (Qing Niu
        firmware ``V10.0``) the scale reported a static ``100`` and did not
        appear to decrement it as the cells drained — it was still reading
        100% on batteries weak enough to need replacing. That unit also
        exposed no other battery source over BLE. Whether this holds across
        other hardware revisions or firmware versions is unknown, so treat a
        steady 100% as *possibly* unreliable rather than assuming it on every
        device — the value is reported as-is and may well be accurate on yours.

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
                    max_attempts=self._max_connect_attempts,
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
        self._vendor_byte = _DEFAULT_VENDOR_BYTE
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
        if len(payload) < 2:
            self._logger.debug(
                "ES-CS20M ignoring unrecognized payload: %s", payload.hex()
            )
            return
        opcode, length = payload[0], payload[1]

        # Byte 2 seems to be a per-device value.
        # Capture it from frames that carry it so our replies echo it back.
        # (The profile command/ack frames use 0x02 there instead, so they're
        # excluded from capture.)
        if (
            opcode
            in (
                _OP_MEASUREMENT,
                _OP_UNIT_REQUEST,
                _OP_MEAS_INIT_REQUEST,
                _OP_PRE_MEASUREMENT,
            )
            and len(payload) >= 3
        ):
            vendor = payload[2]
            if vendor != _DEFAULT_VENDOR_BYTE and vendor != self._vendor_byte:
                # Surface non-renpho scales once per session; replies echo the
                # scale's own byte, but support for them is best-effort.
                self._logger.info(
                    "ES-CS20M %s reports vendor byte 0x%02x (renpho is 0x%02x); "
                    "replies will echo it — non-renpho QN-Scale support is "
                    "best-effort.",
                    address,
                    vendor,
                    _DEFAULT_VENDOR_BYTE,
                )
            self._vendor_byte = vendor

        # Dispatch by opcode (byte 0); byte 1 (length) selects the flavor on
        # the measurement and pre-measurement frames. The extended flavor
        # (HVIN ESCS20MA2) computes body fat on-device from a guest profile;
        # the basic flavor (HVIN ESCS20MN) streams weight + raw impedance only.
        if opcode == _OP_UNIT_REQUEST:
            self._handle_unit_request(address)
        elif opcode == _OP_MEAS_INIT_REQUEST:
            self._handle_meas_init_request(address)
        elif opcode == _OP_PRE_MEASUREMENT:
            # Only renpho's extended scale takes a profile over BLE, and the
            # profile sub-protocol is renpho-specific — so gate it on the
            # renpho vendor byte. A non-renpho scale that happens to send the
            # same length (e.g. 21 05 with a different vendor byte) is treated
            # as basic: no profile reply, it streams measurements on its own.
            if (
                length == _LEN_EXTENDED_PRE_MEASUREMENT
                and self._vendor_byte == _DEFAULT_VENDOR_BYTE
            ):
                self._handle_extended_pre_measurement(address)
            else:
                self._handle_basic_pre_measurement(address)
        elif opcode == _OP_MEASUREMENT and length == _LEN_EXTENDED_MEASUREMENT:
            self._handle_extended_measurement(payload, name, address)
        elif opcode == _OP_MEASUREMENT and length == _LEN_BASIC_MEASUREMENT:
            self._handle_basic_measurement(payload, name, address)
        elif opcode == _OP_PROFILE_ACK:
            # Extended-flavor ack of our user-profile command; nothing to send.
            self._logger.debug("ES-CS20M user profile acknowledged by %s", address)
        else:
            self._logger.debug(
                "ES-CS20M ignoring unrecognized payload: %s", payload.hex()
            )

    def _handle_unit_request(self, address: str) -> None:
        """Reply to the scale's display-unit request (shared across variants)."""
        if self._state_mask & _STATE_UNIT_SET:
            return
        self._state_mask |= _STATE_UNIT_SET
        self._logger.debug(
            "ES-CS20M unit negotiation requested by %s. Sending set-unit reply.",
            address,
        )
        self._fire_and_forget(
            self._safe_write(
                build_unit_update_command(self.display_unit, self._vendor_byte)
            ),
            name="escs20m-unit-update",
        )

    def _handle_meas_init_request(self, address: str) -> None:
        """Reply to the scale's measurement-init request (shared across variants)."""
        if self._state_mask & _STATE_MEASUREMENT_INIT:
            return
        self._state_mask |= _STATE_MEASUREMENT_INIT
        self._logger.debug(
            "ES-CS20M measurement initiation requested by %s. Sending timestamp.",
            address,
        )
        self._fire_and_forget(
            self._safe_write(build_measurement_initiation_command(self._vendor_byte)),
            name="escs20m-measurement-init",
        )

    def _handle_extended_pre_measurement(self, address: str) -> None:
        """Reply to the extended-flavor pre-measurement frame — a profile request.

        The scale will not start a measurement without a profile reply:
          - fixed-Profile mode: send the caller's Profile;
          - user-detection mode: send the bootstrap Profile (algorithm=0x00,
            no body fat) so the measurement starts; the resolver fires on
            the first stable weight and overrides it;
          - weight-only mode: send the bootstrap Profile and leave body fat
            calculation disabled.
        """
        if self._state_mask & _STATE_USER_PROFILE:
            return
        self._state_mask |= _STATE_USER_PROFILE
        if self._fixed_profile is not None:
            profile_to_send = self._fixed_profile
            log_what = "fixed profile"
        elif self._profile_resolver is not None:
            profile_to_send = _BOOTSTRAP_PROFILE
            log_what = (
                "bootstrap profile (algorithm=0x00); resolved profile will "
                "follow on first stable weight (detection mode)"
            )
        else:
            profile_to_send = _BOOTSTRAP_PROFILE
            log_what = (
                "bootstrap profile (algorithm=0x00; weight-only mode — scale "
                "will not produce body fat)"
            )
        self._logger.debug(
            "ES-CS20M profile requested by %s. Sending %s.", address, log_what
        )
        self._fire_and_forget(
            self._safe_write(_build_command_for_profile(profile_to_send)),
            name="escs20m-user-profile",
        )

    def _handle_basic_pre_measurement(self, address: str) -> None:
        """Acknowledge the basic-flavor pre-measurement frame — a no-op.

        This variant takes no profile over BLE and needs no reply; the scale
        begins streaming measurements on its own. Recognized here only so it
        isn't logged as an unrecognized payload. Body fat is computed
        off-scale from ``resistance_1`` + the caller's profile.
        """
        if self._state_mask & _STATE_USER_PROFILE:
            return
        self._state_mask |= _STATE_USER_PROFILE
        self._logger.debug(
            "QN basic-flavor pre-measurement frame from %s; this variant takes no profile "
            "over BLE (scale streams on its own). Body fat is computed "
            "off-scale from resistance.",
            address,
        )

    def _handle_extended_measurement(
        self, payload: bytearray, name: str, address: str
    ) -> None:
        """Handle an extended-flavor measurement broadcast (``10 0e``, 14 bytes)."""
        if len(payload) < _LEN_EXTENDED_MEASUREMENT:
            self._logger.debug(
                "ES-CS20M measurement frame from %s too short (%d bytes): %s",
                address,
                len(payload),
                payload.hex(),
            )
            return
        if payload[3] != _GUEST_USER_ID:
            # The library always drives the scale in guest mode, so the scale
            # should echo our guest sentinel (0xFE) on every measurement
            # frame. Anything else means firmware behaviour has shifted under
            # us; warn loudly on every offending frame.
            self._logger.warning(
                "ES-CS20M frame from %s carries non-guest user_id 0x%02x; "
                "library expects 0xFE.",
                address,
                payload[3],
            )
        frame = parse_extended_measurement(payload)

        if frame.status == _MEASUREMENT_STATUS_UNSTABLE:
            self._logger.debug(
                "ES-CS20M unstable measurement received from %s", address
            )
            return

        self._logger.debug(
            "ES-CS20M stable weight received from %s status=%s", address, frame.status
        )

        # Detection-mode trigger: on the first stable frame, hand the weight
        # to the resolver so the scale can run body fat calculation before
        # the stable-with-metrics frame.
        if frame.status == _MEASUREMENT_STATUS_STABLE:
            if (
                self._profile_resolver is not None
                and not self._state_mask & _STATE_PROFILE_RESOLVING
            ):
                self._state_mask |= _STATE_PROFILE_RESOLVING
                self._resolver_task = asyncio.create_task(
                    self._resolve_and_send_profile(frame.weight_kg, address),
                    name="escs20m-resolve-profile",
                )
        elif frame.status == _MEASUREMENT_STATUS_STABLE_WITH_METRICS:
            self._logger.debug(
                "ES-CS20M measurement appears final. Scheduling measurement "
                "end command."
            )
            self._fire_and_forget(
                self._safe_write(build_end_measurement_command(self._vendor_byte)),
                name="escs20m-end-measurement",
            )

            metrics: dict[str, int | float | None] = {WEIGHT_KEY: frame.weight_kg}
            if frame.body_fat is not None:
                metrics[BODY_FAT_KEY] = frame.body_fat
            if frame.resistance_1 is not None:
                metrics[RESISTANCE_1_KEY] = frame.resistance_1
                metrics[RESISTANCE_2_KEY] = frame.resistance_2

            self._notification_callback(
                ScaleData(
                    name=name,
                    address=address,
                    display_unit=self.display_unit,
                    measurements=metrics,
                )
            )
        else:
            self._logger.warning(
                "ES-CS20M measurement with unknown status received from %s: %s",
                address,
                frame.status,
            )

    def _handle_basic_measurement(
        self, payload: bytearray, name: str, address: str
    ) -> None:
        """Handle a basic-flavor measurement broadcast (``10 0b``, 11 bytes)."""
        if len(payload) < _LEN_BASIC_MEASUREMENT:
            self._logger.warning(
                "QN basic-flavor measurement frame from %s too short (%d bytes): %s",
                address,
                len(payload),
                payload.hex(),
            )
            return

        frame = parse_basic_measurement(payload)

        if frame.status == _BASIC_STATUS_SETTLING:
            self._logger.debug(
                "QN basic-flavor settling frame from %s: weight=%.2f kg",
                address,
                frame.weight_kg,
            )
            return
        if frame.status == _BASIC_STATUS_BIA_RUNNING:
            self._logger.debug(
                "QN basic-flavor stable frame from %s, BIA running: weight=%.2f kg",
                address,
                frame.weight_kg,
            )
            return
        if frame.status != _BASIC_STATUS_FINAL:
            self._logger.warning(
                "QN basic-flavor measurement frame from %s has unexpected status "
                "0x%02x (expected 0x00/0x11/0x01); ignoring: %s",
                address,
                frame.status,
                payload.hex(),
            )
            return

        # Final frame. Guard against a repeated 0x01 frame firing twice.
        if self._state_mask & _STATE_BASIC_FINAL:
            self._logger.debug(
                "QN basic-flavor duplicate final frame from %s; already handled, "
                "ignoring: %s",
                address,
                payload.hex(),
            )
            return
        self._state_mask |= _STATE_BASIC_FINAL

        if not (frame.resistance_1 or frame.resistance_2):
            self._logger.warning(
                "QN basic-flavor final frame from %s (status 0x01) carries zero "
                "impedance; BIA result may be missing: %s",
                address,
                payload.hex(),
            )

        self._logger.debug(
            "QN basic-flavor final measurement from %s: weight=%.2f kg, r1=%d, "
            "r2=%d. Firing callback and sending end-measurement.",
            address,
            frame.weight_kg,
            frame.resistance_1,
            frame.resistance_2,
        )
        self._fire_and_forget(
            self._safe_write(build_end_measurement_command(self._vendor_byte)),
            name="escs20mn-end-measurement",
        )

        data: dict[str, str | float | None] = {WEIGHT_KEY: frame.weight_kg}
        if frame.resistance_1 or frame.resistance_2:
            data[RESISTANCE_1_KEY] = frame.resistance_1
            data[RESISTANCE_2_KEY] = frame.resistance_2
        self._notification_callback(
            ScaleData(
                name=name,
                address=address,
                display_unit=self.display_unit,
                measurements=data,
            )
        )

    async def _resolve_and_send_profile(self, weight_kg: float, address: str) -> None:
        try:
            profile = await self._profile_resolver(weight_kg)
        except asyncio.CancelledError:
            self._logger.debug(
                "ES-CS20M profile resolver cancelled for %s (session ended).",
                address,
            )
            raise
        except Exception:
            self._logger.exception(
                "ES-CS20M profile resolver raised for %s at weight=%s",
                address,
                weight_kg,
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
            self._logger.warning(
                "ES-CS20M command characteristic not found, skipping write"
            )
            return
        try:
            await self._client.write_gatt_char(command_char, data)
            self._logger.debug("ES-CS20M TX payload: %s", data.hex())
        except Exception:
            self._logger.exception("ES-CS20M failed to send command %s", data.hex())
            self._state_mask = 0
