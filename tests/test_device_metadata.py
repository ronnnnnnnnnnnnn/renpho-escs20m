"""Unit tests for device-metadata reads (battery level, firmware revision)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from renpho_escs20m.const import (
    BATTERY_LEVEL_CHARACTERISTIC_UUID,
    FIRMWARE_REVISION_CHARACTERISTIC_UUID,
)
from renpho_escs20m.scale import _read_device_metadata


def _make_client(
    *,
    battery_data: bytes | bytearray = bytearray(b"\x55"),
    firmware_data: bytes | bytearray = bytearray(b"1.2.3"),
    battery_char_present: bool = True,
    firmware_char_present: bool = True,
    battery_raises: BaseException | None = None,
    firmware_raises: BaseException | None = None,
) -> MagicMock:
    """Build a mock BleakClient with controllable per-characteristic behavior.

    Each characteristic can be present or absent; reads can be configured to
    return specific bytes or raise. The two characteristics are independent.
    """
    client = MagicMock(name="client")

    battery_char = MagicMock(name="battery_char") if battery_char_present else None
    firmware_char = MagicMock(name="firmware_char") if firmware_char_present else None

    def get_char(uuid: str):
        if uuid == BATTERY_LEVEL_CHARACTERISTIC_UUID:
            return battery_char
        if uuid == FIRMWARE_REVISION_CHARACTERISTIC_UUID:
            return firmware_char
        raise AssertionError(f"unexpected uuid {uuid!r}")

    client.services.get_characteristic.side_effect = get_char

    async def read(char):
        if char is battery_char:
            if battery_raises is not None:
                raise battery_raises
            return battery_data
        if char is firmware_char:
            if firmware_raises is not None:
                raise firmware_raises
            return firmware_data
        raise AssertionError("unexpected characteristic passed to read_gatt_char")

    client.read_gatt_char = AsyncMock(side_effect=read)
    return client


@pytest.mark.asyncio
async def test_read_device_metadata_happy_path():
    client = _make_client(
        battery_data=bytearray(b"\x55"),
        firmware_data=bytearray(b"1.2.3"),
    )
    battery, firmware = await _read_device_metadata(client)
    assert battery == 0x55
    assert firmware == "1.2.3"


@pytest.mark.asyncio
async def test_read_device_metadata_battery_read_raises():
    client = _make_client(battery_raises=RuntimeError("simulated BLE error"))
    battery, firmware = await _read_device_metadata(client)
    assert battery is None
    assert firmware == "1.2.3"


@pytest.mark.asyncio
async def test_read_device_metadata_battery_characteristic_absent():
    client = _make_client(battery_char_present=False)
    battery, firmware = await _read_device_metadata(client)
    assert battery is None
    assert firmware == "1.2.3"


@pytest.mark.asyncio
async def test_read_device_metadata_battery_empty_payload():
    client = _make_client(battery_data=bytearray(b""))
    battery, firmware = await _read_device_metadata(client)
    assert battery is None
    assert firmware == "1.2.3"


@pytest.mark.asyncio
async def test_read_device_metadata_battery_byte_above_sig_range_passes_through():
    # SIG defines 0-100, but firmware bugs are real; pass through
    # unmodified rather than clamp or reject.
    client = _make_client(battery_data=bytearray(b"\xff"))
    battery, firmware = await _read_device_metadata(client)
    assert battery == 255
    assert firmware == "1.2.3"


@pytest.mark.parametrize(
    "raw_byte,expected",
    [(b"\x00", 0), (b"\x64", 100)],
)
@pytest.mark.asyncio
async def test_read_device_metadata_battery_boundary_values(raw_byte, expected):
    client = _make_client(battery_data=bytearray(raw_byte))
    battery, _ = await _read_device_metadata(client)
    assert battery == expected


@pytest.mark.asyncio
async def test_read_device_metadata_firmware_read_raises():
    client = _make_client(firmware_raises=RuntimeError("simulated BLE error"))
    battery, firmware = await _read_device_metadata(client)
    assert battery == 0x55
    assert firmware is None


@pytest.mark.asyncio
async def test_read_device_metadata_firmware_characteristic_absent():
    client = _make_client(firmware_char_present=False)
    battery, firmware = await _read_device_metadata(client)
    assert battery == 0x55
    assert firmware is None


@pytest.mark.asyncio
async def test_read_device_metadata_firmware_strips_trailing_whitespace_and_nulls():
    client = _make_client(firmware_data=bytearray(b"1.2.3\x00\x00 "))
    battery, firmware = await _read_device_metadata(client)
    assert battery == 0x55
    assert firmware == "1.2.3"


@pytest.mark.asyncio
async def test_read_device_metadata_firmware_invalid_utf8():
    # 0xFF is not valid UTF-8 in any position
    client = _make_client(firmware_data=bytearray(b"\xff\xfe\x00"))
    battery, firmware = await _read_device_metadata(client)
    assert battery == 0x55
    assert firmware is None


@pytest.mark.asyncio
async def test_read_device_metadata_firmware_all_whitespace_returns_none():
    # If the device returns nothing meaningful (only padding), surface as
    # None rather than an empty string — the empty-string state is not
    # useful and gives consumers a falsy sentinel they can check.
    client = _make_client(firmware_data=bytearray(b"\x00\x00 \t"))
    battery, firmware = await _read_device_metadata(client)
    assert battery == 0x55
    assert firmware is None


@pytest.mark.asyncio
async def test_read_device_metadata_battery_timeout_does_not_affect_firmware(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "renpho_escs20m.scale._DEVICE_METADATA_READ_TIMEOUT_SECONDS", 0.001
    )
    client = _make_client()
    battery_char = client.services.get_characteristic(BATTERY_LEVEL_CHARACTERISTIC_UUID)
    firmware_char = client.services.get_characteristic(FIRMWARE_REVISION_CHARACTERISTIC_UUID)

    async def read(char):
        if char is battery_char:
            await asyncio.sleep(0.01)
            return bytearray(b"\x55")
        if char is firmware_char:
            return bytearray(b"1.2.3")
        raise AssertionError("unexpected characteristic passed to read_gatt_char")

    client.read_gatt_char = AsyncMock(side_effect=read)
    battery, firmware = await _read_device_metadata(client)
    assert battery is None
    assert firmware == "1.2.3"


@pytest.mark.asyncio
async def test_read_device_metadata_firmware_timeout_does_not_affect_battery(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "renpho_escs20m.scale._DEVICE_METADATA_READ_TIMEOUT_SECONDS", 0.001
    )
    client = _make_client()
    battery_char = client.services.get_characteristic(BATTERY_LEVEL_CHARACTERISTIC_UUID)
    firmware_char = client.services.get_characteristic(FIRMWARE_REVISION_CHARACTERISTIC_UUID)

    async def read(char):
        if char is battery_char:
            return bytearray(b"\x42")
        if char is firmware_char:
            await asyncio.sleep(0.01)
            return bytearray(b"2.0.0")
        raise AssertionError("unexpected characteristic passed to read_gatt_char")

    client.read_gatt_char = AsyncMock(side_effect=read)
    battery, firmware = await _read_device_metadata(client)
    assert battery == 0x42
    assert firmware is None


def _make_scale(scanner=None, **kwargs):
    """Build a `RenphoESCS20MScale` with mocked scanner backend.

    Bypasses the platform-specific scanner construction so tests run on any
    OS without a Bluetooth adapter. The scanner mock satisfies the
    ``register_detection_callback`` call in `__init__`. Additional
    constructor kwargs can be passed through ``**kwargs``.
    """
    from renpho_escs20m.scale import RenphoESCS20MScale

    if scanner is None:
        scanner = MagicMock(name="scanner")
    return RenphoESCS20MScale(
        address="00:11:22:33:44:55",
        notification_callback=lambda data: None,
        bleak_scanner_backend=scanner,
        **kwargs,
    )


def test_scale_battery_and_firmware_default_to_none():
    scale = _make_scale()
    assert scale.battery_level is None
    assert scale.firmware_revision is None


@pytest.mark.asyncio
async def test_populate_device_metadata_sets_both_from_initial_none():
    scale = _make_scale()
    client = _make_client(
        battery_data=bytearray(b"\x42"),
        firmware_data=bytearray(b"2.0.0"),
    )
    await scale._populate_device_metadata(client)
    assert scale.battery_level == 0x42
    assert scale.firmware_revision == "2.0.0"


@pytest.mark.asyncio
async def test_populate_device_metadata_preserves_battery_when_read_fails():
    scale = _make_scale()
    # Seed prior cached values
    scale._battery_level = 88
    scale._firmware_revision = "1.0.0"

    client = _make_client(
        battery_raises=RuntimeError("transient"),
        firmware_data=bytearray(b"1.0.1"),
    )
    await scale._populate_device_metadata(client)
    assert scale.battery_level == 88           # preserved
    assert scale.firmware_revision == "1.0.1"  # updated


@pytest.mark.asyncio
async def test_populate_device_metadata_preserves_firmware_when_read_fails():
    scale = _make_scale()
    scale._battery_level = 50
    scale._firmware_revision = "1.0.0"

    client = _make_client(
        battery_data=bytearray(b"\x4b"),
        firmware_raises=RuntimeError("transient"),
    )
    await scale._populate_device_metadata(client)
    assert scale.battery_level == 0x4b         # updated
    assert scale.firmware_revision == "1.0.0"  # preserved


@pytest.mark.asyncio
async def test_populate_device_metadata_preserves_both_when_both_reads_fail():
    scale = _make_scale()
    scale._battery_level = 77
    scale._firmware_revision = "9.9.9"

    client = _make_client(
        battery_raises=RuntimeError("transient"),
        firmware_raises=RuntimeError("transient"),
    )
    await scale._populate_device_metadata(client)
    assert scale.battery_level == 77
    assert scale.firmware_revision == "9.9.9"


def test_max_connect_attempts_defaults_to_2():
    scale = _make_scale()
    assert scale._max_connect_attempts == 2


def test_max_connect_attempts_custom_value_is_stored():
    scale = _make_scale(max_connect_attempts=5)
    assert scale._max_connect_attempts == 5


@pytest.mark.parametrize("invalid", [0, -1, -100])
def test_max_connect_attempts_below_one_raises(invalid):
    with pytest.raises(ValueError, match="max_connect_attempts"):
        _make_scale(max_connect_attempts=invalid)
