"""Extended-flavor tests for the 0x55aa GATT client (model ids 0x30/0x31).

The extended flavor never finalises on 0x14: the scale streams settling
frames, and only a guest profile write makes it release the 0x18 final.
The client therefore writes (set-time →) profile → display unit at connect
when it has a profile, or will resolve the user from the settled weight and
write the profile once per weigh-in. Replay frames are capture bytes
except the synthesized settling frames from ``_settling()`` (see
test_x55aa_protocol.py for provenance).
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import logging
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from renpho_escs20m import Renpho55AAScale, X55AAProfile
from renpho_escs20m.body_metrics import Sex
from renpho_escs20m.const import (
    RESISTANCE_1_KEY,
    WEIGHT_KEY,
    X55AA_COMMAND_CHARACTERISTIC_UUID,
    X55AA_NOTIFY_CHARACTERISTIC_UUID,
)
from renpho_escs20m.scale import GattScale
from renpho_escs20m.x55aa.protocol import MANUFACTURER_ID, build_guest_profile_command

ADDRESS = "CF:EA:02:07:93:67"
MODEL_ESCS20MB2 = 0x0031
MODEL_SIBLING = 0x0030
NAME = "ES-CS20M"

_ALL_CHARS = frozenset(
    {X55AA_NOTIFY_CHARACTERISTIC_UUID, X55AA_COMMAND_CHARACTERISTIC_UUID}
)

# Fixed clock: 2026-08-15T17:25:17 in UTC+2 → a captured set-time frame.
FIXED_NOW = datetime.datetime(
    2026, 8, 15, 17, 25, 17, tzinfo=datetime.timezone(datetime.timedelta(hours=2))
)
SET_TIME = bytes.fromhex("55aa9700090100006a8084dd0002ed")
UNIT_KG = bytes.fromhex("55aa9000040100000094")

L0G1C5 = X55AAProfile(sex=Sex.Male, birthday=datetime.date(1990, 1, 1), height_m=1.70)
PROFILE_L0G1C5_88_40 = bytes.fromhex("55aa96000e1907c6010106a400002288a9ff058c")

STATUS_ON = bytes.fromhex("55aa11000a0101010000480000000065")
STATUS_OFF = bytes.fromhex("55aa11000a0001010000440000000060")
FINAL = bytes.fromhex("55aa18000d01010100001ec301df00ef00ecc3")  # 78.75 kg, 479 Ω
FINAL_NO_BIA = bytes.fromhex("55aa18000d010102000028aa0000013e000039")  # 104.10 kg
FINAL_ZERO_CURRENT = bytes.fromhex(
    "55aa18000d110109000027a60000000000000c"
)  # 101.50 kg


def _settling(centigrams: int, status: int = 0x00) -> bytes:
    payload = bytes([status]) + centigrams.to_bytes(4, "big") + b"\x00\x00"
    head = b"\x55\xaa\x14\x00\x07" + payload
    return head + bytes([sum(head) & 0xFF])


def _feed(scale: Renpho55AAScale, *frames: bytes) -> None:
    for frame in frames:
        scale._notification_handler(MagicMock(), bytearray(frame), NAME, ADDRESS)


def _make_scale(
    model_id: int | None = MODEL_ESCS20MB2, **kwargs
) -> tuple[Renpho55AAScale, MagicMock]:
    callback = MagicMock()
    scale = Renpho55AAScale(
        ADDRESS,
        callback,
        bleak_scanner_backend=MagicMock(),
        model_id=model_id,
        **kwargs,
    )
    scale._clock = lambda: FIXED_NOW
    scale._fallback_delay_seconds = 0.01
    return scale, callback


def _make_client(present_uuids: frozenset[str] = _ALL_CHARS) -> MagicMock:
    client = MagicMock(name="client")
    chars = {uuid: MagicMock(name=uuid) for uuid in present_uuids}
    client.services.get_characteristic.side_effect = lambda uuid: chars.get(str(uuid))
    client.start_notify = AsyncMock()
    client.write_gatt_char = AsyncMock()
    client.chars = chars
    return client


async def _settle(scale: Renpho55AAScale) -> None:
    """Let every queued background write run to completion.

    Cancellation is expected (a task the client deliberately cancelled), a
    raised exception never is.
    """
    if scale._bg_tasks:
        results = await asyncio.gather(*list(scale._bg_tasks), return_exceptions=True)
        assert not any(isinstance(r, Exception) for r in results)


async def _start(scale: Renpho55AAScale, client: MagicMock) -> None:
    scale._client = client
    scale._populate_device_metadata = AsyncMock()
    ble_device = MagicMock(name="ble_device")
    ble_device.name = NAME
    ble_device.address = ADDRESS
    await scale._start_scale_session(ble_device)
    await _settle(scale)


def _writes(client: MagicMock) -> list[bytes]:
    return [bytes(c.args[1]) for c in client.write_gatt_char.await_args_list]


# ---- connect-time writes ---------------------------------------------------


@pytest.mark.asyncio
async def test_fixed_profile_writes_set_time_profile_then_unit_in_the_apps_order():
    scale, _ = _make_scale(profile=L0G1C5)
    client = _make_client()
    await _start(scale, client)

    # L0G1C5 carries no last weight, so the connect-time write uses the
    # library's neutral constant (no weight is known yet).
    profile_frame = build_guest_profile_command(L0G1C5, fallback_last_weight_kg=70.0)
    assert _writes(client) == [SET_TIME, profile_frame, UNIT_KG]
    for call in client.write_gatt_char.await_args_list:
        assert call.args[0] is client.chars[X55AA_COMMAND_CHARACTERISTIC_UUID]
        assert call.kwargs.get("response") is True


@pytest.mark.asyncio
async def test_fixed_profile_with_a_last_weight_reproduces_the_apps_frame():
    scale, _ = _make_scale(profile=dataclasses.replace(L0G1C5, last_weight_kg=88.40))
    client = _make_client()
    await _start(scale, client)
    assert _writes(client) == [SET_TIME, PROFILE_L0G1C5_88_40, UNIT_KG]


@pytest.mark.asyncio
async def test_weight_only_mode_writes_the_placeholder_profile_at_connect():
    """No profile → a placeholder guest profile that switches the impedance
    pass off, so the scale shows weight only rather than body composition
    derived from a profile belonging to nobody."""
    scale, _ = _make_scale(profile=None)
    client = _make_client()
    await _start(scale, client)

    writes = _writes(client)
    assert writes[0] == SET_TIME and writes[-1] == UNIT_KG and len(writes) == 3
    placeholder = writes[1]
    assert placeholder[2] == 0x96
    assert placeholder[5] & 0x0F == 9 and placeholder[17] == 0xFF
    assert placeholder[18] == 0x06  # impedance pass off


@pytest.mark.asyncio
async def test_resolver_mode_defers_the_profile_write():
    resolver = AsyncMock(return_value=L0G1C5)
    scale, _ = _make_scale(profile=resolver)
    client = _make_client()
    await _start(scale, client)

    assert _writes(client) == [SET_TIME, UNIT_KG]
    resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_sibling_model_0x30_is_treated_as_extended():
    scale, _ = _make_scale(model_id=MODEL_SIBLING, profile=L0G1C5)
    client = _make_client()
    await _start(scale, client)
    assert _writes(client)[0] == SET_TIME and len(_writes(client)) == 3


@pytest.mark.asyncio
async def test_basic_model_ignores_profile_and_writes_only_the_unit(caplog):
    with caplog.at_level(logging.WARNING):
        scale, _ = _make_scale(model_id=0x0003, profile=L0G1C5)
    client = _make_client()
    await _start(scale, client)
    assert _writes(client) == [UNIT_KG]
    assert any("basic flavor" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_unknown_model_id_defaults_to_basic():
    scale, _ = _make_scale(model_id=None)
    client = _make_client()
    await _start(scale, client)
    assert _writes(client) == [UNIT_KG]


def test_a_profile_the_scale_cannot_take_is_rejected_at_construction():
    with pytest.raises(ValueError):
        _make_scale(profile=dataclasses.replace(L0G1C5, algorithm=0x05))


def test_profile_type_is_validated():
    with pytest.raises(TypeError):
        Renpho55AAScale(
            ADDRESS,
            MagicMock(),
            bleak_scanner_backend=MagicMock(),
            model_id=MODEL_ESCS20MB2,
            profile="nope",  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_set_time_uses_the_current_utc_epoch_and_local_offset():
    scale, _ = _make_scale(profile=L0G1C5)
    scale._clock = lambda: datetime.datetime(
        2026, 9, 11, 1, 2, 3, tzinfo=datetime.timezone(datetime.timedelta(hours=-5))
    )
    client = _make_client()
    await _start(scale, client)
    p = _writes(client)[0][5:-1]
    assert int.from_bytes(p[1:7], "big") == int(scale._clock().timestamp())
    assert p[7:9] == bytes([1, 5])


@pytest.mark.asyncio
async def test_forbidden_frame_is_refused_before_the_link_and_logged(caplog):
    """Defence in depth: the writer refuses anything off the allow-list."""
    scale, _ = _make_scale(profile=L0G1C5)
    client = _make_client()
    await _start(scale, client)
    client.write_gatt_char.reset_mock()
    bad = bytes.fromhex("55aa9000040100020097")  # 0x90 mode 2
    with caplog.at_level(logging.ERROR):
        await scale._safe_write(bad, "test frame")
    client.write_gatt_char.assert_not_awaited()
    assert any("refused" in r.message for r in caplog.records)


# ---- 0x18 finals -----------------------------------------------------------


@pytest.mark.asyncio
async def test_extended_final_reports_weight_and_resistance_only():
    """On-device BMI/body fat are not reported: they reflect whatever profile
    the scale held at commit time, which the client cannot vouch for."""
    scale, callback = _make_scale(profile=L0G1C5)
    client = _make_client()
    await _start(scale, client)

    _feed(scale, STATUS_ON, _settling(7875), _settling(7875), _settling(7875), FINAL)

    callback.assert_called_once()
    data = callback.call_args[0][0]
    assert data.measurements == {WEIGHT_KEY: 78.75, RESISTANCE_1_KEY: 479}
    assert data.address == ADDRESS and data.name == NAME


@pytest.mark.asyncio
async def test_extended_final_without_impedance_is_weight_only():
    scale, callback = _make_scale(profile=L0G1C5)
    client = _make_client()
    await _start(scale, client)
    _feed(scale, FINAL_NO_BIA)
    callback.assert_called_once()
    assert callback.call_args[0][0].measurements == {WEIGHT_KEY: 104.10}


@pytest.mark.asyncio
async def test_extended_zero_current_final_is_weight_only_with_one_info_line(caplog):
    scale, callback = _make_scale(profile=L0G1C5)
    client = _make_client()
    await _start(scale, client)
    with caplog.at_level(logging.INFO):
        _feed(
            scale, _settling(10150, status=0x10), FINAL_ZERO_CURRENT, FINAL_ZERO_CURRENT
        )
    callback.assert_called_once()
    assert callback.call_args[0][0].measurements == {WEIGHT_KEY: 101.50}
    infos = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and "zero-current" in r.message
    ]
    assert len(infos) == 1
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.asyncio
async def test_identical_repeated_extended_final_is_ignored_and_a_differing_one_warns(
    caplog,
):
    scale, callback = _make_scale(profile=L0G1C5)
    client = _make_client()
    await _start(scale, client)
    with caplog.at_level(logging.WARNING):
        _feed(scale, FINAL, FINAL)  # hardware repeat
        _feed(scale, FINAL_NO_BIA)  # a different final without a new settling phase
    callback.assert_called_once()
    assert sum("differs" in r.message for r in caplog.records) == 1


@pytest.mark.asyncio
async def test_a_0x14_final_status_on_the_extended_flavor_is_not_a_reading(caplog):
    """Never observed on this flavor; if a firmware ever sends one, surface it
    once rather than report a possibly incomplete weight."""
    scale, callback = _make_scale(profile=L0G1C5)
    client = _make_client()
    await _start(scale, client)
    with caplog.at_level(logging.WARNING):
        _feed(scale, _settling(7875, status=0x01), _settling(7875, status=0x01))
    callback.assert_not_called()
    assert (
        sum("0x14" in r.message and "final" in r.message for r in caplog.records) == 1
    )


@pytest.mark.asyncio
async def test_unsupported_0x14_status_on_the_extended_flavor_warns_once(caplog):
    scale, callback = _make_scale(profile=L0G1C5)
    client = _make_client()
    await _start(scale, client)
    with caplog.at_level(logging.WARNING):
        _feed(scale, _settling(7875, status=0x02), _settling(7875, status=0x02))
    callback.assert_not_called()
    assert (
        sum("unsupported measurement status" in r.message for r in caplog.records) == 1
    )


@pytest.mark.asyncio
async def test_power_off_echo_of_the_final_weight_does_not_rearm():
    """Every captured session ends final → power-off status → one settling
    frame repeating the final's weight; that echo is not a new weigh-in."""
    scale, callback = _make_scale(profile=L0G1C5)
    client = _make_client()
    await _start(scale, client)
    _feed(scale, STATUS_ON, _settling(130), _settling(7875), _settling(7875), FINAL)
    _feed(scale, STATUS_OFF, _settling(7875), FINAL)  # echo, then a hypothetical repeat
    callback.assert_called_once()
    assert scale._final_fired


