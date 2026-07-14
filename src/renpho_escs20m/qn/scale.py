"""Renpho ES-CS20M (QN-series) BLE scale — GATT connection variant."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import BaseBleakScanner

from ..const import (
    BODY_FAT_KEY,
    COMMAND_CHARACTERISTIC_UUID,
    FFE0_ALT_COMMAND_CHARACTERISTIC_UUID,
    FFE0_COMMAND_CHARACTERISTIC_UUID,
    FFE0_INDICATE_CHARACTERISTIC_UUID,
    FFE0_NOTIFY_CHARACTERISTIC_UUID,
    NOTIFY_CHARACTERISTIC_UUID,
    RESISTANCE_1_KEY,
    RESISTANCE_2_KEY,
    WEIGHT_KEY,
)
from ..data import BluetoothScanningMode, ScaleData, WeightUnit
from ..scale import GattScale
from .protocol import (
    Profile,
    ProfileResolver,
    _BASIC_STATUS_BIA_RUNNING,
    _BASIC_STATUS_FINAL,
    _BASIC_STATUS_SETTLING,
    _BOOTSTRAP_PROFILE,
    _DEFAULT_VENDOR_BYTE,
    _GUEST_USER_ID,
    _LEN_BASIC_MEASUREMENT,
    _LEN_EXTENDED_MEASUREMENT,
    _LEN_EXTENDED_PRE_MEASUREMENT,
    _LEN_STORED_MEASUREMENT,
    _MEASUREMENT_STATUS_STABLE,
    _MEASUREMENT_STATUS_STABLE_WITH_METRICS,
    _MEASUREMENT_STATUS_UNSTABLE,
    _OP_MEAS_INIT_REQUEST,
    _OP_MEASUREMENT,
    _OP_PRE_MEASUREMENT,
    _OP_PROFILE_ACK,
    _OP_STORED_MEASUREMENT,
    _OP_UNIT_REQUEST,
    _build_command_for_profile,
    build_end_measurement_command,
    build_extended_stored_measurement_query,
    build_measurement_initiation_command,
    build_stored_measurement_query,
    build_unit_update_command,
    parse_basic_measurement,
    parse_extended_measurement,
    parse_extended_stored_measurement,
    parse_stored_measurement,
)

_STATE_UNIT_SET = 1
_STATE_MEASUREMENT_INIT = 2
_STATE_USER_PROFILE = 4
_STATE_PROFILE_RESOLVING = 8
# Set once the basic-flavor final (status 0x01) measurement frame has been
# handled, so a repeated final frame does not fire the callback twice.
_STATE_BASIC_FINAL = 16
# Set once the stored-measurement query has been sent this session.
_STATE_STORED_QUERY = 32

# On the FFE0 transport command writes are split across two characteristics
# (capture-verified): set-time (0x20) and the stored-measurement query
# (0x22) go to FFE4, everything else to FFE3. (The FFF0 transport has a
# single shared command characteristic, FFF2.)
_FFE0_ALT_COMMAND_OPCODES = frozenset({0x20, 0x22})


class RenphoQNScale(GattScale):
    """
    Renpho ES-CS20M BLE scale.

    Manages the BLE connection lifecycle and handles the handshake/measurement
    flow for the ES-CS20M variants using the QN protocol - ESCS20MN (basic flavor) and ESCS20MA2 (extended flavor).

    The scale is always driven in *guest mode*: it does not allocate a
    persistent slot or store the reading. This simplifies the protocol handshake,
    and prevents this library from clobbering or evicting any user the
    official Renpho app may have registered on the same scale.

    The ``profile`` argument only has an effect on the **extended
    flavor** (HVIN ESCS20MA2), where the scale computes body fat
    on-device from a profile sent over BLE. That flavor will not start
    a measurement without a profile reply, so the library always sends
    one; ``profile`` selects which:

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
      sent and never overridden, so the scale computes and displays no
      body fat. "Weight-only" describes that outcome, not a filter on
      the reported data — the scale still performs its impedance pass
      and the final reading still carries raw impedance; only body fat
      is absent.

    The **basic flavor** (HVIN ESCS20MN) takes no profile over BLE, so
    ``profile`` is ignored there: nothing is sent to the scale and a
    resolver is never invoked. Its readings always carry weight plus
    raw impedance regardless of mode, and body fat is computed
    off-scale by the caller — see
    :func:`~renpho_escs20m.body_metrics.calculate_body_fat` and
    :class:`~renpho_escs20m.body_metrics.BodyMetrics`.

    ``clear_stored_measurements`` (default ``False``) drains the scale's
    store of offline measurements — readings taken while nothing was
    connected — once per session. Delivering a stored record deletes it
    from the scale (there is no separate delete command), so enabling
    this hides those readings from any other client: leave it off if
    the official Renpho app should still import them. Drained records
    are logged at debug level and discarded for now. Each flavor takes its own query form (sent at
    the point in the handshake where that flavor answers it).
    """

    def __init__(
        self,
        address: str,
        notification_callback: Callable[[ScaleData], None],
        display_unit: WeightUnit = WeightUnit.KG,
        *,
        profile: Profile | ProfileResolver | None = None,
        clear_stored_measurements: bool = False,
        scanning_mode: BluetoothScanningMode = BluetoothScanningMode.ACTIVE,
        adapter: str | None = None,
        bleak_scanner_backend: BaseBleakScanner | None = None,
        cooldown_seconds: int = 5,
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

        self._state_mask = 0
        self._clear_stored_measurements = clear_stored_measurements

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

        # Per-device frame byte (offset 2), detected per-session from the wire
        # and echoed back in our replies; defaults to renpho's 0xFF.
        self._vendor_byte: int = _DEFAULT_VENDOR_BYTE

        # True once this session's stored-measurement query used the
        # extended form — extended 0x23 records shift their fields by one
        # byte, so the record handler must pick the matching parser.
        self._stored_records_extended = False

    def _unavailable_callback(self, client) -> None:
        super()._unavailable_callback(client)
        if self._resolver_task is not None and not self._resolver_task.done():
            self._resolver_task.cancel()
        self._resolver_task = None

    async def _start_scale_session(self, ble_device: BLEDevice) -> None:
        client = self._client
        if client is None:
            return
        self._state_mask = 0
        self._vendor_byte = _DEFAULT_VENDOR_BYTE
        self._stored_records_extended = False
        try:
            self._logger.debug(
                "ES-CS20M starting session for device %s (%s)",
                ble_device.name,
                ble_device.address,
            )
            await self._populate_device_metadata(client)

            def handler(char: BleakGATTCharacteristic, data: bytearray) -> None:
                self._notification_handler(
                    char, data, ble_device.name, ble_device.address
                )

            # Prefer the FFF0 transport (renpho ES-CS20M), then fall back to
            # the FFE0 transport (e.g. Arboleaf CS20M).
            if weight_char := client.services.get_characteristic(
                NOTIFY_CHARACTERISTIC_UUID
            ):
                await client.start_notify(weight_char, handler)
            elif weight_char := client.services.get_characteristic(
                FFE0_NOTIFY_CHARACTERISTIC_UUID
            ):
                await client.start_notify(weight_char, handler)
                # The FFE0 transport delivers the pre-measurement and
                # stored-record frames as indications on FFE2, not on FFE1.
                if indicate_char := client.services.get_characteristic(
                    FFE0_INDICATE_CHARACTERISTIC_UUID
                ):
                    await client.start_notify(indicate_char, handler)
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
                _OP_STORED_MEASUREMENT,
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
        elif opcode == _OP_STORED_MEASUREMENT:
            self._handle_stored_measurement(payload, address)
        elif opcode == _OP_PROFILE_ACK:
            # Extended-flavor ack of our user-profile command; the only
            # follow-up is the optional stored-measurement query, which the
            # extended flavor answers after a *successful* ack (byte 4
            # 0x01) — a failed ack means no session user, so there is no
            # store to read.
            self._logger.debug("ES-CS20M user profile acknowledged by %s", address)
            if len(payload) >= 5 and payload[4] == 0x01:
                self._stored_records_extended = True
                self._query_stored_measurements(
                    build_extended_stored_measurement_query(self._vendor_byte), address
                )
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

    def _query_stored_measurements(self, command: bytearray, address: str) -> None:
        """Send a stored-measurement query once per session (if enabled).

        Delivery of the returned records deletes them from the scale's
        store, which is exactly the "clear" the option promises. Each
        flavor takes its own query form, so the caller passes the built
        command: ``22 04`` on basic (after its pre-measurement frame),
        ``22 06 .. 00 01`` on extended (after the profile ack) — matching
        where each flavor was observed answering it.
        """
        if (
            not self._clear_stored_measurements
            or self._state_mask & _STATE_STORED_QUERY
        ):
            return
        self._state_mask |= _STATE_STORED_QUERY
        self._logger.debug(
            "ES-CS20M querying stored offline measurements on %s to clear them.",
            address,
        )
        self._fire_and_forget(
            self._safe_write(command),
            name="escs20m-stored-query",
        )

    def _handle_stored_measurement(self, payload: bytearray, address: str) -> None:
        """Handle a stored offline-measurement record.

        Sent by the scale only in response to our stored-measurement
        query, one frame per offline reading (``count=0`` when the store
        is empty). Delivery deletes the record from the scale, so simply
        receiving and discarding it here is what clears the store. Never
        fires the measurement callback.
        """
        if len(payload) < _LEN_STORED_MEASUREMENT:
            self._logger.warning(
                "ES-CS20M stored-measurement frame from %s has unexpected "
                "length; ignoring: %s",
                address,
                payload.hex(),
            )
            return
        if payload[3] == 0:
            self._logger.debug(
                "ES-CS20M stored-measurement store on %s is empty.", address
            )
            return
        if self._stored_records_extended:
            ext = parse_extended_stored_measurement(payload)
            self._logger.debug(
                "ES-CS20M discarding stored offline measurement %d/%d from %s: "
                "weight=%.2f kg, r1=%d, r2=%d, body_fat=%s, user_index=0x%02x, "
                "timestamp=%d (delivery clears it from the scale).",
                ext.index,
                ext.count,
                address,
                ext.weight_kg,
                ext.resistance_1,
                ext.resistance_2,
                ext.body_fat,
                ext.user_index,
                ext.timestamp,
            )
            return
        frame = parse_stored_measurement(payload)
        self._logger.debug(
            "ES-CS20M discarding stored offline measurement %d/%d from %s: "
            "weight=%.2f kg, r1=%d, r2=%d, timestamp=%d (delivery clears it "
            "from the scale).",
            frame.index,
            frame.count,
            address,
            frame.weight_kg,
            frame.resistance_1,
            frame.resistance_2,
            frame.timestamp,
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
        self._query_stored_measurements(
            build_stored_measurement_query(self._vendor_byte), address
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

    def _resolve_command_char(self, opcode: int) -> BleakGATTCharacteristic | None:
        """Pick the command characteristic for ``opcode`` on whichever GATT
        transport the connected scale exposes.

        FFF0 transport: everything goes to the single FFF2 characteristic.
        FFE0 transport: the :data:`_FFE0_ALT_COMMAND_OPCODES` commands go
        to FFE4, the rest to FFE3 — the split observed on the wire. The
        routing is strict: a scale missing the required characteristic is
        an unknown GATT layout, surfaced by the caller's skipped-write
        warning rather than papered over with a guessed substitute.
        """
        services = self._client.services
        if command_char := services.get_characteristic(COMMAND_CHARACTERISTIC_UUID):
            return command_char
        if opcode in _FFE0_ALT_COMMAND_OPCODES:
            return services.get_characteristic(FFE0_ALT_COMMAND_CHARACTERISTIC_UUID)
        return services.get_characteristic(FFE0_COMMAND_CHARACTERISTIC_UUID)

    async def _safe_write(self, data: bytearray) -> None:
        if not self._client:
            self._logger.warning("ES-CS20M cannot send command; no active client")
            return
        if not (command_char := self._resolve_command_char(data[0])):
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
