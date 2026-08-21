"""Tests for the 0x55aa GATT transport client (LeFu hardware).

By default the client is notify-only: it subscribes to the vendor
characteristic, fires the callback on the scale's final measurement frame,
and never writes — in particular it never acknowledges stored offline
records, which keeps them available to the official app. The opt-in
``clear_stored_measurements`` flag acknowledges each stored record instead.

Replay frames are verbatim capture bytes; see test_x55aa_protocol.py
for provenance.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from renpho_escs20m import Renpho55AAScale
from renpho_escs20m.const import (
    RESISTANCE_1_KEY,
    WEIGHT_KEY,
    X55AA_COMMAND_CHARACTERISTIC_UUID,
    X55AA_NOTIFY_CHARACTERISTIC_UUID,
)
from renpho_escs20m.data import WeightUnit
from renpho_escs20m.scale import ScaleSessionError

ADDRESS = "CF:E8:FC:05:22:0D"

_ALL_CHARS = frozenset(
    {X55AA_NOTIFY_CHARACTERISTIC_UUID, X55AA_COMMAND_CHARACTERISTIC_UUID}
)


def _make_scale(**kwargs) -> tuple[Renpho55AAScale, MagicMock]:
    callback = MagicMock()
    scale = Renpho55AAScale(
        ADDRESS, callback, bleak_scanner_backend=MagicMock(), **kwargs
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
        scale._notification_handler(MagicMock(), bytearray(frame), "ES-CS20M", ADDRESS)


# ---- session setup ---------------------------------------------------------


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
async def test_session_setup_never_writes():
    """The scale streams on its own; the client is strictly notify-only."""
    scale, _ = _make_scale()
    client = _make_client()
    await _run_session_setup(scale, client)
    client.write_gatt_char.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_setup_raises_when_the_notify_characteristic_is_missing():
    scale, _ = _make_scale()
    client = _make_client(frozenset())
    with pytest.raises(ScaleSessionError):
        await _run_session_setup(scale, client)


# ---- capture replays -------------------------------------------------------

# R-A012: status, junk frame, settling ramp, then a final repeated by the
# hardware. Offline store empty (status byte 3 = 0), so no 0x15 frames.
RA012_SESSION = [
    "55aa110005010001000017",
    "20a4200020102c002000000000",
    "55aa1400070000001b990000ce",
    "55aa1400070000001b940000c9",
    "55aa1400070000001b710000a6",
    "55aa1400070100001b71024af3",
    "55aa1400070100001b71024af3",
]

# ES-CS20MB1: 7 stored records pending; four arrive interleaved with the
# live session before the final.
MB1_SESSION = [
    "55aa11000501010107001f",
    "55aa15000c0000064003570000047e000042",
    "55aa120005010101070020",
    "55aa15000c000005e60310000002830000a3",
    "55aa15000c000017cf036900000267 0000db".replace(" ", ""),
    "55aa15000c000017ca0363000001470000af",
    "55aa14000701000017d9035664",
]


@pytest.mark.asyncio
async def test_ra012_capture_replay_fires_once_with_the_final_reading():
    scale, callback = _make_scale()
    client = _make_client()
    await _run_session_setup(scale, client)

    _feed(scale, *[bytes.fromhex(h) for h in RA012_SESSION])

    client.write_gatt_char.assert_not_awaited()
    callback.assert_called_once()
    data = callback.call_args[0][0]
    assert data.measurements == {WEIGHT_KEY: 70.25, RESISTANCE_1_KEY: 586}
    assert data.address == ADDRESS


@pytest.mark.asyncio
async def test_mb1_capture_replay_reports_the_live_final_not_the_stored_records():
    scale, callback = _make_scale()
    client = _make_client()
    await _run_session_setup(scale, client)

    _feed(scale, *[bytes.fromhex(h) for h in MB1_SESSION])

    # One callback: the live 61.05 kg final. The stored 16.00/15.10/60.95/
    # 60.90 kg records are logged and discarded, never reported and never
    # acknowledged.
    client.write_gatt_char.assert_not_awaited()
    callback.assert_called_once()
    data = callback.call_args[0][0]
    assert data.measurements == {WEIGHT_KEY: 61.05, RESISTANCE_1_KEY: 854}


# ---- stored-record acknowledgement (clear_stored_measurements) ---------------

STORED_RECORD_ACK = bytes.fromhex("55aa950001" "0196")


@pytest.mark.asyncio
async def test_clear_stored_measurements_acks_each_stored_record():
    scale, callback = _make_scale(clear_stored_measurements=True)
    client = _make_client()
    await _run_session_setup(scale, client)

    _feed(scale, *[bytes.fromhex(h) for h in MB1_SESSION])
    await asyncio.sleep(0)

    # Four stored records in the capture -> four acks, each write-with-
    # response to the command characteristic. The live final still fires
    # the callback exactly once; the records themselves are never reported.
    assert client.write_gatt_char.await_count == 4
    for call in client.write_gatt_char.await_args_list:
        assert call.args[0] is client.chars[X55AA_COMMAND_CHARACTERISTIC_UUID]
        assert bytes(call.args[1]) == STORED_RECORD_ACK
        assert call.kwargs.get("response") is True
    callback.assert_called_once()
    assert callback.call_args[0][0].measurements == {
        WEIGHT_KEY: 61.05,
        RESISTANCE_1_KEY: 854,
    }


@pytest.mark.asyncio
async def test_clear_stored_measurements_without_command_characteristic(caplog):
    """Missing write characteristic: warn once, behave as if the flag were off."""
    scale, callback = _make_scale(clear_stored_measurements=True)
    client = _make_client(frozenset({X55AA_NOTIFY_CHARACTERISTIC_UUID}))
    with caplog.at_level(logging.WARNING):
        await _run_session_setup(scale, client)
        _feed(scale, *[bytes.fromhex(h) for h in MB1_SESSION])
        await asyncio.sleep(0)

    client.write_gatt_char.assert_not_awaited()
    callback.assert_called_once()
    warnings = [r for r in caplog.records if "command characteristic" in r.message]
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_clear_stored_measurements_does_not_ack_a_short_stored_record():
    scale, _ = _make_scale(clear_stored_measurements=True)
    client = _make_client()
    await _run_session_setup(scale, client)

    # 0x15 with a truncated payload (checksum valid, body too short to parse).
    head = b"\x55\xaa\x15\x00\x04\x00\x00\x06\x40"
    _feed(scale, head + bytes([sum(head) & 0xFF]))
    await asyncio.sleep(0)

    client.write_gatt_char.assert_not_awaited()


@pytest.mark.asyncio
async def test_clear_stored_measurements_acks_are_written_one_at_a_time():
    """A burst of stored records must not overlap write-with-response calls
    on the GATT link; the acks are serialized."""
    scale, _ = _make_scale(clear_stored_measurements=True)
    client = _make_client()
    in_flight = 0
    max_in_flight = 0
    release = asyncio.Event()

    async def slow_write(*_args, **_kwargs):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await release.wait()
        in_flight -= 1

    client.write_gatt_char = AsyncMock(side_effect=slow_write)
    await _run_session_setup(scale, client)

    _feed(scale, *[bytes.fromhex(h) for h in MB1_SESSION])
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(*scale._bg_tasks)

    assert client.write_gatt_char.await_count == 4
    assert max_in_flight == 1


@pytest.mark.asyncio
async def test_clear_stored_measurements_pending_ack_after_disconnect_is_quiet(
    caplog,
):
    """An ack still queued when the link drops is an expected race, not a fault."""
    scale, _ = _make_scale(clear_stored_measurements=True)
    client = _make_client()
    await _run_session_setup(scale, client)

    with caplog.at_level(logging.DEBUG):
        _feed(scale, bytes.fromhex(MB1_SESSION[1]))
        scale._unavailable_callback(client)  # before the task runs
        await asyncio.sleep(0)

    client.write_gatt_char.assert_not_awaited()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.asyncio
async def test_clear_stored_measurements_write_failure_is_logged_not_raised(caplog):
    scale, callback = _make_scale(clear_stored_measurements=True)
    client = _make_client()
    client.write_gatt_char.side_effect = RuntimeError("gatt write failed")
    await _run_session_setup(scale, client)

    with caplog.at_level(logging.WARNING):
        _feed(scale, *[bytes.fromhex(h) for h in MB1_SESSION])
        await asyncio.sleep(0)

    callback.assert_called_once()
    assert any("acknowledge" in r.message for r in caplog.records)


# ES-26BB-B: complete session — status (power on), settling x4 with zero
# resistance, the final after the bioimpedance pass, status again at
# power-off. Offline store empty.
ES26_SESSION = [
    "55aa110005010101000018",
    "55aa14000700000028dc00001e",
    "55aa14000700000028dc00001e",
    "55aa14000700000028dc00001e",
    "55aa14000700000028dc00001e",
    "55aa14000701000028dc028cad",
    "55aa110005000101000017",
]


@pytest.mark.asyncio
async def test_es26bbb_capture_replay_fires_once_with_the_final_reading():
    scale, callback = _make_scale()
    client = _make_client()
    await _run_session_setup(scale, client)

    _feed(scale, *[bytes.fromhex(h) for h in ES26_SESSION])

    client.write_gatt_char.assert_not_awaited()
    callback.assert_called_once()
    data = callback.call_args[0][0]
    assert data.measurements == {WEIGHT_KEY: 104.60, RESISTANCE_1_KEY: 652}


# ---- measurement handling --------------------------------------------------


def _final(status: int, centigrams: int, resistance: int) -> bytes:
    payload = (
        bytes([status]) + centigrams.to_bytes(4, "big") + resistance.to_bytes(2, "big")
    )
    head = b"\x55\xaa\x14\x00\x07" + payload
    return head + bytes([sum(head) & 0xFF])


@pytest.mark.asyncio
async def test_a_new_settling_phase_rearms_the_final():
    scale, callback = _make_scale()
    client = _make_client()
    await _run_session_setup(scale, client)

    _feed(scale, _final(0x01, 7025, 586))
    _feed(scale, _final(0x00, 6000, 0))  # someone steps on again
    _feed(scale, _final(0x01, 6105, 590))

    assert callback.call_count == 2


@pytest.mark.asyncio
async def test_unsupported_statuses_are_skipped_with_one_warning_each(caplog):
    scale, callback = _make_scale()
    client = _make_client()
    await _run_session_setup(scale, client)

    with caplog.at_level(logging.WARNING):
        _feed(scale, _final(0x11, 7025, 586))
        _feed(scale, _final(0x11, 7025, 586))  # repeated: no second warning
        _feed(scale, _final(0x03, 18000, 0))

    callback.assert_not_called()
    warnings = [
        r for r in caplog.records if "unsupported measurement status" in r.message
    ]
    assert len(warnings) == 2


@pytest.mark.asyncio
async def test_zero_resistance_final_reports_weight_only():
    """Socks-on weigh-in: the bioimpedance pass yields nothing."""
    scale, callback = _make_scale()
    client = _make_client()
    await _run_session_setup(scale, client)

    _feed(scale, _final(0x01, 7025, 0))

    callback.assert_called_once()
    assert callback.call_args[0][0].measurements == {WEIGHT_KEY: 70.25}


@pytest.mark.asyncio
async def test_final_with_no_advertised_name_reports_an_empty_name():
    """bleak's BLEDevice.name can be None; ScaleData.name must stay a str."""
    scale, callback = _make_scale()
    client = _make_client()
    await _run_session_setup(scale, client)

    scale._notification_handler(
        MagicMock(), bytearray(_final(0x01, 7025, 586)), None, ADDRESS
    )

    callback.assert_called_once()
    assert callback.call_args[0][0].name == ""