@pytest.mark.asyncio
async def test_new_settling_phase_rearms_the_extended_final():
    scale, callback = _make_scale(profile=L0G1C5)
    client = _make_client()
    await _start(scale, client)
    _feed(scale, FINAL, _settling(5000), FINAL_NO_BIA)
    assert callback.call_count == 2


@pytest.mark.asyncio
async def test_0x18_on_a_basic_model_is_delivered_with_a_report_it_warning(caplog):
    """An 0x18 can only mean the model id was wrong or a new extended id; the
    weight is still real."""
    scale, callback = _make_scale(model_id=0x0003)
    client = _make_client()
    await _start(scale, client)
    with caplog.at_level(logging.WARNING):
        _feed(scale, FINAL, FINAL)
    callback.assert_called_once()
    assert sum("not registered as extended" in r.message for r in caplog.records) == 1


@pytest.mark.asyncio
async def test_profile_and_settings_acks_are_debug_logged_not_warned(caplog):
    scale, callback = _make_scale(profile=L0G1C5)
    client = _make_client()
    await _start(scale, client)
    ack_96 = bytes.fromhex("55aa160001091f")
    ack_97 = bytes.fromhex("55aa17000201011a")
    with caplog.at_level(logging.DEBUG):
        _feed(scale, ack_96, ack_97)
    callback.assert_not_called()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not any("unrecognized" in r.message for r in caplog.records)


