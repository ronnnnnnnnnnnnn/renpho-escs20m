"""Transport base classes shared by every Renpho scale variant.

- :class:`RenphoScale` — transport-agnostic: BLE scanner setup + lifecycle,
  address filtering, and the notification callback.
- :class:`GattScale` — variants that deliver measurements over a GATT
  connection (QN, 0x55aa).
- :class:`AdvertisementScale` — variants that broadcast measurements in their
  BLE advertisements with no connection (0xaabb).

Protocol-specific handling lives in the per-protocol subpackages
(``qn/``, ``x55aa/``, ``xaabb/``).
"""

from __future__ import annotations

import abc
import asyncio
import logging
import platform
import time
from collections import deque
from collections.abc import Callable, Coroutine, Hashable
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


class ScaleSessionError(Exception):
    """Post-connection session setup failed in a way worth retrying.

    Raised by :meth:`GattScale._start_scale_session` when the connection's
    GATT database cannot support a session (e.g. service discovery
    transiently exposed no notify characteristic). The base class responds
    by disconnecting and retrying on the next advertisement, bounded by
    ``GattScale._MAX_CONSECUTIVE_SETUP_FAILURES``.
    """


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


# Entries kept in the session trace (see RenphoScale._trace_frame). Runs of
# near-identical frames collapse into one entry, so this spans several
# weigh-ins rather than the tail of one.
_TRACE_MAX_ENTRIES = 120


def _parse_mac(address: str) -> bytes | None:
    """Forward-order bytes of a colon-separated MAC, or None if not a MAC
    (e.g. a macOS CoreBluetooth UUID)."""
    octets = address.split(":")
    if len(octets) != 6:
        return None
    try:
        return bytes(int(o, 16) for o in octets)
    except ValueError:
        return None


def mask_mac_echo(data: bytes, address: str, mac: bytes | None = None) -> str:
    """Hex of ``data`` with the device-specific half of any echo of the
    scale's MAC masked as ``xx``.

    The scales echo their own MAC inside advertisements and some frames, in
    forward or reversed byte order. The OUI half is kept — models are told
    apart by it — and the unique half is masked so the result can be shared.
    The MAC is taken from ``address``; where the platform does not expose it
    there (macOS hands out a UUID), pass the six bytes as ``mac`` instead.
    With neither, ``data`` comes back unmasked.
    """
    data = bytes(data)
    text = data.hex()
    mac = _parse_mac(address) or mac
    if mac is None or len(mac) != 6:
        return text
    # (needle, offset of the unique three bytes within it)
    for needle, unique_at in ((mac, 3), (mac[::-1], 0)):
        start = data.find(needle)
        while start != -1:
            lo = (start + unique_at) * 2
            text = text[:lo] + "xxxxxx" + text[lo + 6 :]
            start = data.find(needle, start + 1)
    return text


def mask_hex_bytes(text: str, start: int, stop: int | None = None) -> str:
    """``text`` (hex) with bytes ``start:stop`` replaced by ``xx``.

    Indices are byte offsets and may be negative, as in a slice.
    """
    n = len(text) // 2
    lo, hi, _ = slice(start, stop).indices(n)
    return text[: lo * 2] + "x" * (max(hi - lo, 0) * 2) + text[hi * 2 :]


