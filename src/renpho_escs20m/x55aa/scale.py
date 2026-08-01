"""Renpho ES-CS20M 0x55aa variant (LeFu hardware) - GATT connection variant.

Exposes vendor service 0x1A10 with 0x2A10 (notify) and 0x2A11 (write) rather
than the QN-series FFF0/FFE0 layout. No profile is exchanged: the scale runs
no on-device body composition, so body fat is computed off-scale from
``resistance_1`` via :func:`renpho_escs20m.calculate_body_fat`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import BaseBleakScanner

from ..const import (
    RESISTANCE_1_KEY,
    RESISTANCE_2_KEY,
    WEIGHT_KEY,
    X55AA_COMMAND_CHARACTERISTIC_UUID,
    X55AA_NOTIFY_CHARACTERISTIC_UUID,
)
from ..data import BluetoothScanningMode, ScaleData, WeightUnit
from ..scale import GattScale
from .protocol import Reading, aggregate_session, iter_frames, parse_reading

# Written to the command characteristic after subscribing; the scale streams
# without it on observed units, but the official app sends one.
_HELLO = b"\x01"


class Renpho55AAScale(GattScale):
    """Renpho ES-CS20M variant speaking the ``0x55aa`` protocol.

    Emits one callback per weigh-in with ``{"weight": kg}`` plus
    ``resistance_1``/``resistance_2`` when the scale's bioimpedance pass
    produced them. Weight is always kilograms; the display unit cannot be
    commanded over this protocol.
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
        cooldown_seconds: int = 0,
        max_connect_attempts: int = 2,
        settle_seconds: float = 2.5,
        impedance_wait_seconds: float = 10.0,
        send_hello: bool = True,
        logger: logging.Logger | None = None,
    ) -> None:
        super().__init__(
            address,
            notification_callback,
            display_unit,
            scanning_mode=scanning_mode,
            adapter=adapter,
            bleak_scanner_backend=bleak_scanner_backend,
            cooldown_seconds=cooldown_seconds,
            max_connect_attempts=max_connect_attempts,
            logger=logger,
        )
        self._settle_seconds = settle_seconds
        self._impedance_wait_seconds = impedance_wait_seconds
        self._send_hello = send_hello
        self._buffer = bytearray()
        self._session: list[Reading] = []
        self._settle_handle: asyncio.TimerHandle | None = None
        self._session_name = ""

    @GattScale.display_unit.setter
    def display_unit(self, value: WeightUnit) -> None:
        if value is not None:
            self._logger.debug(
                "Ignoring display_unit=%s; the 0x55aa protocol has no unit command",
                value,
            )

    async def _start_scale_session(self, ble_device: BLEDevice) -> None:
        client = self._client
        if client is None:
            return

        self._buffer.clear()
        self._session.clear()
        self._session_name = ble_device.name or ""

        try:
            await self._populate_device_metadata(client)

            char = client.services.get_characteristic(X55AA_NOTIFY_CHARACTERISTIC_UUID)
            if char is None:
                self._logger.error("0x55aa notification characteristic not found")
                return

            def handler(c: BleakGATTCharacteristic, data: bytearray) -> None:
                self._notification_handler(
                    c, data, ble_device.name, ble_device.address
                )

            await client.start_notify(char, handler)

            if self._send_hello:
                command = client.services.get_characteristic(
                    X55AA_COMMAND_CHARACTERISTIC_UUID
                )
                if command is not None:
                    await client.write_gatt_char(command, _HELLO, response=False)
        except Exception as ex:
            self._logger.exception("%s(%s)", type(ex), ex.args)
            self._client = None

    def _notification_handler(
        self, _: BleakGATTCharacteristic, payload: bytearray, name: str, address: str
    ) -> None:
        self._logger.debug("0x55aa RX payload: %s", payload.hex())
        self._buffer.extend(payload)

        for frame in iter_frames(self._buffer):
            reading = parse_reading(frame)
            if reading is None:
                self._logger.debug(
                    "0x55aa status frame type=0x%02x payload=%s",
                    frame.type,
                    frame.payload.hex(),
                )
                continue
            self._session.append(reading)
            self._arm_settle_timer(name, address)

    def _arm_settle_timer(self, name: str, address: str) -> None:
        """Restart the quiet period that ends a weigh-in.

        The scale pauses between its weight and bioimpedance phases, so a
        session is only cut short once a usable resistance has arrived.
        Asking the aggregator keeps this in step with what the reading will
        actually contain: a raw scan would be satisfied by the ramp-time
        resistance that :func:`aggregate_session` discards.
        """
        if self._settle_handle is not None:
            self._settle_handle.cancel()

        so_far = aggregate_session(self._session)
        delay = (
            self._settle_seconds
            if so_far is not None and so_far.resistance is not None
            else self._impedance_wait_seconds
        )
        self._settle_handle = asyncio.get_running_loop().call_later(
            delay, self._deliver_session, name, address
        )

    def _deliver_session(self, name: str, address: str) -> None:
        self._settle_handle = None
        readings, self._session = self._session, []
        reading = aggregate_session(readings)
        if reading is None:
            return

        self._logger.debug(
            "0x55aa weigh-in from %s: weight=%.2f kg, resistance=%s (%d frames)",
            address,
            reading.weight_kg,
            reading.resistance,
            len(readings),
        )

        measurements: dict[str, str | float | None] = {WEIGHT_KEY: reading.weight_kg}
        if reading.resistance is not None:
            measurements[RESISTANCE_1_KEY] = reading.resistance
        if reading.secondary is not None:
            measurements[RESISTANCE_2_KEY] = reading.secondary

        self._notification_callback(
            ScaleData(
                name=name or self._session_name,
                address=address or self.address,
                display_unit=self.display_unit,
                measurements=measurements,
            )
        )

    def _unavailable_callback(self, client) -> None:
        if self._settle_handle is not None:
            self._settle_handle.cancel()
            self._settle_handle = None
        if self._session:
            self._deliver_session(self._session_name, self.address)
        super()._unavailable_callback(client)
