"""Transport base classes shared by every Renpho scale variant.

The hierarchy mirrors the ``etekcity_esf551_ble`` library:

- :class:`RenphoScale` — transport-agnostic: BLE scanner setup + lifecycle,
  address filtering, and the notification callback.
- :class:`GattScale` — variants that deliver measurements over a GATT
  connection (QN / ES-CS20M, and future 0x55aa / ES-26).
- :class:`AdvertisementScale` — variants that broadcast measurements in their
  BLE advertisements with no connection (0xaabb).

Protocol-specific handling lives in the per-protocol subpackages
(``escs20m/``, ``broadcast/``).
"""

from __future__ import annotations

import abc
import asyncio
import logging
import platform
import time
from collections.abc import Awaitable, Callable
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

from .const import (
    BATTERY_LEVEL_CHARACTERISTIC_UUID,
    FIRMWARE_REVISION_CHARACTERISTIC_UUID,
)
from .data import BluetoothScanningMode, ScaleData, WeightUnit

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


async def _read_device_metadata(
    client: BleakClient,
) -> tuple[int | None, str | None]:
    """Read battery level and firmware revision from the scale.

    Each read is independent and best-effort: any failure (characteristic
    absent, slow response, BLE error, decode error, or empty payload) returns
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
            _LOGGER.debug("Failed to read battery level", exc_info=True)
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
            _LOGGER.debug("Failed to read firmware revision", exc_info=True)
        return None

    battery, firmware = await asyncio.gather(read_battery(), read_firmware())
    return battery, firmware


class RenphoScale(abc.ABC):
    """
    Abstract base for every Renpho scale variant.

    Handles the parts common to every model regardless of how measurements are
    obtained: BLE scanner setup and lifecycle, address filtering, and the
    notification callback. Transport-specific behaviour lives in the
    :class:`GattScale` and :class:`AdvertisementScale` subclasses.
    """

    def __init__(
        self,
        address: str,
        notification_callback: Callable[[ScaleData], None],
        display_unit: WeightUnit = WeightUnit.KG,
        *,
        scanning_mode: BluetoothScanningMode = BluetoothScanningMode.ACTIVE,
        adapter: str | None = None,
        bleak_scanner_backend: BaseBleakScanner | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._logger = logger or _LOGGER
        self._logger.info(
            "Initializing %s for address: %s", type(self).__name__, address
        )

        self.address = address
        self._notification_callback = notification_callback
        self._display_unit: WeightUnit = WeightUnit(display_unit)
        self._bg_tasks: set[asyncio.Task] = set()

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
    def display_unit(self) -> WeightUnit:
        return self._display_unit

    @display_unit.setter
    def display_unit(self, value: WeightUnit) -> None:
        if value is None:
            raise ValueError("display_unit cannot be None")
        self._display_unit = WeightUnit(value)

    @abc.abstractmethod
    async def _advertisement_callback(
        self, ble_device: BLEDevice, advertisement_data: AdvertisementData
    ) -> None:
        """Handle an advertisement from the scanner for the target scale."""

    async def async_start(self) -> None:
        """Start BLE scanning and begin listening for the target scale."""
        self._logger.debug("Starting scanner for %s", self.address)
        try:
            async with self._lock:
                await self._scanner.start()
        except Exception as ex:
            self._logger.error("Failed to start scanner: %s", ex)
            raise

    async def async_stop(self) -> None:
        """Stop BLE scanning."""
        self._logger.debug("Stopping scanner for %s", self.address)
        try:
            async with self._lock:
                await self._scanner.stop()
        except Exception as ex:
            self._logger.error("Failed to stop scanner: %s", ex)
            raise


class GattScale(RenphoScale, abc.ABC):
    """
    Base for scales that deliver measurements over a GATT connection.

    On detecting the target scale's advertisement a connection is established
    and model-specific setup runs in :meth:`_start_scale_session`; measurements
    then arrive via :meth:`_notification_handler`. An optional cooldown period
    ignores advertisements for a while after a disconnection.
    """

    def __init__(
        self,
        address: str,
        notification_callback: Callable[[ScaleData], None],
        display_unit: WeightUnit = WeightUnit.KG,
        *,
        scanning_mode: BluetoothScanningMode = BluetoothScanningMode.ACTIVE,
        adapter: str | None = None,
        bleak_scanner_backend: BaseBleakScanner | None = None,
        cooldown_seconds: int = 5,
        max_connect_attempts: int = 2,
        logger: logging.Logger | None = None,
    ) -> None:
        if max_connect_attempts < 1:
            raise ValueError(
                f"max_connect_attempts must be >= 1; got {max_connect_attempts}"
            )
        super().__init__(
            address,
            notification_callback,
            display_unit,
            scanning_mode=scanning_mode,
            adapter=adapter,
            bleak_scanner_backend=bleak_scanner_backend,
            logger=logger,
        )
        self._client: BleakClient | None = None
        self._initializing: bool = False
        self._cooldown_seconds = cooldown_seconds
        self._cooldown_end_time: float = 0
        self._max_connect_attempts = max_connect_attempts
        self._battery_level: int | None = None
        self._firmware_revision: str | None = None

    @property
    def battery_level(self) -> int | None:
        """Last successfully-read battery level.

        Normally 0-100 (percent), per the BLE SIG definition. Out-of-range
        values (e.g. 255) are passed through unmodified rather than clamped
        or rejected, so a misbehaving firmware can surface here for the
        consumer to handle — don't assume the value is always within range.

        Possible reliability caveat: on at least one observed unit (Qing Niu
        firmware ``V10.0``) the scale reported a static ``100`` and did not
        appear to decrement it as the cells drained. Whether this holds across
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

    def _unavailable_callback(self, _: BleakClient) -> None:
        self._logger.debug("Scale disconnected")
        self._cooldown_end_time = time.time() + self._cooldown_seconds
        self._client = None

    @abc.abstractmethod
    async def _start_scale_session(self, ble_device: BLEDevice) -> None:
        """Post-connection setup: read metadata, register notifications."""

    @abc.abstractmethod
    def _notification_handler(
        self, _: BleakGATTCharacteristic, payload: bytearray, name: str, address: str
    ) -> None:
        """Handle a raw notification payload from the scale."""

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


