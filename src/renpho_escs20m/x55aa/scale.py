"""Renpho ``0x55aa`` variant (LeFu hardware) — GATT connection client.

Basic flavor: the scale streams weight over notify characteristic
``0x2A10`` on vendor service ``0x1A10`` with no writes required, and
computes no body composition on-device. The client subscribes and
listens; body fat is computed off-scale from ``resistance_1`` via
:func:`renpho_escs20m.calculate_body_fat`.

Coexistence with the official Renpho app: by default the client never
writes to the scale — in particular it never acknowledges stored offline
records (``0x15``), which are believed to be deleted by the
acknowledgement, so any readings taken while the app was away remain
intact for the app to collect. The opt-in ``clear_stored_measurements``
flag acknowledges each stored record instead (best-effort; not yet
verified against real hardware).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import BaseBleakScanner

from ..const import (
    RESISTANCE_1_KEY,
    WEIGHT_KEY,
    X55AA_COMMAND_CHARACTERISTIC_UUID,
    X55AA_NOTIFY_CHARACTERISTIC_UUID,
)
from ..data import BluetoothScanningMode, ScaleData, WeightUnit
from ..scale import GattScale, ScaleSessionError
from .protocol import (
    CMD_ACK,
    CMD_MEASUREMENT,
    CMD_STATUS,
    CMD_STATUS_ALT,
    CMD_STORED_RECORD,
    Frame,
    iter_frames,
    parse_measurement,
    parse_status,
    parse_stored_record,
)

# Acknowledgement for a stored offline record (``0x95``, payload ``0x01``).
# Believed to make the scale delete the acknowledged record.
_STORED_RECORD_ACK = bytes.fromhex("55aa950001" "0196")


class Renpho55AAScale(GattScale):
    """Client for scales speaking the ``0x55aa`` protocol (basic flavor).

    Emits one callback per weigh-in, on the scale's final measurement
    frame, with ``{"weight": kg}`` plus ``resistance_1`` (ohms) when the
    bioimpedance pass produced one. Weight on the wire is always
    kilograms regardless of the configured display unit.

    ``display_unit`` is observed, not commanded: this protocol's
    display-unit command is not sent by the library, and the reported
    unit follows what the scale announces in its status frames.

    Stored offline records the scale pushes at connect are logged and
    discarded; they are never delivered via the callback.

    ``clear_stored_measurements`` (default ``False``) acknowledges each
    stored record after it is received. The acknowledgement is believed
    to make the scale delete the stored reading, which is why the default
    is off: draining hides those readings from the official Renpho app.
    This has not yet been verified against real hardware, so the option
    is best-effort. When off, the client is strictly notify-only.
    """

    def __init__(
        self,
        address: str,
        notification_callback: Callable[[ScaleData], None],
        display_unit: WeightUnit = WeightUnit.KG,
        *,
        clear_stored_measurements: bool = False,
        scanning_mode: BluetoothScanningMode = BluetoothScanningMode.ACTIVE,
        adapter: str | None = None,
        bleak_scanner_backend: BaseBleakScanner | None = None,
        cooldown_seconds: int = 0,
        max_connect_attempts: int = 2,
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
        self._clear_stored_measurements = clear_stored_measurements
        self._command_char: BleakGATTCharacteristic | None = None
        # Serializes acks: a burst of stored records would otherwise overlap
        # write-with-response calls, which some BLE stacks reject.
        self._write_lock = asyncio.Lock()
        self._buffer = bytearray()
        self._final_fired = False
        self._warned_statuses: set[int] = set()

    @GattScale.display_unit.setter
    def display_unit(self, value: WeightUnit) -> None:
        if value is not None:
            self._logger.debug(
                "Ignoring display_unit=%s; the 0x55aa protocol's unit is "
                "observed from the scale, not set by the library",
                value,
            )

    async def _start_scale_session(self, ble_device: BLEDevice) -> None:
        client = self._client
        if client is None:
            return

        self._buffer.clear()
        self._final_fired = False
        self._warned_statuses.clear()
        self._command_char = None

        await self._populate_device_metadata(client)

        char = client.services.get_characteristic(X55AA_NOTIFY_CHARACTERISTIC_UUID)
        if char is None:
            raise ScaleSessionError(
                "0x55aa notification characteristic (2A10) not found"
            )

        if self._clear_stored_measurements:
            self._command_char = client.services.get_characteristic(
                X55AA_COMMAND_CHARACTERISTIC_UUID
            )
            if self._command_char is None:
                self._logger.warning(
                    "0x55aa command characteristic (2A11) not found on %s; "
                    "stored offline records will not be acknowledged",
                    ble_device.address,
                )

        def handler(c: BleakGATTCharacteristic, data: bytearray) -> None:
            self._notification_handler(c, data, ble_device.name, ble_device.address)

        await client.start_notify(char, handler)

    def _notification_handler(
        self,
        _: BleakGATTCharacteristic,
        payload: bytearray,
        name: str | None,
        address: str,
    ) -> None:
        self._logger.debug("0x55aa RX payload from %s: %s", address, payload.hex())
        self._buffer.extend(payload)

        for frame in iter_frames(self._buffer):
            if frame.cmd == CMD_MEASUREMENT:
                self._handle_measurement(frame, name, address)
            elif frame.cmd == CMD_STORED_RECORD:
                self._handle_stored_record(frame, address)
            elif frame.cmd == CMD_STATUS:
                self._handle_status(frame, address)
            elif frame.cmd in (CMD_ACK, CMD_STATUS_ALT):
                self._logger.debug(
                    "0x55aa frame 0x%02x from %s: %s",
                    frame.cmd,
                    address,
                    frame.payload.hex(),
                )
            else:
                self._logger.debug(
                    "0x55aa ignoring unrecognized frame 0x%02x from %s: %s",
                    frame.cmd,
                    address,
                    frame.payload.hex(),
                )

    def _handle_measurement(self, frame: Frame, name: str | None, address: str) -> None:
        measurement = parse_measurement(frame)
        if measurement is None:
            self._logger.debug(
                "0x55aa short measurement frame from %s: %s",
                address,
                frame.payload.hex(),
            )
            return

        if measurement.is_settling:
            # A new settling phase re-arms the final for scales that stay
            # connected across weigh-ins.
            self._final_fired = False
            self._logger.debug(
                "0x55aa settling frame from %s: weight=%.2f kg",
                address,
                measurement.weight_kg,
            )
            return

        if not measurement.is_final:
            # Unsupported status: skip the reading, and surface it loudly
            # (once per status per connection) rather than misparse it.
            # Some scales have app-settable modes (e.g. for weighing during
            # pregnancy or holding a baby) that this library deliberately
            # does not support.
            if measurement.status not in self._warned_statuses:
                self._warned_statuses.add(measurement.status)
                self._logger.warning(
                    "0x55aa unsupported measurement status 0x%02x from %s; "
                    "reading skipped (possibly a scale mode this library "
                    "does not support): %s — please report this on the "
                    "issue tracker",
                    measurement.status,
                    address,
                    frame.payload.hex(),
                )
            return

        if self._final_fired:
            self._logger.debug(
                "0x55aa duplicate final frame from %s; already handled",
                address,
            )
            return
        self._final_fired = True

        measurements: dict[str, str | float | None] = {
            WEIGHT_KEY: measurement.weight_kg
        }
        if measurement.resistance:
            measurements[RESISTANCE_1_KEY] = measurement.resistance

        self._logger.debug(
            "0x55aa final measurement from %s: weight=%.2f kg, "
            "resistance=%s. Firing callback.",
            address,
            measurement.weight_kg,
            measurement.resistance or None,
        )
        self._notification_callback(
            ScaleData(
                name=name or "",
                address=address,
                display_unit=self.display_unit,
                measurements=measurements,
            )
        )

    def _handle_stored_record(self, frame: Frame, address: str) -> None:
        record = parse_stored_record(frame)
        if record is None:
            self._logger.debug(
                "0x55aa short stored-record frame from %s: %s",
                address,
                frame.payload.hex(),
            )
            return
        # Historical reading (possibly another person's). Deliberately not
        # reported as a live measurement. Acknowledged only when opted in:
        # the ack is believed to delete the record, and leaving it intact
        # keeps it available to the official app.
        self._logger.debug(
            "0x55aa stored offline record from %s (discarded): "
            "weight=%.2f kg, resistance=%d, measured %d seconds ago",
            address,
            record.weight_kg,
            record.resistance,
            record.seconds_ago,
        )
        if self._command_char is not None:
            self._fire_and_forget(
                self._safe_write(_STORED_RECORD_ACK),
                name="x55aa-stored-record-ack",
            )

    async def _safe_write(self, data: bytes) -> None:
        """Write ``data`` to the command characteristic; log failures, never raise."""
        async with self._write_lock:
            client = self._client
            command_char = self._command_char
            if client is None or command_char is None:
                # Expected when the link drops with an ack still queued.
                self._logger.debug(
                    "0x55aa dropping queued write %s; no active session", data.hex()
                )
                return
            try:
                # The command characteristic supports write-with-response only.
                await client.write_gatt_char(command_char, data, response=True)
                self._logger.debug("0x55aa TX payload: %s", data.hex())
            except Exception:
                self._logger.exception(
                    "0x55aa failed to acknowledge stored record (%s)", data.hex()
                )

    def _handle_status(self, frame: Frame, address: str) -> None:
        status = parse_status(frame)
        if status is None:
            self._logger.debug(
                "0x55aa short status frame from %s: %s",
                address,
                frame.payload.hex(),
            )
            return
        if status.display_unit is not None:
            self._display_unit = status.display_unit
        self._logger.debug(
            "0x55aa status from %s: power_on=%s, unit=%s, stored_count=%d, "
            "battery=%d",
            address,
            status.power_on,
            status.display_unit,
            status.stored_count,
            status.battery,
        )

    def _unavailable_callback(self, client: BleakClient) -> None:
        self._buffer.clear()
        self._final_fired = False
        self._warned_statuses.clear()
        self._command_char = None
        super()._unavailable_callback(client)