class RenphoScale(abc.ABC):
    """
    Abstract base for every Renpho scale variant.

    Handles the parts common to every model regardless of how measurements are
    obtained: BLE scanner setup and lifecycle, address filtering, the
    notification callback, and the cooldown gate that ignores advertisements
    while a cooldown window is open. The base only *checks* the window;
    each subclass decides when to arm it (:class:`GattScale` on disconnect,
    :class:`AdvertisementScale` on delivering a reading). The window is
    disabled by default (``cooldown_seconds=0``) at the transport level —
    a sensible length is hardware knowledge, so the concrete model classes
    set their own defaults. Transport-specific behaviour lives in the
    subclasses' :meth:`_handle_advertisement`.
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
        logger: logging.Logger | None = None,
    ) -> None:
        self._logger = logger or _LOGGER
        self._logger.info(
            "Initializing %s for address: %s", type(self).__name__, address
        )

        self.address = address
        self._notification_callback = notification_callback
        self._display_unit: WeightUnit = WeightUnit(display_unit)
        self._cooldown_seconds = cooldown_seconds
        self._cooldown_end_time: float = 0
        self._bg_tasks: set[asyncio.Task] = set()
        # Most recent advertisement from the target scale (raw), kept for
        # :attr:`diagnostic_info`.
        self._last_advertisement: dict[str, Any] | None = None
        # What crossed the wire recently, in order, for :attr:`diagnostic_info`.
        # One buffer across sessions — a tap-to-wake or a reconnect must not
        # erase the session that mattered — with markers between them.
        self._trace: deque[dict[str, Any]] = deque(maxlen=_TRACE_MAX_ENTRIES)
        self._trace_t0: float | None = None

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

    def _spawn(self, coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task:
        """Run ``coro`` as a tracked background task and return it.

        The reference is held until the task finishes, so it cannot be
        garbage-collected mid-flight; callers that need to cancel or await
        the task keep the returned handle themselves.
        """
        task = asyncio.create_task(coro, name=name)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    def _fire_and_forget(self, coro: Coroutine[Any, Any, Any], name: str) -> None:
        self._spawn(coro, name)

    @property
    def display_unit(self) -> WeightUnit:
        return self._display_unit

    @display_unit.setter
    def display_unit(self, value: WeightUnit) -> None:
        if value is None:
            raise ValueError("display_unit cannot be None")
        self._display_unit = WeightUnit(value)

    async def _advertisement_callback(
        self, ble_device: BLEDevice, advertisement_data: AdvertisementData
    ) -> None:
        """Filter advertisements from the scanner and dispatch the target
        scale's to :meth:`_handle_advertisement`.

        Drops advertisements from other devices, and all advertisements while
        the cooldown window is open.
        """
        if ble_device.address != self.address:
            return

        # Before the cooldown gate, so the snapshot stays current even while
        # advertisements are otherwise being ignored.
        self._record_advertisement(advertisement_data)

        if self._cooldown_seconds > 0 and time.time() < self._cooldown_end_time:
            self._logger.debug(
                "Ignoring advertisement during cooldown (ends at %s)",
                self._cooldown_end_time,
            )
            return

        await self._handle_advertisement(ble_device, advertisement_data)

    def _record_advertisement(self, advertisement_data: AdvertisementData) -> None:
        """Keep a plain snapshot of the target scale's latest advertisement.

        Runs on the path to a connection, so it must never raise: fields are
        read defensively and anything unexpected is dropped.
        """
        try:
            name = getattr(advertisement_data, "local_name", None)
            rssi = getattr(advertisement_data, "rssi", None)
            uuids = getattr(advertisement_data, "service_uuids", None) or []
            mfr = getattr(advertisement_data, "manufacturer_data", None) or {}
            self._last_advertisement = {
                "timestamp": int(time.time()),
                "local_name": name if isinstance(name, str) else None,
                "rssi": rssi if isinstance(rssi, int) else None,
                "service_uuids": [u for u in uuids if isinstance(u, str)],
                "manufacturer_data": {
                    int(company_id): bytes(payload)
                    for company_id, payload in mfr.items()
                },
            }
        except Exception:  # noqa: BLE001 - diagnostics must not break detection
            self._logger.debug("Could not record advertisement", exc_info=True)

    def _trace_key(self, direction: str, data: bytes) -> Hashable:
        """What makes two consecutive frames "the same" for the trace.

        A weigh-in streams dozens of frames that differ only in the weight;
        consecutive frames with equal keys collapse into one entry (first,
        last, count). Where the status sits is protocol knowledge, so
        subclasses override this; by default only exact repeats collapse.
        """
        return data

    def _trace_frame(
        self, direction: str, data: bytes, note: str | None = None
    ) -> None:
        """Append a frame to the session trace. Never raises."""
        try:
            data = bytes(data)
            now = time.monotonic()
            if self._trace_t0 is None:
                self._trace_t0 = now
            t = round(now - self._trace_t0, 3)
            key = (direction, note, self._trace_key(direction, data))
            last = self._trace[-1] if self._trace else None
            if last is not None and last.get("key") == key:
                last["count"] = last.get("count", 1) + 1
                last["last"] = data
                last["t_last"] = t
                return
            entry: dict[str, Any] = {"t": t, "dir": direction, "data": data, "key": key}
            if note is not None:
                entry["note"] = note
            self._trace.append(entry)
        except Exception:  # noqa: BLE001 - diagnostics must not break a session
            self._logger.debug("Could not trace frame", exc_info=True)

    def _trace_event(self, event: str, *, marker: bool = False) -> None:
        """Append a library decision or lifecycle event to the session trace.

        A ``marker`` starts a new timebase: later entries' ``t`` counts from
        it, and it alone carries wall-clock ``time``.
        """
        now = time.monotonic()
        if marker or self._trace_t0 is None:
            self._trace_t0 = now
        entry: dict[str, Any] = {"t": round(now - self._trace_t0, 3), "event": event}
        if marker:
            entry["time"] = int(time.time())
        self._trace.append(entry)

    def _mask_profile_frame(self, data: bytes, text: str) -> str:
        """Mask the personal fields of an outgoing profile frame.

        ``data`` is the frame, ``text`` its hex (MAC already masked); return
        ``text`` with the personal bytes replaced. Which frame is a profile
        and where its fields sit is protocol knowledge, so subclasses that
        send one override this. The frame itself always stays in the trace:
        when a profile was written is what the trace is for.
        """
        return text

    def _trace_snapshot(self, mask_profiles: bool) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for entry in self._trace:
            item = {k: v for k, v in entry.items() if k != "key"}
            for field in ("data", "last"):
                if field in item:
                    raw = item[field]
                    text = self._mask(raw)
                    if mask_profiles and item.get("dir") == "tx":
                        text = self._mask_profile_frame(raw, text)
                    item[field] = text
            out.append(item)
        return out

    @property
    def diagnostic_info(self) -> dict[str, Any]:
        """:meth:`get_diagnostic_info` with its defaults (frames verbatim)."""
        return self.get_diagnostic_info()

    def get_diagnostic_info(self, *, mask_profiles: bool = False) -> dict[str, Any]:
        """JSON-safe snapshot of what the scale advertises and how its last
        session behaved, for bug reports.

        Always carries ``protocol``, ``model_code``, ``model_label``
        (``"unknown"`` for an unregistered code, ``None`` when no code has
        been seen), ``flavor`` and ``trace`` — the recent frames in both
        directions, in order, with runs of near-identical frames collapsed
        and the library's decisions interleaved. Frames are otherwise
        verbatim, so they carry measurements and any profile sent. MAC
        echoes inside payloads are masked (see :func:`mask_mac_echo`).

        ``mask_profiles=True`` also masks the personal fields (sex, age or
        birth date, height) of outgoing profile frames, for dumps that end
        up posted in public. Measurements are never masked.
        """
        adv = self._last_advertisement
        info: dict[str, Any] = {
            "scale_class": type(self).__name__,
            "protocol": None,
            "model_code": None,
            "model_label": None,
            "flavor": None,
            "advertisement": None
            if adv is None
            else {
                **adv,
                "manufacturer_data": {
                    f"0x{company_id:04x}": self._mask(payload)
                    for company_id, payload in adv["manufacturer_data"].items()
                },
            },
        }
        info.update(self._protocol_diagnostics())
        info["trace"] = self._trace_snapshot(mask_profiles)
        return info

    def _protocol_diagnostics(self) -> dict[str, Any]:
        """Protocol-specific entries for :attr:`diagnostic_info`."""
        return {}

    def _advertised_mac(self) -> bytes | None:
        """The MAC as echoed in the latest advertisement, forward order.

        Only consulted when :attr:`address` is not a MAC. Where the echo sits
        is protocol knowledge, so each subclass supplies it.
        """
        return None

    def _mask(self, data: bytes) -> str:
        return mask_mac_echo(data, self.address, self._advertised_mac())

    @abc.abstractmethod
    async def _handle_advertisement(
        self, ble_device: BLEDevice, advertisement_data: AdvertisementData
    ) -> None:
        """Handle an advertisement from the target scale (already filtered by
        address and cooldown)."""

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

    A connection can succeed while service discovery transiently comes back
    incomplete, making session setup fail on an otherwise-working scale. Any
    failure in :meth:`_start_scale_session` therefore disconnects and leaves
    the cooldown window closed, so the next advertisement (the user is
    usually still on the scale) reconnects and re-runs discovery. Consecutive
    failures are bounded: after ``_MAX_CONSECUTIVE_SETUP_FAILURES`` the
    cooldown is armed, so a scale whose GATT database genuinely lacks the
    required characteristics doesn't reconnect on every advertisement.
    """

    # Consecutive session-setup failures tolerated before arming the cooldown
    # instead of retrying on the next advertisement.
    _MAX_CONSECUTIVE_SETUP_FAILURES = 3

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
            cooldown_seconds=cooldown_seconds,
            logger=logger,
        )
        self._client: BleakClient | None = None
        self._initializing: bool = False
        self._max_connect_attempts = max_connect_attempts
        self._consecutive_setup_failures: int = 0
        self._expected_disconnect_client: BleakClient | None = None
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

    def _unavailable_callback(self, client: BleakClient) -> None:
        # A disconnect we initiated in _teardown_client must not arm the
        # cooldown: the retry depends on the very next advertisement getting
        # through. Identity comparison, so a later client's natural
        # disconnect can never be mistaken for our teardown.
        if client is self._expected_disconnect_client:
            self._logger.debug("Scale disconnected (torn down after setup failure)")
            return
        self._logger.debug("Scale disconnected")
        self._trace_event("disconnected")
        self._cooldown_end_time = time.time() + self._cooldown_seconds
        self._client = None

    async def _teardown_client(self) -> None:
        """Best-effort disconnect and clear of the current client."""
        client, self._client = self._client, None
        if client is None:
            return
        # Set before awaiting: BlueZ fires the disconnect callback during the
        # disconnect() await itself.
        self._expected_disconnect_client = client
        try:
            await client.disconnect()
        except Exception:
            self._logger.debug("Error disconnecting during teardown", exc_info=True)

    def _register_setup_failure(self, reason: str) -> None:
        self._trace_event(f"session setup failed: {reason}")
        self._consecutive_setup_failures += 1
        if self._consecutive_setup_failures >= self._MAX_CONSECUTIVE_SETUP_FAILURES:
            self._consecutive_setup_failures = 0
            self._cooldown_end_time = time.time() + self._cooldown_seconds
            self._logger.error(
                "Session setup failed %d consecutive times (%s); giving up "
                "until the cooldown window (%ss) closes",
                self._MAX_CONSECUTIVE_SETUP_FAILURES,
                reason,
                self._cooldown_seconds,
            )
        else:
            self._logger.warning(
                "Session setup failed (%s); disconnected, will retry on the "
                "next advertisement (attempt %d/%d)",
                reason,
                self._consecutive_setup_failures,
                self._MAX_CONSECUTIVE_SETUP_FAILURES,
            )

    @abc.abstractmethod
    async def _start_scale_session(self, ble_device: BLEDevice) -> None:
        """Post-connection setup: read metadata, register notifications.

        Raises :class:`ScaleSessionError` if the connection's GATT database
        cannot support a session. Any exception makes the base class
        disconnect and retry on a later advertisement (bounded)."""

    @abc.abstractmethod
    def _notification_handler(
        self,
        _: BleakGATTCharacteristic,
        payload: bytearray,
        name: str | None,
        address: str,
    ) -> None:
        """Handle a raw notification payload from the scale."""

    async def _handle_advertisement(
        self, ble_device: BLEDevice, _: AdvertisementData
    ) -> None:
        async with self._lock:
            if self._client is not None or self._initializing:
                return
            self._initializing = True

        try:
            try:
                # Once per connection attempt, not per advertisement: this is
                # the only place a debug log records what the scale
                # broadcasts (its model identifier in particular).
                adv = self._last_advertisement or {}
                self._logger.debug(
                    "Advertisement from %s: name=%r rssi=%s service_uuids=%s "
                    "manufacturer_data=[%s]",
                    self.address,
                    adv.get("local_name"),
                    adv.get("rssi"),
                    adv.get("service_uuids"),
                    ", ".join(
                        f"company=0x{company_id:04x} {payload.hex()}"
                        for company_id, payload in adv.get(
                            "manufacturer_data", {}
                        ).items()
                    ),
                )
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
                await self._teardown_client()
                self._register_setup_failure("client not connected")
                return

            self._trace_event("session start", marker=True)
            try:
                await self._start_scale_session(ble_device)
            except ScaleSessionError as ex:
                await self._teardown_client()
                self._register_setup_failure(str(ex))
                return
            except Exception as ex:
                self._logger.exception(
                    "Session setup raised: %s(%s)", type(ex), ex.args
                )
                await self._teardown_client()
                self._register_setup_failure(type(ex).__name__)
                return
            self._consecutive_setup_failures = 0
        finally:
            self._initializing = False


class AdvertisementScale(RenphoScale, abc.ABC):
    """
    Base for scales that broadcast measurements in their BLE advertisements,
    with no GATT connection.

    Each advertisement from the target scale has its manufacturer-data entries
    passed to :meth:`_parse`; a non-``None`` result is wrapped in a
    :class:`ScaleData` and delivered to the notification callback.

    A weigh-in makes the scale re-broadcast its final frame for the whole
    advertising burst, so delivering a reading arms the cooldown window: one
    callback per weigh-in, with the repeated final frames (and any unit
    observation) suppressed until the window closes. ``cooldown_seconds=0``
    delivers every final frame.
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

    async def _handle_advertisement(
        self, ble_device: BLEDevice, advertisement_data: AdvertisementData
    ) -> None:
        for company_id, mfr_bytes in advertisement_data.manufacturer_data.items():
            payload = bytearray(mfr_bytes)
            # No session here: the advertisements are the whole conversation.
            self._trace_frame("adv", payload)
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
                self._cooldown_end_time = time.time() + self._cooldown_seconds
                return