@pytest.mark.asyncio
async def test_warned_statuses_reset_per_connection(caplog):
    scale, callback = _make_scale()
    client = _make_client()
    await _run_session_setup(scale, client)

    with caplog.at_level(logging.WARNING):
        _feed(scale, _final(0x02, 7025, 586))
        scale._client = client
        scale._unavailable_callback(client)
        await _run_session_setup(scale, client)
        _feed(scale, _final(0x02, 7025, 586))

    callback.assert_not_called()
    warnings = [
        r for r in caplog.records if "unsupported measurement status" in r.message
    ]
    assert len(warnings) == 2


# ---- status frames / display unit ------------------------------------------


@pytest.mark.asyncio
async def test_display_unit_follows_the_scale_status_frame():
    scale, callback = _make_scale()
    client = _make_client()
    await _run_session_setup(scale, client)

    # Status announces lb, then a final arrives.
    status_lb = bytes.fromhex("55aa110005010201000019")
    _feed(scale, status_lb, _final(0x01, 7025, 586))

    assert scale.display_unit is WeightUnit.LB
    assert callback.call_args[0][0].display_unit is WeightUnit.LB


@pytest.mark.asyncio
async def test_display_unit_assignment_is_ignored():
    scale, _ = _make_scale()
    scale.display_unit = WeightUnit.LB
    assert scale.display_unit is WeightUnit.KG


# ---- lifecycle -------------------------------------------------------------


@pytest.mark.asyncio
async def test_unavailable_resets_the_session_state():
    scale, callback = _make_scale()
    client = _make_client()
    await _run_session_setup(scale, client)

    _feed(scale, _final(0x01, 7025, 586))
    scale._client = client
    scale._unavailable_callback(client)
    await _run_session_setup(scale, client)
    _feed(scale, _final(0x01, 7030, 590))

    assert callback.call_count == 2