# ---- user detection: resolver on client-side stability ---------------------


@pytest.mark.asyncio
async def test_resolver_is_called_once_with_the_settled_weight_and_its_profile_is_written():
    resolver = AsyncMock(return_value=L0G1C5)
    scale, _ = _make_scale(profile=resolver)
    client = _make_client()
    await _start(scale, client)

    # Partial steps, then three identical frames = stable.
    _feed(scale, _settling(420), _settling(5250), _settling(8810), _settling(8840))
    await _settle(scale)
    resolver.assert_not_awaited()
    _feed(scale, _settling(8840), _settling(8840))
    await _settle(scale)

    resolver.assert_awaited_once_with(88.40)
    # The resolved profile has no last weight, so the settled weight fills the field.
    assert _writes(client) == [SET_TIME, UNIT_KG, PROFILE_L0G1C5_88_40]

    # More settling frames after stability do not re-resolve.
    _feed(scale, _settling(8840), _settling(8845), _settling(8840))
    await _settle(scale)
    resolver.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_resolved_profiles_own_last_weight_wins_over_the_settled_weight():
    resolver = AsyncMock(return_value=dataclasses.replace(L0G1C5, last_weight_kg=93.40))
    scale, _ = _make_scale(profile=resolver)
    client = _make_client()
    await _start(scale, client)
    _feed(scale, _settling(8840), _settling(8840), _settling(8840))
    await _settle(scale)
    assert int.from_bytes(_writes(client)[2][12:16], "big") == 9340


@pytest.mark.asyncio
async def test_two_identical_frames_are_not_yet_stable():
    resolver = AsyncMock(return_value=L0G1C5)
    scale, _ = _make_scale(profile=resolver)
    client = _make_client()
    await _start(scale, client)
    _feed(
        scale,
        _settling(10120),
        _settling(10120),
        _settling(10280),
        _settling(10280),
        _settling(9875),
    )
    await _settle(scale)
    resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_zero_weight_frames_never_count_as_stable():
    resolver = AsyncMock(return_value=L0G1C5)
    scale, _ = _make_scale(profile=resolver)
    client = _make_client()
    await _start(scale, client)
    _feed(scale, _settling(0), _settling(0), _settling(0), _settling(0))
    await _settle(scale)
    resolver.assert_not_awaited()


async def _writes_after_plateau(
    resolver,
    caplog,
    caplog_level=logging.DEBUG,
    *,
    deadline_seconds=None,
) -> list[bytes]:
    """Connect in user-detection mode, hold one plateau, return the writes.

    The four fallback paths differ only in the resolver they are given and
    the log line it is expected to leave behind.
    """
    scale, _ = _make_scale(profile=resolver)
    if deadline_seconds is not None:
        scale._resolver_deadline_seconds = deadline_seconds
    client = _make_client()
    await _start(scale, client)
    with caplog.at_level(caplog_level):
        _feed(scale, _settling(8840), _settling(8840), _settling(8840))
        await asyncio.wait_for(_settle(scale), 1)
    return _writes(client)


