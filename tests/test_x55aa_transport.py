"""Tests for the 0x55aa GATT transport (LeFu hardware).

The scale pauses between its weight and bioimpedance phases, so the client
accumulates a weigh-in and delivers one aggregated reading once the stream
goes quiet. These tests drive that timing with short windows.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from renpho_escs20m import Renpho55AAScale
from renpho_escs20m.const import (
    RESISTANCE_1_KEY,
    RESISTANCE_2_KEY,
    WEIGHT_KEY,
    X55AA_COMMAND_CHARACTERISTIC_UUID,
    X55AA_NOTIFY_CHARACTERISTIC_UUID,
)

ADDRESS = "CF:E8:FC:05:22:0D"

_ALL_CHARS = frozenset(
    {X55AA_NOTIFY_CHARACTERISTIC_UUID, X55AA_COMMAND_CHARACTERISTIC_UUID}
)

SETTLE = 0.05
IMPEDANCE_WAIT = 0.40
QUIET = 0.20


def _frame(frame_type: int, payload: bytes) -> bytes:
    head = b"\x55\xaa" + bytes([frame_type, 0x00, len(payload)]) + payload
    return head + bytes([sum(head) & 0xFF])


def _live(centigrams: int, resistance: int, secondary: int) -> bytes:
    payload = (
        centigrams.to_bytes(4, "big")
        + resistance.to_bytes(2, "big")
        + b"\x00\x00"
        + secondary.to_bytes(2, "big")
        + b"\x00\x00"
    )
    return _frame(0x15, payload)


def _stable(centigrams: int, resistance: int) -> bytes:
    return _frame(
        0x14, b"\x01" + centigrams.to_bytes(4, "big") + resistance.to_bytes(2, "big")
    )


def _make_scale(**kwargs) -> tuple[Renpho55AAScale, MagicMock]:
    callback = MagicMock()
    scale = Renpho55AAScale(
        ADDRESS,
        callback,
        bleak_scanner_backend=MagicMock(),
        settle_seconds=kwargs.pop("settle_seconds", SETTLE),
        impedance_wait_seconds=kwargs.pop("impedance_wait_seconds", IMPEDANCE_WAIT),
        **kwargs,
    )
    return scale, callback


def _make_client(present_uuids: frozenset[str] = _ALL_CHARS) -> MagicMock:
    client = MagicMock(name="client")
    chars = {uuid: MagicMock(name=uuid) for uuid in present_uuids}
    client.services.get_characteristic.side_effect = lambda uuid: chars.get(str(uuid))
    client.start_notify = AsyncMock()
    client.write_gatt_char = AsyncMock()
    client.chars = chars
    return client


async def _run_session_setup(scale: Renpho55AAScale, client: MagicMock) -> None:
    scale._client = client
    scale._populate_device_metadata = AsyncMock()
    ble_device = MagicMock(name="ble_device")
    ble_device.name = "ES-CS20M"
    ble_device.address = ADDRESS
    await scale._start_scale_session(ble_device)


def _feed(scale: Renpho55AAScale, *frames: bytes) -> None:
    for frame in frames:
        scale._notification_handler(
            MagicMock(), bytearray(frame), "ES-CS20M", ADDRESS
        )


@pytest.mark.asyncio
async def test_session_setup_subscribes_to_the_vendor_notify_characteristic():
    scale, _ = _make_scale()
    client = _make_client()
    await _run_session_setup(scale, client)
    client.start_notify.assert_awaited_once()
    assert (
        client.start_notify.await_args.args[0]
        is client.chars[X55AA_NOTIFY_CHARACTERISTIC_UUID]
    )


@pytest.mark.asyncio
async def test_session_setup_writes_the_hello_command():
    scale, _ = _make_scale()
    client = _make_client()
    await _run_session_setup(scale, client)
    client.write_gatt_char.assert_awaited_once()
    assert (
        client.write_gatt_char.await_args.args[0]
        is client.chars[X55AA_COMMAND_CHARACTERISTIC_UUID]
    )


@pytest.mark.asyncio
async def test_session_setup_can_skip_the_hello_command():
    scale, _ = _make_scale(send_hello=False)
    client = _make_client()
    await _run_session_setup(scale, client)
    client.write_gatt_char.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_setup_errors_when_notify_characteristic_is_absent(caplog):
    scale, _ = _make_scale()
    client = _make_client(frozenset())
    with caplog.at_level("ERROR"):
        await _run_session_setup(scale, client)
    client.start_notify.assert_not_awaited()
    assert "notification characteristic not found" in caplog.text


@pytest.mark.asyncio
async def test_one_callback_per_weigh_in():
    scale, callback = _make_scale()
    await _run_session_setup(scale, _make_client())

    _feed(scale, _live(1600, 855, 1150), _stable(6005, 0), _stable(6005, 875))
    callback.assert_not_called()

    await asyncio.sleep(QUIET)
    callback.assert_called_once()
    data = callback.call_args.args[0]
    assert data.address == ADDRESS
    assert data.measurements[WEIGHT_KEY] == 60.05
    assert data.measurements[RESISTANCE_1_KEY] == 875
    assert data.measurements[RESISTANCE_2_KEY] == 1150


@pytest.mark.asyncio
async def test_weigh_in_stays_open_until_the_bioimpedance_pass_reports():
    """The scale goes quiet mid-weigh-in; cutting it short loses resistance."""
    scale, callback = _make_scale()
    await _run_session_setup(scale, _make_client())

    _feed(scale, _live(1600, 855, 1150), _stable(6005, 0))
    await asyncio.sleep(SETTLE * 3)
    callback.assert_not_called()

    _feed(scale, _stable(6005, 875))
    await asyncio.sleep(QUIET)
    callback.assert_called_once()
    assert callback.call_args.args[0].measurements[RESISTANCE_1_KEY] == 875


@pytest.mark.asyncio
async def test_weigh_in_without_resistance_reports_weight_only():
    scale, callback = _make_scale()
    await _run_session_setup(scale, _make_client())

    _feed(scale, _live(1600, 855, 1150), _stable(6005, 0), _stable(6005, 0))
    await asyncio.sleep(IMPEDANCE_WAIT + QUIET)

    callback.assert_called_once()
    measurements = callback.call_args.args[0].measurements
    assert measurements[WEIGHT_KEY] == 60.05
    assert RESISTANCE_1_KEY not in measurements


@pytest.mark.asyncio
async def test_consecutive_weigh_ins_are_reported_separately():
    scale, callback = _make_scale()
    await _run_session_setup(scale, _make_client())

    _feed(scale, _stable(6005, 875))
    await asyncio.sleep(QUIET)
    _feed(scale, _stable(7215, 712))
    await asyncio.sleep(QUIET)

    assert callback.call_count == 2
    assert [c.args[0].measurements[WEIGHT_KEY] for c in callback.call_args_list] == [
        60.05,
        72.15,
    ]


@pytest.mark.asyncio
async def test_disconnect_delivers_a_weigh_in_still_in_progress():
    scale, callback = _make_scale()
    await _run_session_setup(scale, _make_client())

    _feed(scale, _stable(6005, 875))
    scale._unavailable_callback(MagicMock())

    callback.assert_called_once()
    assert callback.call_args.args[0].measurements[WEIGHT_KEY] == 60.05


@pytest.mark.asyncio
async def test_notifications_split_across_boundaries_are_reassembled():
    scale, callback = _make_scale()
    await _run_session_setup(scale, _make_client())

    stream = _live(1600, 855, 1150) + _stable(6005, 875)
    for index in range(0, len(stream), 3):
        _feed(scale, stream[index : index + 3])

    await asyncio.sleep(QUIET)
    callback.assert_called_once()
    assert callback.call_args.args[0].measurements[WEIGHT_KEY] == 60.05


@pytest.mark.asyncio
async def test_display_unit_cannot_be_set():
    from renpho_escs20m import WeightUnit

    scale, _ = _make_scale()
    scale.display_unit = WeightUnit.LB
    assert scale.display_unit is WeightUnit.KG
