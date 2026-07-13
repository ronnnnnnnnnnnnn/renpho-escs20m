"""Tests for the QN dual-GATT-transport support.

QN-Scale hardware ships two GATT transports for the same wire protocol:

- FFF0 service: FFF1 notify / FFF2 write — the renpho ES-CS20M transport,
  verified on hardware.
- FFE0 service: FFE1 notify / FFE2 indicate / FFE3 write / FFE4 secondary
  write — some other QN scales (e.g. Arboleaf CS20M, issue #5). The
  pre-measurement and stored-record frames arrive as indications on FFE2,
  and captured sessions split the writes: set-time (0x20) and stored-query
  (0x22) go to FFE4, everything else to FFE3.

The frame hex in the replay test is taken verbatim from the btsnoop
capture attached to issue #5 (Arboleaf CS20M, vendor byte 0x15).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from renpho_escs20m import RenphoQNScale
from renpho_escs20m.const import (
    COMMAND_CHARACTERISTIC_UUID,
    FFE0_ALT_COMMAND_CHARACTERISTIC_UUID,
    FFE0_COMMAND_CHARACTERISTIC_UUID,
    FFE0_INDICATE_CHARACTERISTIC_UUID,
    FFE0_NOTIFY_CHARACTERISTIC_UUID,
    NOTIFY_CHARACTERISTIC_UUID,
)
from renpho_escs20m.data import WeightUnit

ADDRESS = "AA:BB:CC:DD:EE:FF"

_FFF0_CHARS = frozenset({NOTIFY_CHARACTERISTIC_UUID, COMMAND_CHARACTERISTIC_UUID})
_FFE0_CHARS = frozenset(
    {
        FFE0_NOTIFY_CHARACTERISTIC_UUID,
        FFE0_INDICATE_CHARACTERISTIC_UUID,
        FFE0_COMMAND_CHARACTERISTIC_UUID,
        FFE0_ALT_COMMAND_CHARACTERISTIC_UUID,
    }
)


def _make_scale(**kwargs) -> tuple[RenphoQNScale, MagicMock]:
    callback = MagicMock()
    scale = RenphoQNScale(
        ADDRESS,
        callback,
        kwargs.pop("display_unit", WeightUnit.KG),
        bleak_scanner_backend=MagicMock(),
        **kwargs,
    )
    return scale, callback


def _make_client(present_uuids: frozenset[str]) -> MagicMock:
    """Mock BleakClient exposing one distinct characteristic per UUID in
    ``present_uuids``; lookups for anything else return None."""
    client = MagicMock(name="client")
    chars = {uuid: MagicMock(name=uuid) for uuid in present_uuids}
    client.services.get_characteristic.side_effect = (
        lambda uuid: chars.get(str(uuid))
    )
    client.start_notify = AsyncMock()
    client.write_gatt_char = AsyncMock()
    client.chars = chars
    return client


def _subscribed_chars(client: MagicMock) -> list[MagicMock]:
    return [call.args[0] for call in client.start_notify.await_args_list]


def _writes(client: MagicMock) -> list[tuple[MagicMock, bytes]]:
    return [
        (call.args[0], bytes(call.args[1]))
        for call in client.write_gatt_char.await_args_list
    ]


async def _run_session_setup(scale: RenphoQNScale, client: MagicMock) -> None:
    scale._client = client
    scale._populate_device_metadata = AsyncMock()
    ble_device = MagicMock(name="ble_device")
    ble_device.name = "QN-Scale"
    ble_device.address = ADDRESS
    await scale._start_scale_session(ble_device)


# ---- notification subscriptions -------------------------------------------


@pytest.mark.asyncio
async def test_session_setup_subscribes_fff1_only_on_fff0_transport():
    scale, _ = _make_scale()
    client = _make_client(_FFF0_CHARS)
    await _run_session_setup(scale, client)
    assert _subscribed_chars(client) == [client.chars[NOTIFY_CHARACTERISTIC_UUID]]


@pytest.mark.asyncio
async def test_session_setup_falls_back_to_ffe1_and_ffe2():
    scale, _ = _make_scale()
    client = _make_client(_FFE0_CHARS)
    await _run_session_setup(scale, client)
    assert _subscribed_chars(client) == [
        client.chars[FFE0_NOTIFY_CHARACTERISTIC_UUID],
        client.chars[FFE0_INDICATE_CHARACTERISTIC_UUID],
    ]


@pytest.mark.asyncio
async def test_session_setup_prefers_fff0_when_both_transports_present():
    scale, _ = _make_scale()
    client = _make_client(_FFF0_CHARS | _FFE0_CHARS)
    await _run_session_setup(scale, client)
    assert _subscribed_chars(client) == [client.chars[NOTIFY_CHARACTERISTIC_UUID]]


@pytest.mark.asyncio
async def test_session_setup_ffe0_without_ffe2_still_subscribes_ffe1():
    scale, _ = _make_scale()
    client = _make_client(
        frozenset(
            {FFE0_NOTIFY_CHARACTERISTIC_UUID, FFE0_COMMAND_CHARACTERISTIC_UUID}
        )
    )
    await _run_session_setup(scale, client)
    assert _subscribed_chars(client) == [
        client.chars[FFE0_NOTIFY_CHARACTERISTIC_UUID]
    ]


@pytest.mark.asyncio
async def test_session_setup_errors_when_no_notify_characteristic(caplog):
    scale, _ = _make_scale()
    client = _make_client(frozenset())
    with caplog.at_level("ERROR"):
        await _run_session_setup(scale, client)
    client.start_notify.assert_not_awaited()
    assert "notification characteristic not found" in caplog.text


# ---- command-write routing --------------------------------------------------


@pytest.mark.asyncio
async def test_safe_write_uses_fff2_on_fff0_transport():
    scale, _ = _make_scale()
    client = _make_client(_FFF0_CHARS | _FFE0_CHARS)
    scale._client = client
    for opcode in (0x13, 0x20, 0x22, 0x1F, 0xA0):
        await scale._safe_write(bytearray([opcode, 0x04, 0xFF, 0x00]))
    assert [char for char, _ in _writes(client)] == (
        [client.chars[COMMAND_CHARACTERISTIC_UUID]] * 5
    )


@pytest.mark.asyncio
async def test_safe_write_splits_ffe3_and_ffe4_on_ffe0_transport():
    scale, _ = _make_scale()
    client = _make_client(_FFE0_CHARS)
    scale._client = client
    # Captured sessions route set-time (0x20) and the stored-measurement
    # query (0x22) to FFE4 and everything else to FFE3.
    for opcode in (0x13, 0x20, 0x22, 0x1F, 0xA0):
        await scale._safe_write(bytearray([opcode, 0x04, 0xFF, 0x00]))
    ffe3 = client.chars[FFE0_COMMAND_CHARACTERISTIC_UUID]
    ffe4 = client.chars[FFE0_ALT_COMMAND_CHARACTERISTIC_UUID]
    assert [char for char, _ in _writes(client)] == [ffe3, ffe4, ffe4, ffe3, ffe3]


@pytest.mark.asyncio
async def test_safe_write_ffe0_skips_alt_opcodes_when_ffe4_absent(caplog):
    # Routing is strict: no capture shows an FFE0 scale without FFE4, so
    # rather than guess at a substitute characteristic the library skips
    # the write and surfaces the unknown GATT layout in the logs.
    scale, _ = _make_scale()
    client = _make_client(
        frozenset(
            {FFE0_NOTIFY_CHARACTERISTIC_UUID, FFE0_COMMAND_CHARACTERISTIC_UUID}
        )
    )
    scale._client = client
    with caplog.at_level("WARNING"):
        for opcode in (0x20, 0x22):
            await scale._safe_write(bytearray([opcode, 0x04, 0xFF, 0x00]))
    client.write_gatt_char.assert_not_awaited()
    assert caplog.text.count("command characteristic not found") == 2
    # Non-alt commands are unaffected by the missing FFE4.
    await scale._safe_write(bytearray([0x13, 0x04, 0xFF, 0x00]))
    assert [char for char, _ in _writes(client)] == [
        client.chars[FFE0_COMMAND_CHARACTERISTIC_UUID]
    ]


@pytest.mark.asyncio
async def test_safe_write_warns_when_no_command_characteristic(caplog):
    scale, _ = _make_scale()
    client = _make_client(frozenset({FFE0_NOTIFY_CHARACTERISTIC_UUID}))
    scale._client = client
    with caplog.at_level("WARNING"):
        await scale._safe_write(bytearray([0x13, 0x04, 0xFF, 0x00]))
    client.write_gatt_char.assert_not_awaited()
    assert "command characteristic not found" in caplog.text


# ---- full-session replay of the issue #5 Arboleaf CS20M capture ------------

# Scale -> app frames, capture order. The 0x21 pre-measurement and the
# 0x23 stored records arrived as indications on FFE2; the rest as
# notifications on FFE1 (the handler does not distinguish).
_CAPTURE_RX = [
    "120f1505aa0bcb0bd83f013f010523",  # unit request
    "140b150000010000000035",  # measurement-init request
    "210515013c",  # pre-measurement (extended length, basic flavor)
    "2314150501da0100001c9301f901f300000000ca",  # stored records 1-5
    "2314150502340300001c7001fd01f00000000005",
    "2314150503aa0300001c5c01fa01f9000000006e",
    "2314150504400500001c6b01fa01f30000000010",
    "2314150505550500001c6b01f901f50000000027",
    "100b151c5c0000000000a8",  # settling frames
    "100b151c6b0000000000b7",
    "100b151c660000000000b2",
    "100b151c660101fa01ee9d",  # final: 72.70 kg, r1=506, r2=494
]


@pytest.mark.asyncio
async def test_arboleaf_capture_replay_over_ffe0_transport():
    scale, callback = _make_scale(
        display_unit=WeightUnit.LB, clear_stored_measurements=True
    )
    client = _make_client(_FFE0_CHARS)
    scale._client = client

    for hx in _CAPTURE_RX:
        scale._notification_handler(
            MagicMock(), bytearray(bytes.fromhex(hx)), "QN-Scale", ADDRESS
        )
        # drain the fire-and-forget write tasks so ordering is deterministic
        for _ in range(4):
            await asyncio.sleep(0)

    ffe3 = client.chars[FFE0_COMMAND_CHARACTERISTIC_UUID]
    ffe4 = client.chars[FFE0_ALT_COMMAND_CHARACTERISTIC_UUID]
    writes = _writes(client)

    assert [char for char, _ in writes] == [ffe3, ffe4, ffe4, ffe3]

    unit_cmd, time_cmd, stored_query, end_cmd = (data for _, data in writes)
    # Unit reply: lb (0x02), vendor byte 0x15 echoed. The captured session
    # additionally carries user demographics in bytes 5-7; the library
    # sends zeros there (hardware-verified on renpho).
    assert unit_cmd.hex() == "130915021000000043"
    assert time_cmd.hex().startswith("200815") and len(time_cmd) == 8
    assert time_cmd[7] == sum(time_cmd[:7]) & 0xFF
    # Byte-exact frames from the capture.
    assert stored_query.hex() == "2204153b"
    assert end_cmd.hex() == "1f05151049"

    callback.assert_called_once()
    data = callback.call_args[0][0]
    assert data.measurements["weight"] == 72.70
    assert data.measurements["resistance_1"] == 506
    assert data.measurements["resistance_2"] == 494
