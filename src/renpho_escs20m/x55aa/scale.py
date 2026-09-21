"""Renpho ``0x55aa`` variant (LeFu hardware) — GATT connection client.

Both flavors stream over notify characteristic ``0x2A10`` on vendor
service ``0x1A10``, and this client reports weight plus raw impedance
(``resistance_1``) from either; body composition is computed off-scale
via :func:`renpho_escs20m.calculate_body_fat`.

Basic flavor: the scale computes no body composition on-device and takes
no profile. The client subscribes, sends the display-unit command once,
and listens.

Extended flavor (advertised model ids ``0x0030``/``0x0031``): the scale
computes body composition on-device from a guest profile written over
BLE, and releases its final measurement only once such a profile is on
it — so the client writes one at connect (or, in user-detection mode,
as soon as the weight settles). Its on-device figures are still not
reported: they reflect whatever profile the scale held when the
measurement committed, and a later write does not make it recompute
them.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from collections.abc import Callable, Hashable
from typing import Any

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData, BaseBleakScanner

from ..body_metrics import Sex
from ..const import (
    RESISTANCE_1_KEY,
    WEIGHT_KEY,
    X55AA_COMMAND_CHARACTERISTIC_UUID,
    X55AA_NOTIFY_CHARACTERISTIC_UUID,
)
from ..data import BluetoothScanningMode, ScaleData, WeightUnit
from ..scale import GattScale, ScaleSessionError, mask_hex_bytes
from .protocol import (
    ADV_MAC_SLICE,
    CMD_ACK,
    CMD_EXTENDED_MEASUREMENT,
    CMD_EXTENDED_STORED_RECORD,
    CMD_MEASUREMENT,
    CMD_PROFILE_ACK,
    CMD_SETTINGS_ACK,
    CMD_STATUS,
    CMD_STATUS_ALT,
    CMD_STORED_RECORD,
    CMD_USER_PROFILE,
    MANUFACTURER_ID,
    SET_TIME_MODEL_IDS,
    ForbiddenWrite,
    Frame,
    Measurement,
    X55AAProfile,
    X55AAProfileResolver,
    assert_allowed_write,
    build_display_unit_command,
    build_extended_stored_record_ack,
    build_guest_profile_command,
    build_set_time_command,
    is_advertisement,
    is_extended_model,
    iter_frames,
    parse_model_id,
    parse_extended_measurement,
    parse_extended_stored_record,
    parse_measurement,
    parse_status,
    parse_stored_record,
    strip_fragment_header,
)

# Acknowledgement for a stored offline record (``0x95``, payload ``0x01``).
_STORED_RECORD_ACK = bytes.fromhex("55aa950001" "0196")

# Sent when no caller profile is available: weight-only mode, or the resolver
# declined/failed/timed out. Its demographics are never reported — the scale's
# own numbers are discarded in every mode — so they exist only to make a
# well-formed frame. An unremarkable adult keeps the scale's display
# unalarming in the case that still shows something, the resolver fallback.
# Whether the impedance pass runs with it is decided per call site.
_PLACEHOLDER_PROFILE = X55AAProfile(
    sex=Sex.Male, birthday=datetime.date(1985, 7, 1), height_m=1.70
)
# For the profile frame's mandatory last-known-weight field when neither the
# profile nor the session supplies one (connect-time writes). Captured
# profiles carry the subject's own previous reading, and no effect of this
# field on the reading has been observed, so any plausible value serves.
_PLACEHOLDER_LAST_WEIGHT_KG = 70.0

# Client-side stability: the scale gives no wire signal for its own weight
# lock, and two identical frames occur mid-step (e.g. 101.20, 101.20, 102.80,
# 102.80 while stepping on); three has matched the scale's plateau within
# ~0.5 s on every capture.
_STABLE_FRAMES = 3
# The scale holds a settled weight for ~8-20 s with no profile, then powers
# off without a final. The resolver must be well inside that.
_RESOLVER_DEADLINE_SECONDS = 2.0
# A plateau below this is not somebody standing on the scale. Captured after a
# battery pull: a unit reported a steady 1.10 kg with nothing on it from the
# moment of connection, nine frames running — enough to look settled. Without
# this floor that phantom would spend the weigh-in's one profile write, and the
# real weigh-in would commit against whatever came back. The floor gates only
# the profile write; the weight is still latched, so the shutdown fallback
# reports it, and a scale that never reaches the floor never gets a profile and
# so never finalises anyway. Low enough not to gatekeep a light user.
_MIN_WEIGH_IN_KG = 10.0
# Grace period between the power-off status and the weight-only fallback: the
# link stays up for seconds after that status in every capture, so a final
# that is merely late must win over the fallback — one callback, not two.
_FALLBACK_DELAY_SECONDS = 1.0


class Renpho55AAScale(GattScale):
    """Client for scales speaking the ``0x55aa`` protocol.

    Emits one callback per weigh-in, on the scale's final measurement
    frame, with ``{"weight": kg}`` plus ``resistance_1`` (ohms) when the
    bioimpedance pass produced one. Weight on the wire is always
    kilograms regardless of the configured display unit.

    ``model_id`` is the model identifier the scale advertises, and selects
    the flavor: ``0x0030``/``0x0031`` are the extended flavor, whose scales
    compute body composition on-device and will not release a final
    measurement unless a user profile is on the scale; anything else,
    including an identifier this library does not know, is the basic
    flavor. It may be omitted, in which case the flavor is learned from the
    first advertisement — every ``0x55aa`` advertisement carries the
    identifier, and it is read before the client connects. An explicit
    value overrides what the scale advertises (a disagreement is warned
    about once).

    ``profile`` only has an effect on the extended flavor. On that flavor
    the library always puts a guest profile on the scale (never a
    registered slot), and ``profile`` selects which:

    - :class:`~renpho_escs20m.X55AAProfile` instance —
      *fixed-user mode*: the profile is written at connect, exactly as the
      Renpho app does for a known user. It is checked there and then, so
      one the scale could not take raises in the caller's own call stack.
    - :data:`~renpho_escs20m.X55AAProfileResolver` callable
      — *user-detection mode*: nothing is written at connect. The scale
      gives no wire signal for its own weight lock, so the library
      detects it: three consecutive settling frames at the same non-zero
      weight count as settled. It then calls the resolver once with that
      weight, under a 2 s deadline, and writes the profile it returns —
      falling back to the library's placeholder profile if the resolver
      declines, fails, is too slow, or returns a profile the scale cannot
      take. Only ``callable()`` is checked at construction; anything the
      resolver returns is validated when it answers.
    - ``None`` (default) — *weight-only mode*: a placeholder profile is
      written at connect, asking the scale to skip its bioimpedance pass.
      The reading is then weight alone, and so is the scale's own display,
      rather than body composition worked out from a profile belonging to
      nobody. Note this differs from the QN client, whose weight-only mode
      still reports impedance: on this flavor no value is known that
      suppresses the on-device numbers without also suppressing the
      measurement that feeds them. A caller who wants the impedance without body composition
      should pass a profile — its numbers are discarded either way.

    A scale that stays connected for a second weigh-in gets a fresh
    profile write on every subsequent plateau, carrying the settled weight
    in the frame's last-known-weight field: in fixed-user and weight-only
    mode the same profile again, and in user-detection mode the answer to
    a fresh resolver call for that weight.

    The callback never carries more than weight and ``resistance_1``, on
    any flavor and in any mode: the extended scale's on-device BMI and body
    fat are computed from whatever profile it held when the measurement
    committed, and a profile written after that moment does not make it
    recompute them, so they are not reported. Body composition is
    computed off-scale from ``resistance_1`` and the caller's profile —
    see :func:`~renpho_escs20m.body_metrics.calculate_body_fat` and
    :class:`~renpho_escs20m.body_metrics.BodyMetrics`.

    ``display_unit`` is sent to the scale at the start of every session
    (and immediately when changed while connected) with the mode byte
    that leaves the scale's stored zero-current setting untouched. The
    unit the scale announces in its status frames is still tracked and
    reported, since it reflects what the display actually shows.

    When the scale is in zero-current (pregnancy) mode — a setting the
    Renpho app stores on it — the bioimpedance pass is skipped and the
    reading is delivered as weight only.

    On the extended flavor, a scale that announces its power-off without
    ever releasing a final — which a unit with no registered user may do
    even for a guest profile — still reports the settled weight, alone,
    a second after that announcement. A final arriving inside that second
    wins, so the impedance is never traded away for the fallback.

    Stored offline records the scale pushes at connect are logged and
    discarded on both flavors; they are never delivered via the callback.
    ``clear_stored_measurements`` (default ``False``) acknowledges each
    record as it arrives — the basic flavor's ``0x95``, the extended
    flavor's ``0x99`` — which is believed to delete it from the scale.
    On the extended flavor records are released per user slot, to a
    session presenting that slot's profile; a guest session has never
    been observed to receive any.
    """

    def __init__(
        self,
        address: str,
        notification_callback: Callable[[ScaleData], None],
        display_unit: WeightUnit = WeightUnit.KG,
        *,
        model_id: int | None = None,
        profile: X55AAProfile | X55AAProfileResolver | None = None,
        clear_stored_measurements: bool = False,
        scanning_mode: BluetoothScanningMode = BluetoothScanningMode.ACTIVE,
        adapter: str | None = None,
        bleak_scanner_backend: BaseBleakScanner | None = None,
        # Same default as the QN client. The scale announces its shutdown
        # and drops the link ~11 s after the final, then sleeps, so a
        # re-weigh cannot happen inside this window anyway; the gate only
        # spares a futile reconnect if a unit keeps advertising briefly.
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
        self._clear_stored_measurements = clear_stored_measurements
        self._model_id = model_id
        self._extended = is_extended_model(model_id)
        self._fixed_profile: X55AAProfile | None = None
        self._profile_resolver: X55AAProfileResolver | None = None
        if profile is None:
            pass
        elif isinstance(profile, X55AAProfile):
            # Build it once and discard it: an unusable profile (bad
            # algorithm or out-of-range height) then raises here, in the
            # caller's own call stack, rather than inside a background
            # write task at connect time.
            build_guest_profile_command(profile, _PLACEHOLDER_LAST_WEIGHT_KG)
            self._fixed_profile = profile
        elif callable(profile):
            self._profile_resolver = profile
        else:
            raise TypeError(
                "profile must be an X55AAProfile, an async X55AAProfileResolver, "
                f"or None; got {type(profile).__name__}"
            )
        if profile is not None and model_id is not None and not self._extended:
            # Only for an explicit basic identifier: with none given the flavor
            # is still unknown here, and the advertisement may yet say extended.
            self._logger.warning(
                "profile given for 0x55aa model id 0x%04x, which is the basic "
                "flavor (no profile over BLE); it will be ignored",
                model_id,
            )
        # Not reset per session: it records a static disagreement between the
        # caller's model id and the hardware's, not session state.
        self._warned_model_id_mismatch = False
        # True once this weigh-in's profile write is spoken for — either handed
        # to the link, or claimed by the connect-time prelude before it runs.
        # _safe_write never raises, so this records the attempt, not delivery.
        # Reset per weigh-in by ``_rearm_weigh_in``.
        self._profile_sent = False
        # Client-side weight-lock detection (see ``_track_stability``).
        self._stable_weight: float | None = None
        self._stable_zero_current = False
        self._stable_run = 0
        self._last_settling_weight: float | None = None
        self._resolver_task: asyncio.Task | None = None
        self._resolver_deadline_seconds = _RESOLVER_DEADLINE_SECONDS
        # Armed by the power-off status when no final has arrived; see
        # ``_deliver_fallback``.
        self._fallback_task: asyncio.Task | None = None
        self._fallback_delay_seconds = _FALLBACK_DELAY_SECONDS
        # Overridable for tests; the app writes the phone's clock at connect.
        self._clock: Callable[[], datetime.datetime] = (
            lambda: datetime.datetime.now().astimezone()
        )
        self._command_char: BleakGATTCharacteristic | None = None
        # Serializes acks: a burst of stored records would otherwise overlap
        # write-with-response calls, which some BLE stacks reject.
        self._write_lock = asyncio.Lock()
        self._buffer = bytearray()
        self._final_fired = False
        # (weight_kg, resistance) of the final already reported this weigh-in.
        self._final_measurement: tuple[float, int] | None = None
        # True when that final came from the shutdown fallback rather than from
        # the scale: a real final arriving afterwards then means the grace
        # period was too short, not that the firmware repeats itself.
        self._final_from_fallback = False
        # (frame command, status) pairs already warned about this session: an
        # unsupported status on one frame type must not silence the warning for
        # the same status value on another, which may cost a reading.
        self._warned_statuses: set[tuple[int, int]] = set()
        self._zero_current_logged = False
        self._warned_extended_0x14_final = False
        self._warned_unexpected_0x18 = False
        self._warned_stored_record_length = False
        self._warned_shutdown_fallback = False

    @GattScale.display_unit.setter
    def display_unit(self, value: WeightUnit) -> None:
        GattScale.display_unit.fset(self, value)
        if self._client is not None and self._command_char is not None:
            self._send_display_unit()

    def _send_display_unit(self) -> None:
        self._fire_and_forget(
            self._safe_write(
                build_display_unit_command(self.display_unit),
                f"display-unit command ({self.display_unit.value})",
            ),
            name="x55aa-display-unit",
        )

    def _protocol_diagnostics(self) -> dict[str, Any]:
        # The flavor follows from the model identifier, which every
        # advertisement carries (or the caller supplied) — nothing here
        # depends on a session having run.
        # Imported here: ``detection`` imports this package's ``protocol``
        # module, so a module-level import would be circular.
        from ..detection import ScaleProtocol, model_label

        model_id = self._model_id
        return {
            "protocol": ScaleProtocol.X55AA.value,
            "model_code": None if model_id is None else f"0x{model_id:04x}",
            "model_label": model_label(ScaleProtocol.X55AA, model_id),
            "flavor": (
                None
                if model_id is None
                else ("extended" if self._extended else "basic")
            ),
        }

    def _trace_key(self, direction: str, data: bytes) -> Hashable:
        # Live measurement frames repeat with only the weight changing;
        # command + status is what tells a run apart.
        if len(data) >= 6 and data[:2] == b"\x55\xaa" and data[2] == CMD_MEASUREMENT:
            return (data[2], data[5])
        return data

    def _mask_profile_frame(self, data: bytes, text: str) -> str:
        # 5-byte header, 14-byte payload, checksum.
        if len(data) != 20 or data[:2] != b"\x55\xaa" or data[2] != CMD_USER_PROFILE:
            return text
        # Payload [0] packs sex (high nibble) with the slot (low nibble):
        # only the sex goes. Then birth date (4) and height (2). Last weight,
        # flags and trailer stay; the checksum goes, since it would give away
        # the sum of the hidden bytes.
        text = text[:10] + "x" + text[11:]
        return mask_hex_bytes(mask_hex_bytes(text, 6, 12), -1)

    def _advertised_mac(self) -> bytes | None:
        adv = self._last_advertisement
        payload = adv and adv["manufacturer_data"].get(MANUFACTURER_ID)
        if not payload or not is_advertisement(payload, self.address):
            return None
        return payload[ADV_MAC_SLICE]

    async def _handle_advertisement(
        self, ble_device: BLEDevice, advertisement_data: AdvertisementData
    ) -> None:
        """Learn the flavor from the advertisement, then connect as usual."""
        self._learn_model_id(ble_device, advertisement_data)
        await super()._handle_advertisement(ble_device, advertisement_data)

    def _learn_model_id(
        self, ble_device: BLEDevice, advertisement_data: AdvertisementData
    ) -> None:
        """Adopt the advertised model identifier when none was given.

        Every ``0x55aa`` advertisement carries it, so a caller that does not
        know their unit's identifier still gets the right flavor. This runs
        before the base class connects, which is what makes the ordering
        safe: ``_start_scale_session`` reads ``self._extended`` when it
        claims this weigh-in's profile write, and by then the flavor is
        settled. It also runs on every later advertisement, but the value is
        adopted once and never changes afterwards (a learn can only widen
        basic to extended, never the reverse).
        """
        payload = advertisement_data.manufacturer_data.get(MANUFACTURER_ID)
        if payload is None or not is_advertisement(bytes(payload), ble_device.address):
            return
        model_id = parse_model_id(bytes(payload))
        if model_id is None:
            return
        if self._model_id is not None:
            if model_id != self._model_id and not self._warned_model_id_mismatch:
                # The caller's value wins: they may be steering a unit whose
                # identifier this library has not seen, and silently switching
                # flavors under them would be worse than an unheeded warning.
                self._warned_model_id_mismatch = True
                self._logger.warning(
                    "0x55aa scale %s advertises model id 0x%04x, but 0x%04x was "
                    "given; keeping the given one",
                    ble_device.address,
                    model_id,
                    self._model_id,
                )
            return
        self._model_id = model_id
        self._extended = is_extended_model(model_id)
        self._logger.info(
            "0x55aa scale %s advertises model id 0x%04x (%s flavor)",
            ble_device.address,
            model_id,
            "extended" if self._extended else "basic",
        )

    async def _start_scale_session(self, ble_device: BLEDevice) -> None:
        client = self._client
        if client is None:
            return

        # A fallback armed by the previous session is superseded by this one.
        self._cancel_shutdown_fallback()
        self._buffer.clear()
        self._final_fired = False
        self._final_measurement = None
        self._final_from_fallback = False
        self._warned_statuses.clear()
        self._zero_current_logged = False
        self._warned_extended_0x14_final = False
        self._warned_unexpected_0x18 = False
        self._warned_stored_record_length = False
        self._warned_shutdown_fallback = False
        self._profile_sent = False
        self._stable_weight = None
        self._stable_zero_current = False
        self._stable_run = 0
        self._last_settling_weight = None
        self._cancel_resolver()
        self._command_char = None

        await self._populate_device_metadata(client)

        char = client.services.get_characteristic(X55AA_NOTIFY_CHARACTERISTIC_UUID)
        if char is None:
            raise ScaleSessionError(
                "0x55aa notification characteristic (2A10) not found"
            )

        self._command_char = client.services.get_characteristic(
            X55AA_COMMAND_CHARACTERISTIC_UUID
        )
        if self._command_char is None:
            lost = ["the display unit cannot be set"]
            if self._extended:
                # Nothing can be written, so no profile reaches the scale — and
                # without one this flavor never releases a final at all.
                lost.append(
                    "no profile can be sent, so this scale will not release a "
                    "final measurement and only a settled weight can be reported"
                )
            if self._clear_stored_measurements:
                lost.append("stored offline records will not be acknowledged")
            self._logger.warning(
                "0x55aa command characteristic (2A11) not found on %s: %s",
                ble_device.address,
                "; ".join(lost),
            )

        def handler(c: BleakGATTCharacteristic, data: bytearray) -> None:
            self._notification_handler(c, data, ble_device.name, ble_device.address)

        await client.start_notify(char, handler)
        if self._command_char is not None:
            # Claim this weigh-in's profile write before the prelude runs, not
            # inside it: the prelude is a background task, and a plateau can be
            # detected while it is still awaiting the set-time write. Claiming
            # it here keeps ``_on_stable_weight`` from racing a second, and
            # worse, profile frame onto the wire ahead of the prelude's.
            self._profile_sent = self._extended and self._profile_resolver is None
            self._fire_and_forget(
                self._send_session_prelude(), name="x55aa-session-prelude"
            )

    async def _send_session_prelude(self) -> None:
        """Connect-time writes, in the official app's order.

        Extended flavor: set the scale's clock (it presumably derives the
        user's age from the profile's birth date and this clock), then the
        guest profile when one is known up front — the scale only releases a
        final for a profile that was on it before the weigh-in started — then
        the display unit. Basic flavor: the display unit only.
        """
        if self._extended:
            try:
                if self._model_id in SET_TIME_MODEL_IDS:
                    now = self._clock()
                    if now.tzinfo is None:
                        now = now.astimezone()
                    # The wire carries the offset in whole hours, so a
                    # half-hour zone sets the scale's clock up to 45 minutes
                    # off — harmless for an age in whole years.
                    # A custom tzinfo may report no offset; UTC is the only
                    # sane assumption then, and an hour is not load-bearing here.
                    offset = now.utcoffset() or datetime.timedelta(0)
                    await self._safe_write(
                        build_set_time_command(
                            int(now.timestamp()), int(offset.total_seconds())
                        ),
                        "set-time command",
                    )
                if self._profile_resolver is None:
                    # Weight-only mode asks the scale to skip its bioimpedance
                    # pass: with no caller profile there is nothing to compute
                    # from, and it stops the scale displaying body composition
                    # derived from a profile that belongs to nobody.
                    weight_only = self._fixed_profile is None
                    await self._safe_write(
                        build_guest_profile_command(
                            self._fixed_profile or _PLACEHOLDER_PROFILE,
                            _PLACEHOLDER_LAST_WEIGHT_KG,
                            measure_impedance=not weight_only,
                        ),
                        "weight-only placeholder profile"
                        if weight_only
                        else "guest profile",
                    )
            except Exception:
                # Broad on purpose: whatever went wrong, the display-unit write
                # below still has to happen, and a raise here would surface
                # only as an asyncio task warning, not in this library's log.
                self._logger.exception(
                    "0x55aa could not build the connect-time frames for %s; "
                    "continuing with the display unit only",
                    self.address,
                )
        await self._safe_write(
            build_display_unit_command(self.display_unit),
            f"display-unit command ({self.display_unit.value})",
        )

    def _notification_handler(
        self,
        _: BleakGATTCharacteristic,
        payload: bytearray,
        name: str | None,
        address: str,
    ) -> None:
        self._logger.debug("0x55aa RX payload from %s: %s", address, payload.hex())
        # Raw notifications, so an extended unit's fragment headers show.
        self._trace_frame("rx", payload)
        # The extended flavor splits a frame longer than one notification into
        # chunks behind a 3-byte header; a plain frame starts with the magic
        # and passes through untouched. The basic flavor has no frame long
        # enough to fragment, so its bytes are never inspected for a header.
        raw = bytes(payload)
        self._buffer.extend(strip_fragment_header(raw) if self._extended else raw)

        for frame in iter_frames(self._buffer):
            if frame.cmd == CMD_MEASUREMENT:
                self._handle_measurement(frame, name, address)
            elif frame.cmd == CMD_EXTENDED_MEASUREMENT:
                self._handle_extended_measurement(frame, name, address)
            elif frame.cmd == CMD_STORED_RECORD:
                self._handle_stored_record(frame, address)
            elif frame.cmd == CMD_EXTENDED_STORED_RECORD:
                self._handle_extended_stored_record(frame, address)
            elif frame.cmd == CMD_STATUS:
                self._handle_status(frame, name, address)
            elif frame.cmd in (
                CMD_ACK,
                CMD_STATUS_ALT,
                CMD_PROFILE_ACK,
                CMD_SETTINGS_ACK,
            ):
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

        if self._extended:
            self._handle_extended_progress_frame(measurement, frame, address)
            return

        if measurement.is_settling:
            if self._final_fired:
                # A settling phase after a final is a new weigh-in on a scale
                # that stayed connected.
                self._rearm_weigh_in()
            self._logger.debug(
                "0x55aa settling frame from %s: weight=%.2f kg%s",
                address,
                measurement.weight_kg,
                " (zero-current mode)" if measurement.zero_current else "",
            )
            return

        if not measurement.is_final:
            # Unsupported status: skip the reading, and surface it loudly
            # (once per status per connection) rather than misparse it.
            # Some scales have app-settable modes (e.g. for weighing during
            # pregnancy or holding a baby) that this library deliberately
            # does not support.
            self._warn_unsupported_status(frame, measurement.status, address)
            return

        if self._dedupe_final(
            measurement.weight_kg, measurement.resistance, address, frame.payload.hex()
        ):
            return
        self._final_from_fallback = False
        self._deliver_final(
            measurement.weight_kg,
            measurement.resistance,
            measurement.zero_current,
            name,
            address,
        )

    def _warn_unsupported_status(self, frame: Frame, status: int, address: str) -> None:
        """Surface a status this library will not parse, once per frame type.

        Keyed by (frame, status): the same status value means different things
        on a 0x14 and an 0x18, and silencing the settling frame's warning would
        hide the final's, which is the one that costs a reading.
        """
        if (frame.cmd, status) in self._warned_statuses:
            return
        self._warned_statuses.add((frame.cmd, status))
        self._logger.warning(
            "0x55aa unsupported measurement status 0x%02x on frame 0x%02x from "
            "%s; reading skipped (possibly a scale mode this library does not "
            "support) — please report this on the issue tracker: %s",
            status,
            frame.cmd,
            address,
            frame.payload.hex(),
        )

    def _rearm_weigh_in(self) -> None:
        """Arm the client for a new weigh-in on an already-connected scale."""
        self._cancel_shutdown_fallback()
        self._final_fired = False
        self._final_measurement = None
        self._stable_weight = None
        self._stable_zero_current = False
        self._stable_run = 0
        self._last_settling_weight = None
        self._final_from_fallback = False
        # A resolver still working on the previous weigh-in is answering a
        # question nobody is asking any more: its profile would land carrying a
        # stale weight, and the new plateau starts its own resolution.
        self._cancel_resolver()
        # Send the profile again for the next weigh-in: whether the scale keeps
        # a guest profile across weigh-ins on one link is unverified, and a
        # repeat write is harmless.
        self._profile_sent = False

    def _dedupe_final(
        self, weight_kg: float, resistance: int, address: str, raw: str
    ) -> bool:
        """True if a final for this weigh-in was already reported (and log why)."""
        if not self._final_fired:
            return False
        first = self._final_measurement
        if self._final_from_fallback:
            # Not a repeat at all: the scale's own final, later than the grace
            # period allowed for. Checked before the contents are compared,
            # because a final with no impedance is byte-identical to what the
            # fallback reported. The reading is already out (weight-only), so
            # this frame is still ignored — but the grace wants lengthening,
            # which only a report can establish. Cleared so that a hardware
            # repeat of this same final logs as the duplicate it is.
            self._final_from_fallback = False
            self._logger.warning(
                "0x55aa scale %s sent a final (%.2f kg, %d ohm) after the "
                "shutdown fallback had already reported %.2f kg; the "
                "fallback grace may be too short for this unit — please "
                "report this on the issue tracker",
                address,
                weight_kg,
                resistance,
                first[0] if first is not None else weight_kg,
            )
        elif first is not None and first != (weight_kg, resistance):
            # Every repeated final captured so far has been byte-identical.
            # One that differs is worth a report: a firmware that finalizes
            # before its impedance pass would lose the resistance here.
            self._logger.warning(
                "0x55aa repeated final frame from %s differs from the one "
                "already reported (%.2f kg, %d ohm -> %.2f kg, %d ohm); "
                "ignoring it — please report this on the issue tracker: %s",
                address,
                first[0],
                first[1],
                weight_kg,
                resistance,
                raw,
            )
        else:
            self._logger.debug(
                "0x55aa duplicate final frame from %s; already handled", address
            )
        return True

    def _deliver_final(
        self,
        weight_kg: float,
        resistance: int,
        zero_current: bool,
        name: str | None,
        address: str,
    ) -> None:
        self._final_fired = True
        self._final_measurement = (weight_kg, resistance)
        measurements: dict[str, str | float | None] = {WEIGHT_KEY: weight_kg}
        if zero_current:
            if not self._zero_current_logged:
                self._zero_current_logged = True
                self._logger.info(
                    "0x55aa scale %s is in zero-current (pregnancy) mode, a "
                    "setting stored on the scale by the Renpho app; reporting "
                    "weight only",
                    address,
                )
        elif resistance:
            measurements[RESISTANCE_1_KEY] = resistance
        self._logger.debug(
            "0x55aa final measurement from %s: weight=%.2f kg, "
            "resistance=%s. Firing callback.",
            address,
            weight_kg,
            resistance or None,
        )
        self._notification_callback(
            ScaleData(
                name=name or "",
                address=address,
                display_unit=self.display_unit,
                measurements=measurements,
            )
        )

    def _handle_extended_progress_frame(
        self, measurement: Measurement, frame: Frame, address: str
    ) -> None:
        """Classify a 0x14 frame on the extended flavor, which never finalises.

        That flavor finalises on 0x18, so a 0x14 carrying a final status has
        never been observed and is not treated as a reading; anything else is
        a settling frame or an unsupported status.
        """
        if measurement.is_final:
            if not self._warned_extended_0x14_final:
                self._warned_extended_0x14_final = True
                self._logger.warning(
                    "0x55aa extended-flavor scale %s sent a 0x14 frame with a "
                    "final status; this flavor finalises on 0x18, so it is not "
                    "reported — please report this on the issue tracker: %s",
                    address,
                    frame.payload.hex(),
                )
            return
        if not measurement.is_settling:
            self._warn_unsupported_status(frame, measurement.status, address)
            return
        if (
            self._final_fired
            and self._final_measurement is not None
            and measurement.weight_kg != self._final_measurement[0]
        ):
            # A settling frame at a new weight after a final is someone
            # stepping on again; the scale also echoes the final's weight once
            # at power-off, which is not.
            self._rearm_weigh_in()
        elif self._cancel_shutdown_fallback():
            # The scale is demonstrably still weighing, so its power-off status
            # was premature (or we misread it): let it finalise properly rather
            # than trade the impedance away for a weight-only fallback.
            self._logger.debug(
                "0x55aa settling frame from %s after a power-off status; "
                "shutdown fallback cancelled",
                address,
            )
        self._logger.debug(
            "0x55aa settling frame from %s: weight=%.2f kg%s",
            address,
            measurement.weight_kg,
            " (zero-current mode)" if measurement.zero_current else "",
        )
        self._track_stability(measurement.weight_kg, measurement.zero_current, address)

    def _track_stability(
        self, weight_kg: float, zero_current: bool, address: str
    ) -> None:
        if weight_kg <= 0:
            self._last_settling_weight = None
            self._stable_run = 0
            return
        if weight_kg == self._last_settling_weight:
            self._stable_run += 1
        else:
            self._last_settling_weight = weight_kg
            self._stable_run = 1
        # A later plateau supersedes an earlier one for as long as no final
        # has fired: the scale may never finalise, and the shutdown fallback
        # must report the weight that was actually on it last. Once a final
        # is out, the power-off echo of its weight must not re-latch.
        if self._stable_run == _STABLE_FRAMES and not self._final_fired:
            self._stable_weight = weight_kg
            # Latched with the weight: the shutdown fallback reports this
            # weigh-in's mode, and no frame carries it once the scale is off.
            self._stable_zero_current = zero_current
            self._logger.debug(
                "0x55aa weight settled at %.2f kg on %s", weight_kg, address
            )
            if weight_kg < _MIN_WEIGH_IN_KG:
                self._logger.debug(
                    "0x55aa settled weight %.2f kg on %s is below %.1f kg; treating "
                    "it as a resting offset rather than a weigh-in, and not "
                    "spending this weigh-in's profile write on it",
                    weight_kg,
                    address,
                    _MIN_WEIGH_IN_KG,
                )
                return
            self._on_stable_weight(weight_kg, address)

    def _on_stable_weight(self, weight_kg: float, address: str) -> None:
        """Write the one profile this weigh-in gets, if none has gone out yet.

        Without a profile on the scale the final never comes; with a profile
        written after the scale's own commit the final still comes, computed
        from whatever the scale held, and the resistance is intact — so a late
        write costs display accuracy, never the reading.
        """
        if self._profile_sent or self._command_char is None:
            return
        self._profile_sent = True
        if self._profile_resolver is not None:
            self._resolver_task = self._spawn(
                self._resolve_and_send_profile(weight_kg, address, self._client),
                name="x55aa-resolve-profile",
            )
            return
        weight_only = self._fixed_profile is None
        profile = self._fixed_profile or _PLACEHOLDER_PROFILE
        try:
            frame = build_guest_profile_command(
                profile, weight_kg, measure_impedance=not weight_only
            )
        except ValueError:
            # Unreachable in practice: a fixed profile is validated at
            # construction and the placeholder is a library constant. Caught
            # anyway so a future edit cannot take down the notification handler.
            self._logger.exception(
                "0x55aa could not encode the guest profile for %s", address
            )
            return
        self._fire_and_forget(
            self._safe_write(
                frame,
                "weight-only placeholder profile" if weight_only else "guest profile",
            ),
            name="x55aa-guest-profile",
        )

    async def _resolve_and_send_profile(
        self, weight_kg: float, address: str, session_client: BleakClient | None
    ) -> None:
        resolver = self._profile_resolver
        if resolver is None:
            return
        profile: X55AAProfile | None = None
        try:
            profile = await asyncio.wait_for(
                resolver(weight_kg), self._resolver_deadline_seconds
            )
        except asyncio.CancelledError:
            self._logger.debug(
                "0x55aa profile resolver cancelled for %s (session ended)", address
            )
            self._trace_event("profile resolver cancelled")
            raise
        except TimeoutError:
            self._logger.warning(
                "0x55aa profile resolver did not answer within %.1f s for %s at "
                "weight=%.2f; sending the placeholder profile so the reading is not lost",
                self._resolver_deadline_seconds,
                address,
                weight_kg,
            )
            self._trace_event("profile resolver timed out")
        except Exception:
            self._logger.exception(
                "0x55aa profile resolver raised for %s at weight=%.2f; sending the "
                "placeholder profile",
                address,
                weight_kg,
            )
            self._trace_event("profile resolver raised")
        else:
            if profile is None:
                self._trace_event("profile resolver returned none")
        if profile is not None and not isinstance(profile, X55AAProfile):
            self._logger.error(
                "0x55aa profile resolver for %s returned %s, not an X55AAProfile; "
                "sending the placeholder profile",
                address,
                type(profile).__name__,
            )
            self._trace_event("profile resolver returned a non-profile")
            profile = None
        if profile is None:
            self._logger.debug(
                "0x55aa no resolved profile for %s at weight=%.2f; placeholder profile "
                "(weight and impedance are still delivered)",
                address,
                weight_kg,
            )
            profile, what = _PLACEHOLDER_PROFILE, "placeholder guest profile"
        else:
            what = "resolved guest profile"
        try:
            frame = build_guest_profile_command(profile, weight_kg)
        except Exception:
            # Broad on purpose: the dataclass does not check its field types, so
            # a caller's profile can carry anything. Enumerating the exceptions
            # that produces is a losing game, and the cost of missing one is a
            # dead background task and a weigh-in that never finalises.
            self._logger.exception(
                "0x55aa could not encode the resolved profile for %s; sending the "
                "placeholder profile",
                address,
            )
            # This placeholder keeps the impedance pass on, unlike weight-only
            # mode: the reading may still be assigned to a user afterwards, and
            # the resistance is what makes that possible.
            frame = build_guest_profile_command(_PLACEHOLDER_PROFILE, weight_kg)
            what = "placeholder guest profile"
        if self._client is not session_client:
            # The link dropped and came back while the resolver was thinking:
            # the new session runs its own prelude and detects its own plateau,
            # so this answer belongs to a weigh-in that is over.
            self._logger.debug(
                "0x55aa session changed while resolving the profile for %s; "
                "dropping the resolved profile",
                address,
            )
            return
        await self._safe_write(frame, what)

    def _handle_extended_measurement(
        self, frame: Frame, name: str | None, address: str
    ) -> None:
        measurement = parse_extended_measurement(frame)
        if measurement is None:
            self._logger.debug(
                "0x55aa short 0x18 frame from %s: %s", address, frame.payload.hex()
            )
            return
        if not self._extended and not self._warned_unexpected_0x18:
            # Either the advertised model id was missed or this is an
            # extended model the library does not know yet. The weight on
            # the frame is real either way, so it is still reported.
            self._warned_unexpected_0x18 = True
            self._logger.warning(
                "0x55aa scale %s (model id %s) sent an extended-flavor frame "
                "but is not registered as extended; its finals are still "
                "reported — please report this on the issue tracker",
                address,
                "unknown" if self._model_id is None else f"0x{self._model_id:04x}",
            )
        if not measurement.is_final:
            self._warn_unsupported_status(frame, measurement.status, address)
            return
        if self._cancel_shutdown_fallback():
            self._logger.debug(
                "0x55aa final arrived from %s; shutdown fallback cancelled", address
            )
        if self._dedupe_final(
            measurement.weight_kg, measurement.resistance, address, frame.payload.hex()
        ):
            return
        self._logger.debug(
            "0x55aa 0x18 final from %s: slot echo %d, on-device BMI %.1f / "
            "body fat %.1f%% (not reported)",
            address,
            measurement.slot,
            measurement.bmi,
            measurement.body_fat,
        )
        self._final_from_fallback = False
        self._deliver_final(
            measurement.weight_kg,
            measurement.resistance,
            measurement.zero_current,
            name,
            address,
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
            "weight=%.2f kg, resistance=%d, measured %d seconds ago%s",
            address,
            record.weight_kg,
            record.resistance,
            record.seconds_ago,
            f", mode flag={record.mode_flag}" if record.mode_flag is not None else "",
        )
        if self._clear_stored_measurements and self._command_char is not None:
            self._fire_and_forget(
                self._safe_write(_STORED_RECORD_ACK, "stored-record acknowledgement"),
                name="x55aa-stored-record-ack",
            )

    def _handle_extended_stored_record(self, frame: Frame, address: str) -> None:
        record = parse_extended_stored_record(frame)
        if record is None:
            if not self._warned_stored_record_length:
                self._warned_stored_record_length = True
                self._logger.warning(
                    "0x55aa unrecognised stored-record length (%d-byte payload) from %s; "
                    "record skipped — please report this on the issue tracker: %s",
                    len(frame.payload),
                    address,
                    frame.payload.hex(),
                )
            return
        # Historical reading, assigned by the scale to a user slot (possibly
        # another person). Never reported as a live measurement. Acknowledged
        # only when opted in: the ack is believed to delete the record.
        self._logger.debug(
            "0x55aa stored offline record from %s (discarded): weight=%.2f kg, "
            "resistance=%d, measured %d seconds ago, slot %d%s",
            address,
            record.weight_kg,
            record.resistance,
            record.seconds_ago,
            record.slot,
            ", zero-current mode" if record.zero_current else "",
        )
        if self._clear_stored_measurements and self._command_char is not None:
            self._fire_and_forget(
                self._safe_write(
                    build_extended_stored_record_ack(), "stored-record acknowledgement"
                ),
                name="x55aa-stored-record-ack",
            )

    async def _safe_write(self, data: bytes, what: str) -> None:
        """Write ``data`` to the command characteristic; log failures, never raise."""
        try:
            assert_allowed_write(data)
        except ForbiddenWrite as exc:
            self._logger.error(
                "0x55aa refused to write %s (%s): %s — this is a library bug, "
                "please report it",
                what,
                data.hex(),
                exc,
            )
            self._trace_event(f"refused forbidden write: {what}")
            return
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
                self._logger.debug("0x55aa TX %s: %s", what, data.hex())
                # ``what`` tells a resolved profile from the placeholder — the
                # resolver's outcome, which the frame alone does not show.
                self._trace_frame("tx", data, note=what)
            except Exception:
                self._logger.exception(
                    "0x55aa failed to write %s (%s)", what, data.hex()
                )

    def _handle_status(self, frame: Frame, name: str | None, address: str) -> None:
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
            "0x55aa status from %s: power_on=%s, unit=%s, stored_count=%d, byte4=%d",
            address,
            status.power_on,
            status.display_unit,
            status.stored_count,
            status.byte4,
        )
        if (
            self._extended
            and not status.power_on
            and not self._final_fired
            and self._stable_weight is not None
            and (self._fallback_task is None or self._fallback_task.done())
        ):
            weight = self._stable_weight
            zero_current = self._stable_zero_current
            if not self._warned_shutdown_fallback:
                self._warned_shutdown_fallback = True
                self._logger.warning(
                    "0x55aa scale %s powered off without a final; reporting the "
                    "settled weight (%.2f kg) without impedance — please report "
                    "this on the issue tracker",
                    address,
                    weight,
                )
            else:
                self._logger.info(
                    "0x55aa scale %s powered off without a final again; reporting "
                    "the settled weight (%.2f kg) without impedance",
                    address,
                    weight,
                )
            self._fallback_task = self._spawn(
                self._deliver_fallback(weight, zero_current, name, address),
                name="x55aa-shutdown-fallback",
            )

    async def _deliver_fallback(
        self, weight_kg: float, zero_current: bool, name: str | None, address: str
    ) -> None:
        """Report a settled weight the scale never finalised.

        Deferred rather than immediate: the link outlives the power-off
        status by seconds, so a final that is merely late still arrives and
        cancels this — it carries the impedance this fallback cannot.
        """
        await asyncio.sleep(self._fallback_delay_seconds)
        if self._final_fired:
            return
        # The weigh-in is over: a resolver still thinking would write its
        # profile to a scale that has already powered off.
        self._cancel_resolver()
        self._final_from_fallback = True
        try:
            self._deliver_final(weight_kg, 0, zero_current, name, address)
        except Exception:
            # Every other delivery runs inside the notification handler, where
            # bleak logs a raising callback; this one is our own task, so an
            # unhandled exception would surface only as a task warning.
            self._logger.exception(
                "0x55aa notification callback raised on the shutdown fallback "
                "for %s",
                address,
            )

    def _cancel_resolver(self) -> None:
        """Cancel a profile resolution still in flight, if there is one."""
        task = self._resolver_task
        self._resolver_task = None
        if task is not None and not task.done():
            task.cancel()

    def _cancel_shutdown_fallback(self) -> bool:
        """Cancel a pending shutdown fallback; True if one was pending."""
        task = self._fallback_task
        self._fallback_task = None
        if task is not None and not task.done():
            task.cancel()
            return True
        return False

    def _unavailable_callback(self, client: BleakClient) -> None:
        self._buffer.clear()
        self._final_fired = False
        self._final_measurement = None
        self._final_from_fallback = False
        self._warned_statuses.clear()
        self._zero_current_logged = False
        self._warned_extended_0x14_final = False
        self._warned_unexpected_0x18 = False
        self._warned_stored_record_length = False
        self._warned_shutdown_fallback = False
        self._profile_sent = False
        self._stable_weight = None
        self._stable_zero_current = False
        self._stable_run = 0
        self._last_settling_weight = None
        self._cancel_resolver()
        self._command_char = None
        super()._unavailable_callback(client)