@pytest.mark.asyncio
async def test_resolver_none_falls_back_to_the_placeholder(caplog):
    writes = await _writes_after_plateau(AsyncMock(return_value=None), caplog)
    assert len(writes) == 3 and writes[2][2] == 0x96
    assert writes[2][5] & 0x0F == 9 and writes[2][18] & 0x03 == 1
    assert (
        int.from_bytes(writes[2][12:16], "big") == 8840
    )  # settled weight as last weight


@pytest.mark.asyncio
async def test_resolver_exception_falls_back_to_the_placeholder(caplog):
    writes = await _writes_after_plateau(
        AsyncMock(side_effect=RuntimeError("boom")), caplog, logging.ERROR
    )
    assert len(writes) == 3 and writes[2][2] == 0x96
    assert any("resolver raised" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_resolver_deadline_falls_back_to_the_placeholder(caplog):
    """The scale powers off ~8 s after settling with no profile; a slow
    resolver must not cost the reading."""

    async def slow(_weight):
        await asyncio.Event().wait()

    writes = await _writes_after_plateau(
        slow, caplog, logging.WARNING, deadline_seconds=0.01
    )
    assert len(writes) == 3 and writes[2][2] == 0x96
    assert any("did not answer" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_resolver_profile_that_cannot_be_encoded_falls_back_to_the_placeholder(
    caplog,
):
    writes = await _writes_after_plateau(
        AsyncMock(return_value=dataclasses.replace(L0G1C5, algorithm=0x05)),
        caplog,
        logging.ERROR,
    )
    assert len(writes) == 3 and writes[2][2] == 0x96
    assert int.from_bytes(writes[2][6:8], "big") == 1985  # placeholder birth year
    assert any("could not encode" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_resolver_returning_a_non_profile_falls_back_to_the_placeholder(caplog):
    """A duck-typed answer is rejected on type, not on the fields it happens
    to carry: only an X55AAProfile is known to encode the way the wire wants."""
    impostor = types.SimpleNamespace(
        sex=Sex.Male,
        birthday=datetime.date(1990, 1, 1),
        height_m=1.70,
        athlete=False,
        algorithm=0x03,
        last_weight_kg=None,
    )
    writes = await _writes_after_plateau(
        AsyncMock(return_value=impostor), caplog, logging.ERROR
    )
    assert len(writes) == 3 and writes[2][2] == 0x96
    assert int.from_bytes(writes[2][6:8], "big") == 1985  # placeholder birth year
    assert any("not an X55AAProfile" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_resolver_runs_again_for_a_second_weigh_in_on_the_same_link():
    resolver = AsyncMock(return_value=L0G1C5)
    scale, callback = _make_scale(profile=resolver)
    client = _make_client()
    await _start(scale, client)
    _feed(scale, _settling(8840), _settling(8840), _settling(8840))
    await _settle(scale)
    _feed(scale, FINAL)
    _feed(scale, _settling(7000), _settling(7875), _settling(7875), _settling(7875))
    await _settle(scale)
    assert resolver.await_count == 2
    assert resolver.await_args_list[1].args == (78.75,)
    assert sum(1 for w in _writes(client) if w[2] == 0x96) == 2


@pytest.mark.asyncio
async def test_fixed_profile_is_resent_on_a_second_weigh_in_on_the_same_link():
    scale, _ = _make_scale(profile=L0G1C5)
    client = _make_client()
    await _start(scale, client)
    _feed(scale, FINAL, _settling(8840), _settling(8840), _settling(8840))
    await _settle(scale)
    profile_writes = [w for w in _writes(client) if w[2] == 0x96]
    assert profile_writes == [
        build_guest_profile_command(L0G1C5, 70.0),
        build_guest_profile_command(L0G1C5, 88.40),
    ]


@pytest.mark.asyncio
async def test_power_off_echo_does_not_resolve_again():
    resolver = AsyncMock(return_value=L0G1C5)
    scale, _ = _make_scale(profile=resolver)
    client = _make_client()
    await _start(scale, client)
    _feed(scale, _settling(7875), _settling(7875), _settling(7875))
    await _settle(scale)
    _feed(scale, FINAL, STATUS_OFF, _settling(7875))
    await _settle(scale)
    resolver.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolver_task_is_cancelled_when_the_link_drops():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def hang(_weight):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    scale, _ = _make_scale(profile=hang)
    client = _make_client()
    await _start(scale, client)
    _feed(scale, _settling(8840), _settling(8840), _settling(8840))
    await asyncio.wait_for(started.wait(), 1)
    scale._unavailable_callback(client)
    await asyncio.wait_for(cancelled.wait(), 1)
    assert sum(1 for w in _writes(client) if w[2] == 0x96) == 0


@pytest.mark.asyncio
async def test_unsupported_status_warnings_are_keyed_by_frame_type(caplog):
    """An unsupported status on a settling frame must not silence the same
    status on an 0x18, which is the one that costs a reading."""
    scale, _ = _make_scale(profile=L0G1C5)
    client = _make_client()
    await _start(scale, client)
    bad_final = bytearray(FINAL)
    bad_final[5] = 0x02
    bad_final[-1] = sum(bad_final[:-1]) & 0xFF
    with caplog.at_level(logging.WARNING):
        _feed(scale, _settling(7875, status=0x02), bytes(bad_final))
    assert sum("unsupported" in r.message for r in caplog.records) == 2


@pytest.mark.asyncio
async def test_a_plateau_during_the_connect_prelude_does_not_double_write_the_profile():
    """The prelude is a background task, so a plateau can be detected while it
    is still awaiting the set-time write. The plateau must not slip a second
    profile frame onto the wire ahead of the prelude's own."""
    entered = asyncio.Event()
    release = asyncio.Event()
    blocked = False

    async def gated_write(_char, _data, response=True):
        nonlocal blocked
        if not blocked:
            blocked = True
            entered.set()
            await release.wait()

    client = _make_client()
    client.write_gatt_char = AsyncMock(side_effect=gated_write)
    scale, _ = _make_scale(profile=L0G1C5)
    scale._client = client
    scale._populate_device_metadata = AsyncMock()
    ble_device = MagicMock(name="ble_device")
    ble_device.name = NAME
    ble_device.address = ADDRESS
    await scale._start_scale_session(ble_device)
    await asyncio.wait_for(entered.wait(), 1)

    _feed(scale, _settling(8840), _settling(8840), _settling(8840))
    release.set()
    await asyncio.wait_for(_settle(scale), 1)

    # Exactly the prelude's frame, with the neutral fallback weight.
    assert [w for w in _writes(client) if w[2] == 0x96] == [
        build_guest_profile_command(L0G1C5, 70.0)
    ]


@pytest.mark.asyncio
async def test_a_new_weigh_in_cancels_a_resolver_still_working_on_the_previous_one():
    """A late answer would carry the previous weigh-in's weight; the new
    plateau resolves for itself."""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def hang(_weight):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    scale, _ = _make_scale(profile=hang)
    client = _make_client()
    await _start(scale, client)
    _feed(scale, _settling(8840), _settling(8840), _settling(8840))
    await asyncio.wait_for(started.wait(), 1)

    _feed(scale, FINAL, _settling(7000), _settling(7875), _settling(7875))
    await asyncio.wait_for(cancelled.wait(), 1)
    assert sum(1 for w in _writes(client) if w[2] == 0x96) == 0
    scale._unavailable_callback(client)  # cancel the second weigh-in's resolver


@pytest.mark.asyncio
async def test_resolver_is_not_called_when_the_command_characteristic_is_missing():
    """Nothing can be written, so there is no user to resolve for."""
    resolver = AsyncMock(return_value=L0G1C5)
    scale, _ = _make_scale(profile=resolver)
    client = _make_client(frozenset({X55AA_NOTIFY_CHARACTERISTIC_UUID}))
    await _start(scale, client)
    _feed(scale, _settling(8840), _settling(8840), _settling(8840))
    await asyncio.wait_for(_settle(scale), 1)
    resolver.assert_not_awaited()
    client.write_gatt_char.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_resolver_on_a_basic_model_is_never_called():
    """The basic flavor takes no profile over BLE, so it never resolves a
    user — its settling frames do not even track stability."""
    resolver = AsyncMock(return_value=L0G1C5)
    scale, _ = _make_scale(model_id=0x0003, profile=resolver)
    client = _make_client()
    await _start(scale, client)
    _feed(scale, _settling(8840), _settling(8840), _settling(8840))
    await asyncio.wait_for(_settle(scale), 1)
    resolver.assert_not_awaited()
    assert _writes(client) == [UNIT_KG]


# ---- shutdown fallback -----------------------------------------------------


@pytest.mark.asyncio
async def test_power_off_without_a_final_reports_the_settled_weight(caplog):
    """A scale with no registered user may never release a final for a late
    guest profile; the settled weight is real and must not be lost."""
    scale, callback = _make_scale(profile=L0G1C5)
    client = _make_client()
    await _start(scale, client)
    with caplog.at_level(logging.WARNING):
        _feed(
            scale,
            STATUS_ON,
            _settling(10120),
            _settling(10120),
            _settling(10120),
            STATUS_OFF,
        )
        await asyncio.wait_for(_settle(scale), 1)
    callback.assert_called_once()
    assert callback.call_args[0][0].measurements == {WEIGHT_KEY: 101.20}
    assert any("powered off without a final" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_power_off_after_a_final_is_quiet(caplog):
    scale, callback = _make_scale(profile=L0G1C5)
    client = _make_client()
    await _start(scale, client)
    with caplog.at_level(logging.WARNING):
        _feed(
            scale, _settling(7875), _settling(7875), _settling(7875), FINAL, STATUS_OFF
        )
        await asyncio.wait_for(_settle(scale), 1)
    callback.assert_called_once()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.asyncio
async def test_power_off_without_a_settled_weight_reports_nothing():
    scale, callback = _make_scale(profile=L0G1C5)
    client = _make_client()
    await _start(scale, client)
    _feed(scale, _settling(500), _settling(4200), STATUS_OFF)
    await asyncio.wait_for(_settle(scale), 1)
    callback.assert_not_called()


@pytest.mark.asyncio
async def test_power_off_fallback_fires_once_per_weigh_in():
    scale, callback = _make_scale(profile=L0G1C5)
    client = _make_client()
    await _start(scale, client)
    _feed(
        scale,
        _settling(10120),
        _settling(10120),
        _settling(10120),
        STATUS_OFF,
        STATUS_OFF,
    )
    await asyncio.wait_for(_settle(scale), 1)
    callback.assert_called_once()


@pytest.mark.asyncio
async def test_basic_flavor_power_off_is_unchanged():
    scale, callback = _make_scale(model_id=0x0003)
    client = _make_client()
    await _start(scale, client)
    # The basic flavor never tracks stability, so hand it a settled weight:
    # only the flavor gate can keep the fallback from firing here.
    scale._stable_weight = 70.25
    _feed(
        scale,
        _settling(7025),
        _settling(7025),
        _settling(7025),
        bytes.fromhex("55aa110005000101000017"),
    )
    await asyncio.wait_for(_settle(scale), 1)
    callback.assert_not_called()


@pytest.mark.asyncio
async def test_a_late_final_wins_over_the_shutdown_fallback(caplog):
    """The link outlives the power-off status, so a final that is merely late
    must deliver its impedance — once, not as a duplicate of the fallback."""
    scale, callback = _make_scale(profile=L0G1C5)
    client = _make_client()
    await _start(scale, client)
    with caplog.at_level(logging.WARNING):
        _feed(scale, _settling(7875), _settling(7875), _settling(7875), STATUS_OFF)
        _feed(scale, FINAL)  # before the fallback deadline elapses
        await asyncio.sleep(0.05)
    callback.assert_called_once()
    assert callback.call_args[0][0].measurements == {
        WEIGHT_KEY: 78.75,
        RESISTANCE_1_KEY: 479,
    }
    assert not any("differs" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_the_shutdown_fallback_cancels_a_resolver_still_working():
    """The weigh-in is over: its profile would land on a scale already off."""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def hang(_weight):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    scale, callback = _make_scale(profile=hang)
    client = _make_client()
    await _start(scale, client)
    _feed(scale, _settling(8840), _settling(8840), _settling(8840))
    await asyncio.wait_for(started.wait(), 1)
    _feed(scale, STATUS_OFF)
    await asyncio.wait_for(cancelled.wait(), 1)
    callback.assert_called_once()
    assert callback.call_args[0][0].measurements == {WEIGHT_KEY: 88.40}
    assert sum(1 for w in _writes(client) if w[2] == 0x96) == 0


@pytest.mark.asyncio
async def test_the_shutdown_fallback_keeps_the_settled_weights_zero_current_mode(
    caplog,
):
    """No frame carries the mode once the scale is off, so it is latched with
    the settled weight."""
    scale, callback = _make_scale(profile=L0G1C5)
    client = _make_client()
    await _start(scale, client)
    with caplog.at_level(logging.INFO):
        _feed(
            scale,
            _settling(10150, status=0x10),
            _settling(10150, status=0x10),
            _settling(10150, status=0x10),
            STATUS_OFF,
        )
        await asyncio.wait_for(_settle(scale), 1)
    callback.assert_called_once()
    assert callback.call_args[0][0].measurements == {WEIGHT_KEY: 101.50}
    assert sum("zero-current" in r.message for r in caplog.records) == 1


@pytest.mark.asyncio
async def test_the_shutdown_fallback_survives_the_link_dropping():
    """It is the only path left to the reading once the scale is off."""
    scale, callback = _make_scale(profile=L0G1C5)
    client = _make_client()
    await _start(scale, client)
    _feed(scale, _settling(10120), _settling(10120), _settling(10120), STATUS_OFF)
    scale._unavailable_callback(client)
    await asyncio.wait_for(_settle(scale), 1)
    callback.assert_called_once()
    assert callback.call_args[0][0].measurements == {WEIGHT_KEY: 101.20}


@pytest.mark.asyncio
async def test_a_settling_frame_cancels_a_pending_shutdown_fallback():
    """A settling frame after the power-off status proves the scale is still
    weighing, so the fallback must stand down and let the final arrive with
    its impedance."""
    scale, callback = _make_scale(profile=L0G1C5)
    client = _make_client()
    await _start(scale, client)
    _feed(scale, _settling(7875), _settling(7875), _settling(7875), STATUS_OFF)
    _feed(scale, _settling(7875))
    await asyncio.sleep(scale._fallback_delay_seconds * 5)
    callback.assert_not_called()

    _feed(scale, FINAL)
    callback.assert_called_once()
    assert callback.call_args[0][0].measurements == {
        WEIGHT_KEY: 78.75,
        RESISTANCE_1_KEY: 479,
    }


@pytest.mark.asyncio
async def test_a_final_after_the_fallback_fired_reports_the_grace_as_too_short(caplog):
    """Not a duplicate final: the scale's own, later than the grace allowed.
    The reading is already out, so the frame is still ignored — but the delay
    wants lengthening, which only a report can establish."""
    scale, callback = _make_scale(profile=L0G1C5)
    client = _make_client()
    await _start(scale, client)
    with caplog.at_level(logging.WARNING):
        _feed(scale, _settling(7875), _settling(7875), _settling(7875), STATUS_OFF)
        await asyncio.wait_for(_settle(scale), 1)
        callback.assert_called_once()
        _feed(scale, FINAL)

    callback.assert_called_once()
    assert callback.call_args[0][0].measurements == {WEIGHT_KEY: 78.75}
    assert sum("fallback grace" in r.message for r in caplog.records) == 1
    assert not any("differs" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_a_weigh_in_after_a_shutdown_fallback_still_reports():
    scale, callback = _make_scale(profile=L0G1C5)
    client = _make_client()
    await _start(scale, client)
    _feed(scale, _settling(10120), _settling(10120), _settling(10120), STATUS_OFF)
    await asyncio.wait_for(_settle(scale), 1)
    _feed(scale, _settling(7875), _settling(7875), _settling(7875), FINAL)
    await asyncio.wait_for(_settle(scale), 1)
    assert callback.call_count == 2
    assert callback.call_args[0][0].measurements == {
        WEIGHT_KEY: 78.75,
        RESISTANCE_1_KEY: 479,
    }


# ---- stored records --------------------------------------------------------

# Capture bytes: the 2026-09-06 app-sync session delivered three such records,
# each split exactly as ``AD 00 01`` + 17 bytes and ``AF 00 00`` + 9 bytes.
EXT_STORED = bytes.fromhex("55aa19001411020102000027a6000000000000000000000069" "78")
EXT_STORED_FRAG_1 = bytes.fromhex("ad0001") + EXT_STORED[:17]
EXT_STORED_FRAG_2 = bytes.fromhex("af0000") + EXT_STORED[17:]
EXT_STORED_ACK = bytes.fromhex("55aa990001019a")


@pytest.mark.asyncio
async def test_fragmented_stored_record_is_logged_and_discarded(caplog):
    scale, callback = _make_scale(profile=L0G1C5)
    client = _make_client()
    await _start(scale, client)
    with caplog.at_level(logging.DEBUG):
        _feed(scale, EXT_STORED_FRAG_1, EXT_STORED_FRAG_2, FINAL)
        await _settle(scale)
    callback.assert_called_once()  # the live final only
    assert EXT_STORED_ACK not in _writes(client)
    assert any(
        "stored offline record" in r.message and "105 seconds ago" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_clear_stored_measurements_acks_each_extended_record():
    scale, _ = _make_scale(profile=L0G1C5, clear_stored_measurements=True)
    client = _make_client()
    await _start(scale, client)
    _feed(
        scale,
        EXT_STORED_FRAG_1,
        EXT_STORED_FRAG_2,
        EXT_STORED_FRAG_1,
        EXT_STORED_FRAG_2,
    )
    await _settle(scale)
    assert _writes(client)[3:] == [EXT_STORED_ACK, EXT_STORED_ACK]


@pytest.mark.asyncio
async def test_stored_record_fragments_do_not_corrupt_a_following_final():
    scale, callback = _make_scale(profile=L0G1C5)
    client = _make_client()
    await _start(scale, client)
    _feed(
        scale, EXT_STORED_FRAG_1, EXT_STORED_FRAG_2 + FINAL
    )  # final in the same notification
    callback.assert_called_once()
    assert callback.call_args[0][0].measurements == {
        WEIGHT_KEY: 78.75,
        RESISTANCE_1_KEY: 479,
    }


@pytest.mark.asyncio
async def test_unknown_length_stored_record_is_reported_once(caplog):
    """A 0x19 of another length is a layout we have not captured; say so
    once rather than decode a plausible-looking wrong record."""
    scale, _ = _make_scale(profile=L0G1C5)
    client = _make_client()
    await _start(scale, client)
    head = EXT_STORED[:24]  # 19-byte payload
    head = head[:3] + (19).to_bytes(2, "big") + head[5:]
    odd = head + bytes([sum(head) & 0xFF])
    with caplog.at_level(logging.WARNING):
        _feed(scale, odd, odd)
    assert (
        sum("unrecognised stored-record length" in r.message for r in caplog.records)
        == 1
    )


# ---- model id learned from the advertisement -------------------------------

# Capture-shaped advertisement payloads: 00 04 prefix, model id, then this
# scale's MAC in forward order and the trailing bytes.
ADV_EXTENDED = bytes.fromhex("00040031cfea020793670109")
ADV_BASIC = bytes.fromhex("00040003cfea020793670109")


async def _advertise(
    scale: Renpho55AAScale, payload: bytes, address: str = ADDRESS
) -> None:
    """Hand the client one advertisement, without letting it connect."""
    device = MagicMock(name="ble_device")
    device.name = NAME
    device.address = address
    advertisement = MagicMock(name="advertisement")
    advertisement.manufacturer_data = {MANUFACTURER_ID: payload}
    with patch.object(GattScale, "_handle_advertisement", new=AsyncMock()) as connect:
        await scale._handle_advertisement(device, advertisement)
    connect.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_extended_advertisement_selects_the_extended_flavor(caplog):
    scale, _ = _make_scale(model_id=None, profile=L0G1C5)
    with caplog.at_level(logging.INFO):
        await _advertise(scale, ADV_EXTENDED)
    assert scale._model_id == MODEL_ESCS20MB2
    assert scale._extended is True
    assert any(
        "advertises model id 0x0031 (extended flavor)" in r.getMessage()
        for r in caplog.records
    )

    client = _make_client()
    await _start(scale, client)
    profile_frame = build_guest_profile_command(L0G1C5, fallback_last_weight_kg=70.0)
    assert _writes(client) == [SET_TIME, profile_frame, UNIT_KG]


@pytest.mark.asyncio
async def test_a_basic_advertisement_keeps_the_basic_flavor():
    scale, _ = _make_scale(model_id=None, profile=L0G1C5)
    await _advertise(scale, ADV_BASIC)
    assert scale._model_id == 0x0003
    assert scale._extended is False

    client = _make_client()
    await _start(scale, client)
    assert _writes(client) == [UNIT_KG]


@pytest.mark.asyncio
async def test_a_given_model_id_wins_over_the_advertised_one_and_warns_once(caplog):
    scale, _ = _make_scale(model_id=MODEL_ESCS20MB2, profile=L0G1C5)
    with caplog.at_level(logging.WARNING):
        await _advertise(scale, ADV_BASIC)
        await _advertise(scale, ADV_BASIC)
    assert scale._model_id == MODEL_ESCS20MB2
    assert scale._extended is True
    assert sum("advertises model id" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.asyncio
async def test_an_advertisement_from_another_address_is_ignored():
    scale, _ = _make_scale(model_id=None)
    await _advertise(scale, ADV_EXTENDED, address="AA:BB:CC:DD:EE:FF")
    assert scale._model_id is None
    assert scale._extended is False


@pytest.mark.asyncio
async def test_an_omitted_model_id_does_not_warn_about_the_profile(caplog):
    """The flavor is not known yet at construction, so a profile is not
    (necessarily) pointless — only an explicit basic model id makes it so."""
    with caplog.at_level(logging.WARNING):
        _make_scale(model_id=None, profile=L0G1C5)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.asyncio
async def test_a_profile_on_an_explicit_basic_model_id_still_warns(caplog):
    with caplog.at_level(logging.WARNING):
        _make_scale(model_id=0x0003, profile=L0G1C5)
    assert any("basic flavor" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_late_final_without_impedance_still_reports_the_grace_as_too_short(
    caplog,
):
    """The fallback reports (weight, 0), and a genuine zero-current or
    socks-on final carries exactly that pair, so the diagnostic cannot key on
    the contents of the frame."""
    scale, callback = _make_scale(profile=L0G1C5)
    client = _make_client()
    await _start(scale, client)
    with caplog.at_level(logging.WARNING):
        _feed(
            scale,
            _settling(10150, status=0x10),
            _settling(10150, status=0x10),
            _settling(10150, status=0x10),
            STATUS_OFF,
        )
        await asyncio.wait_for(_settle(scale), 1)
        _feed(scale, FINAL_ZERO_CURRENT)
        _feed(scale, FINAL_ZERO_CURRENT)  # the hardware repeat: not a second report
    callback.assert_called_once()
    assert callback.call_args[0][0].measurements == {WEIGHT_KEY: 101.50}
    assert sum("fallback grace" in r.message for r in caplog.records) == 1


@pytest.mark.asyncio
async def test_a_second_plateau_before_any_final_supersedes_the_first():
    """The scale may never finalise, so the fallback has to report the weight
    that was on it last rather than the first one to settle."""
    scale, callback = _make_scale(profile=L0G1C5)
    client = _make_client()
    await _start(scale, client)
    _feed(scale, _settling(7500), _settling(7500), _settling(7500))
    _feed(scale, _settling(6000), _settling(6000), _settling(6000))
    _feed(scale, STATUS_OFF)
    await asyncio.wait_for(_settle(scale), 1)
    callback.assert_called_once()
    assert callback.call_args[0][0].measurements == {WEIGHT_KEY: 60.00}


@pytest.mark.asyncio
async def test_a_prelude_that_raises_still_writes_the_display_unit(caplog):
    """Whatever the connect-time frames fail on, the session must not be left
    without its display-unit write, and the failure must reach this logger."""
    scale, _ = _make_scale(profile=L0G1C5)
    scale._clock = MagicMock(side_effect=RuntimeError("clock unavailable"))
    client = _make_client()
    with caplog.at_level(logging.ERROR):
        await _start(scale, client)
    assert _writes(client) == [UNIT_KG]
    assert any("connect-time frames" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_a_fixed_profile_keeps_the_impedance_pass_on():
    scale, _ = _make_scale(profile=L0G1C5)
    client = _make_client()
    await _start(scale, client)
    assert _writes(client)[1][18] == 0x05


@pytest.mark.asyncio
async def test_the_resolver_fallback_placeholder_keeps_the_impedance_pass_on():
    """Unlike weight-only mode: this reading may still be assigned to a user
    afterwards, and the resistance is what makes that possible."""
    resolver = AsyncMock(return_value=None)
    scale, _ = _make_scale(profile=resolver)
    client = _make_client()
    await _start(scale, client)
    _feed(scale, _settling(8840), _settling(8840), _settling(8840))
    await asyncio.wait_for(_settle(scale), 1)
    assert _writes(client)[2][18] == 0x05


@pytest.mark.asyncio
async def test_weight_only_mode_keeps_the_impedance_pass_off_on_a_second_weigh_in():
    """The setting must not flip partway through a session the caller asked to
    be weight-only."""
    scale, _ = _make_scale(profile=None)
    client = _make_client()
    await _start(scale, client)
    _feed(scale, FINAL, _settling(8840), _settling(8840), _settling(8840))
    await asyncio.wait_for(_settle(scale), 1)
    profiles = [w for w in _writes(client) if w[2] == 0x96]
    assert len(profiles) == 2
    assert all(w[18] == 0x06 for w in profiles)


@pytest.mark.asyncio
async def test_a_resting_offset_does_not_spend_the_weigh_ins_profile_write():
    """Captured after a battery pull: a unit reported a steady 1.10 kg with
    nothing on it from the moment of connection. That is not a weigh-in, and
    resolving against it would leave the real one without a profile."""
    resolver = AsyncMock(return_value=L0G1C5)
    scale, _ = _make_scale(profile=resolver)
    client = _make_client()
    await _start(scale, client)

    _feed(scale, *[_settling(110, status=0x10)] * 4)
    await asyncio.wait_for(_settle(scale), 1)
    resolver.assert_not_awaited()
    assert not [w for w in _writes(client) if w[2] == 0x96]

    # The real weigh-in still gets it.
    _feed(scale, _settling(8840), _settling(8840), _settling(8840))
    await asyncio.wait_for(_settle(scale), 1)
    resolver.assert_awaited_once_with(88.40)
    assert [w for w in _writes(client) if w[2] == 0x96] == [PROFILE_L0G1C5_88_40]


@pytest.mark.asyncio
async def test_a_resting_offset_is_still_latched_for_the_shutdown_fallback():
    """The floor gates the profile write, not the weight: a scale that powers
    off without finalising still reports what was on it."""
    scale, callback = _make_scale(profile=None)
    client = _make_client()
    await _start(scale, client)
    _feed(scale, *[_settling(110, status=0x10)] * 3, STATUS_OFF)
    await asyncio.wait_for(_settle(scale), 1)
    callback.assert_called_once()
    assert callback.call_args[0][0].measurements == {WEIGHT_KEY: 1.10}


@pytest.mark.asyncio
async def test_a_resolved_profile_with_malformed_fields_falls_back(caplog):
    """The dataclass does not check field types, so a caller can hand back a
    real X55AAProfile the encoder cannot use. That must cost the on-device
    numbers, not the reading."""
    bad = dataclasses.replace(L0G1C5, birthday="1990-01-01")  # type: ignore[arg-type]
    resolver = AsyncMock(return_value=bad)
    scale, _ = _make_scale(profile=resolver)
    client = _make_client()
    await _start(scale, client)
    with caplog.at_level(logging.ERROR):
        _feed(scale, _settling(8840), _settling(8840), _settling(8840))
        await asyncio.wait_for(_settle(scale), 1)
    writes = _writes(client)
    assert len(writes) == 3 and writes[2][2] == 0x96
    assert int.from_bytes(writes[2][6:8], "big") == 1985  # the placeholder's year
    assert any("could not encode" in r.message for r in caplog.records)
