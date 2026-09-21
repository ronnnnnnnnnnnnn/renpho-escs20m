"""Tests for GattScale's session-setup failure handling (issue #9).

A GATT connection can succeed while service discovery transiently comes back
without the notify characteristic. These tests drive the full advertisement
path (``_advertisement_callback`` -> connect -> ``_start_scale_session``) with
``establish_connection`` monkeypatched, and verify that a failed setup
disconnects, leaves the cooldown window closed so the next advertisement
retries, and arms the cooldown only after the consecutive-failure bound.
"""

from __future__ import annotations

import logging
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from renpho_escs20m import RenphoQNScale, ScaleSessionError
from renpho_escs20m.const import (
    COMMAND_CHARACTERISTIC_UUID,
    NOTIFY_CHARACTERISTIC_UUID,
)
from renpho_escs20m.data import WeightUnit

ADDRESS = "AA:BB:CC:DD:EE:FF"

_FFF0_CHARS = frozenset({NOTIFY_CHARACTERISTIC_UUID, COMMAND_CHARACTERISTIC_UUID})


def _make_scale(**kwargs) -> RenphoQNScale:
    return RenphoQNScale(
        ADDRESS,
        MagicMock(),
        WeightUnit.KG,
        bleak_scanner_backend=MagicMock(),
        **kwargs,
    )


def _make_client(present_uuids: frozenset[str] = frozenset()) -> MagicMock:
    """Mock connected BleakClient exposing the given characteristics; lookups
    for anything else (including battery/firmware) return None."""
    client = MagicMock(name="client")
    chars = {uuid: MagicMock(name=uuid) for uuid in present_uuids}
    client.services.get_characteristic.side_effect = lambda uuid: chars.get(str(uuid))
    client.is_connected = True
    client.start_notify = AsyncMock()
    client.write_gatt_char = AsyncMock()
    client.disconnect = AsyncMock()
    return client


def _make_ble_device() -> MagicMock:
    ble_device = MagicMock(name="ble_device")
    ble_device.name = "QN-Scale"
    ble_device.address = ADDRESS
    return ble_device


def _patch_connection(monkeypatch, clients: list[MagicMock]) -> AsyncMock:
    """Patch establish_connection to hand out ``clients`` in order."""
    connect = AsyncMock(side_effect=clients)
    monkeypatch.setattr("renpho_escs20m.scale.establish_connection", connect)
    return connect


async def _advertise(scale: RenphoQNScale) -> None:
    await scale._advertisement_callback(_make_ble_device(), MagicMock())


@pytest.mark.asyncio
async def test_missing_notify_char_disconnects_and_leaves_cooldown_closed(
    monkeypatch,
):
    scale = _make_scale()
    client = _make_client()
    _patch_connection(monkeypatch, [client])

    await _advertise(scale)

    client.disconnect.assert_awaited_once()
    assert scale._client is None
    assert scale._cooldown_end_time == 0
    assert not scale._initializing
    assert scale._consecutive_setup_failures == 1


@pytest.mark.asyncio
async def test_next_advertisement_retries_and_success_resets_counter(monkeypatch):
    scale = _make_scale()
    bad_client = _make_client()
    good_client = _make_client(_FFF0_CHARS)
    connect = _patch_connection(monkeypatch, [bad_client, good_client])

    await _advertise(scale)
    await _advertise(scale)

    assert connect.await_count == 2
    good_client.start_notify.assert_awaited_once()
    assert scale._client is good_client
    assert scale._consecutive_setup_failures == 0


@pytest.mark.asyncio
async def test_retry_bound_arms_cooldown_and_stops_reconnecting(monkeypatch):
    scale = _make_scale()
    clients = [_make_client() for _ in range(3)]
    connect = _patch_connection(monkeypatch, clients)

    for _ in range(3):
        await _advertise(scale)

    assert scale._cooldown_end_time > time.time()
    assert scale._consecutive_setup_failures == 0
    for client in clients:
        client.disconnect.assert_awaited_once()

    # The armed cooldown now gates the very next advertisement.
    await _advertise(scale)
    assert connect.await_count == 3


@pytest.mark.asyncio
async def test_unexpected_setup_exception_disconnects_and_counts(monkeypatch):
    scale = _make_scale()
    client = _make_client(_FFF0_CHARS)
    client.start_notify = AsyncMock(side_effect=RuntimeError("boom"))
    _patch_connection(monkeypatch, [client])

    await _advertise(scale)

    client.disconnect.assert_awaited_once()
    assert scale._client is None
    assert scale._cooldown_end_time == 0
    assert scale._consecutive_setup_failures == 1


@pytest.mark.asyncio
async def test_client_not_connected_tears_down_and_counts(monkeypatch):
    scale = _make_scale()
    client = _make_client(_FFF0_CHARS)
    client.is_connected = False
    _patch_connection(monkeypatch, [client])

    await _advertise(scale)

    client.disconnect.assert_awaited_once()
    assert scale._client is None
    assert scale._consecutive_setup_failures == 1


@pytest.mark.asyncio
async def test_own_disconnect_callback_does_not_arm_cooldown(monkeypatch):
    # bleak fires the disconnect callback for our own teardown disconnect;
    # that must not arm the cooldown, or the fix would eat its own retry
    # window.
    scale = _make_scale()
    client = _make_client()
    client.disconnect = AsyncMock(
        side_effect=lambda: scale._unavailable_callback(client)
    )
    _patch_connection(monkeypatch, [client])

    await _advertise(scale)

    client.disconnect.assert_awaited_once()
    assert scale._client is None
    assert scale._cooldown_end_time == 0


@pytest.mark.asyncio
async def test_natural_disconnect_still_arms_cooldown(monkeypatch):
    scale = _make_scale()
    client = _make_client(_FFF0_CHARS)
    _patch_connection(monkeypatch, [client])

    await _advertise(scale)
    assert scale._client is client

    scale._unavailable_callback(client)

    assert scale._client is None
    assert scale._cooldown_end_time > time.time()


def test_scale_session_error_is_exported():
    from renpho_escs20m import ScaleSessionError as exported

    assert exported is ScaleSessionError
    assert issubclass(exported, Exception)


@pytest.mark.asyncio
async def test_triggering_advertisement_is_debug_logged(monkeypatch, caplog):
    """The advertisement that triggers a connection is the only record of
    what the scale broadcasts (model identifier included) in a debug log."""
    scale = _make_scale()
    _patch_connection(monkeypatch, [_make_client(_FFF0_CHARS)])
    adv = MagicMock()
    adv.local_name = "QN-Scale"
    adv.manufacturer_data = {0xFFFF: bytes.fromhex("099b0123456789")}
    adv.service_uuids = ["0000fff0-0000-1000-8000-00805f9b34fb"]
    adv.rssi = -61

    with caplog.at_level(logging.DEBUG):
        await scale._advertisement_callback(_make_ble_device(), adv)

    assert "company=0xffff 099b0123456789" in caplog.text
    assert "0000fff0-0000-1000-8000-00805f9b34fb" in caplog.text
    assert "QN-Scale" in caplog.text


@pytest.mark.asyncio
async def test_advertisement_without_manufacturer_data_still_connects(monkeypatch):
    """The debug line sits on the connection path; it must never abort it."""
    scale = _make_scale()
    connect = _patch_connection(monkeypatch, [_make_client(_FFF0_CHARS)])
    adv = MagicMock()
    adv.manufacturer_data = None

    await scale._advertisement_callback(_make_ble_device(), adv)

    connect.assert_awaited_once()