class AdvertisementScale(RenphoScale, abc.ABC):
    """
    Base for scales that broadcast measurements in their BLE advertisements,
    with no GATT connection.

    Each advertisement from the target scale has its manufacturer-data entries
    passed to :meth:`_parse`; a non-``None`` result is wrapped in a
    :class:`ScaleData` and delivered to the notification callback.
    """

    # Fallback device name used when the advertisement carries none.
    _model_name: str = ""

    @RenphoScale.display_unit.setter
    def display_unit(self, value: WeightUnit) -> None:
        # Advertisement-only scales report the unit observed in their
        # advertisements; it cannot be commanded. Ignore writes (but log so the
        # caller can tell why a requested unit had no effect).
        if value is not None:
            self._logger.debug(
                "Ignoring display_unit=%s; %s reports the unit observed in "
                "advertisements and cannot set it on the scale",
                value,
                type(self).__name__,
            )

    @abc.abstractmethod
    def _parse(
        self, company_id: int, payload: bytearray
    ) -> dict[str, str | float | None] | None:
        """Parse one manufacturer-data entry into a measurements dict.

        Returns ``None`` if the entry is not from this scale or the reading is
        not usable yet (e.g. not stable).
        """

    def _display_unit_for(
        self, parsed: dict[str, str | float | None]
    ) -> WeightUnit | None:
        """Return the unit shown on the scale's display for this reading.

        Receives the dict from :meth:`_parse` and may ``pop`` a display-unit
        entry out of it so it does not leak into ``measurements``. Defaults to
        ``None`` (unknown).
        """
        return None

    async def _advertisement_callback(
        self, ble_device: BLEDevice, advertisement_data: AdvertisementData
    ) -> None:
        if ble_device.address != self.address:
            return

        for company_id, mfr_bytes in advertisement_data.manufacturer_data.items():
            payload = bytearray(mfr_bytes)
            self._logger.debug(
                "Raw manufacturer data from %s: company=0x%04x %s",
                ble_device.address,
                company_id,
                payload.hex(),
            )
            parsed = self._parse(company_id, payload)
            if parsed:
                display_unit = self._display_unit_for(parsed)
                if display_unit is not None:
                    self._display_unit = display_unit
                self._notification_callback(
                    ScaleData(
                        name=ble_device.name or self._model_name,
                        address=ble_device.address,
                        display_unit=display_unit
                        if display_unit is not None
                        else self._display_unit,
                        measurements=parsed,
                    )
                )
                return
