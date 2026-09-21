"""Tests for ``diagnostic_info`` — the shareable snapshot of what a scale
advertises and how its last session behaved.

Each protocol learns these facts differently: QN reads its model identifier
from the advertisement but its flavor only from the session's frames; 0x55aa
derives the flavor from the advertised model identifier; the 0xaabb
broadcast variant has no session at all.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from renpho_escs20m import Renpho55AAScale, RenphoAABBScale, RenphoQNScale
from renpho_escs20m.data import WeightUnit
from renpho_escs20m.detection import ScaleProtocol, model_label

ADDRESS = "FF:04:00:23:8C:24"
_MAC_REVERSED = bytes.fromhex("248c230004ff")
# QN payload: model identifier (BE16), 3 filler bytes, reversed MAC.
_QN_ADV_KNOWN = bytes.fromhex("099b") + bytes(3) + _MAC_REVERSED
_QN_ADV_UNKNOWN = bytes.fromhex("0abc") + bytes(3) + _MAC_REVERSED


def _qn_scale(**kwargs) -> RenphoQNScale:
    scale = RenphoQNScale(
        ADDRESS,
        MagicMock(),
        WeightUnit.KG,
        bleak_scanner_backend=MagicMock(),
        **kwargs,
    )
    scale._safe_write = AsyncMock()
    # Keep the advertisement from starting a real connection.
    scale._handle_advertisement = AsyncMock()
    return scale


def _adv(manufacturer_data, name="QN-Scale") -> SimpleNamespace:
    return SimpleNamespace(
        local_name=name,
        rssi=-61,
        service_uuids=["0000fff0-0000-1000-8000-00805f9b34fb"],
        manufacturer_data=manufacturer_data,
    )


def _device(address=ADDRESS) -> SimpleNamespace:
    return SimpleNamespace(address=address, name="QN-Scale")


def _feed(scale: RenphoQNScale, *frames: str) -> None:
    for frame in frames:
        scale._notification_handler(
            MagicMock(), bytearray.fromhex(frame), "QN-Scale", ADDRESS
        )


# --- model labels ----------------------------------------------------------


def test_model_label_known_unknown_and_absent():
    assert "QN-Scale" in model_label(ScaleProtocol.QN, 0x099B)
    assert model_label(ScaleProtocol.QN, 0x0ABC) == "unknown"
    assert model_label(ScaleProtocol.QN, None) is None
    assert "ESCS20MB2" in model_label(ScaleProtocol.X55AA, 0x0031)
    assert model_label(ScaleProtocol.X55AA, 0x0999) == "unknown"


# --- QN --------------------------------------------------------------------


def test_qn_before_any_advertisement_reports_nothing_observed():
    info = _qn_scale().diagnostic_info
    assert info["protocol"] == "qn"
    assert info["advertisement"] is None
    assert info["model_code"] is None
    assert info["flavor"] is None
    json.dumps(info)


@pytest.mark.asyncio
async def test_qn_advertisement_yields_model_code_and_label():
    scale = _qn_scale()
    await scale._advertisement_callback(_device(), _adv({0xFFFF: _QN_ADV_KNOWN}))

    info = scale.diagnostic_info
    assert info["model_code"] == "0x099b"
    assert "QN-Scale" in info["model_label"]
    assert info["advertisement"]["local_name"] == "QN-Scale"
    assert info["advertisement"]["rssi"] == -61
    json.dumps(info)


@pytest.mark.asyncio
async def test_qn_unregistered_model_code_is_labelled_unknown():
    scale = _qn_scale()
    await scale._advertisement_callback(_device(), _adv({0xFFFF: _QN_ADV_UNKNOWN}))
    info = scale.diagnostic_info
    assert info["model_code"] == "0x0abc"
    assert info["model_label"] == "unknown"


@pytest.mark.asyncio
async def test_embedded_mac_is_masked_but_oui_kept():
    """The snapshot is meant to be shared: the device-specific half of the
    MAC echoed inside the payload is masked, the OUI (used for model
    classification) stays."""
    scale = _qn_scale()
    await scale._advertisement_callback(_device(), _adv({0xFFFF: _QN_ADV_KNOWN}))
    payload_hex = scale.diagnostic_info["advertisement"]["manufacturer_data"]["0xffff"]
    assert "248c23" not in payload_hex
    assert payload_hex == "099b000000" + "xxxxxx" + "0004ff"


@pytest.mark.asyncio
async def test_advertisement_is_recorded_during_cooldown():
    scale = _qn_scale(cooldown_seconds=30)
    scale._cooldown_end_time = float("inf")
    await scale._advertisement_callback(_device(), _adv({0xFFFF: _QN_ADV_KNOWN}))
    scale._handle_advertisement.assert_not_awaited()
    assert scale.diagnostic_info["model_code"] == "0x099b"


@pytest.mark.asyncio
async def test_other_devices_advertisements_are_not_recorded():
    scale = _qn_scale()
    await scale._advertisement_callback(
        _device("00:11:22:33:44:55"), _adv({0xFFFF: _QN_ADV_KNOWN})
    )
    assert scale.diagnostic_info["advertisement"] is None


@pytest.mark.asyncio
async def test_sparse_advertisement_does_not_raise():
    """Recording sits on the path to a connection; it must tolerate an
    advertisement object with missing or empty fields."""
    scale = _qn_scale()
    await scale._advertisement_callback(
        _device(), SimpleNamespace(manufacturer_data=None)
    )
    scale._handle_advertisement.assert_awaited_once()
    info = scale.diagnostic_info
    assert info["advertisement"]["manufacturer_data"] == {}
    assert info["model_code"] is None


# --- QN session trace ------------------------------------------------------


def _frames(info: dict) -> list[dict]:
    return [e for e in info["trace"] if "data" in e]


def _qn_scale_with_client() -> RenphoQNScale:
    """A QN scale whose writes go through the real ``_safe_write``."""
    scale = RenphoQNScale(
        ADDRESS, MagicMock(), WeightUnit.KG, bleak_scanner_backend=MagicMock()
    )
    client = MagicMock()
    client.write_gatt_char = AsyncMock()
    scale._client = client
    return scale


@pytest.mark.asyncio
async def test_qn_trace_records_both_directions_in_order():
    """ES-30M (fw V20.0) field frames."""
    scale = _qn_scale_with_client()
    _feed(scale, "2106ff01547b")
    await asyncio.sleep(0)
    _feed(scale, "a10602fe01a8")

    frames = _frames(scale.diagnostic_info)
    assert [(e["dir"], e["data"][:6]) for e in frames] == [
        ("rx", "2106ff"),
        ("tx", "a00d02"),
        ("rx", "a10602"),
    ]
    assert all(isinstance(e["t"], float) for e in frames)
    json.dumps(scale.diagnostic_info)


@pytest.mark.asyncio
async def test_qn_trace_collapses_a_run_of_same_status_frames():
    """A weigh-in streams dozens of unstable frames that differ only in the
    weight; the run is kept as its first and last frame plus a count."""
    scale = _qn_scale()
    unstable = [
        "100efffe0023b9000000000000f7",
        "100efffe002400000000000000" + "3f",
        "100efffe002585000000000000c5",
    ]
    _feed(
        scale,
        *unstable,
        "100efffe012585000000000000c6",
        "100efff002258501fa01f80000ad",
    )
    await asyncio.sleep(0)

    frames = _frames(scale.diagnostic_info)
    assert len(frames) == 3
    run = frames[0]
    assert run["data"] == unstable[0]
    assert run["last"] == unstable[-1]
    assert run["count"] == 3
    assert "count" not in frames[1]
    assert frames[2]["data"] == "100efff002258501fa01f80000ad"


@pytest.mark.asyncio
async def test_qn_trace_masks_the_mac_echo():
    scale = _qn_scale()
    _feed(scale, "1212ff248c230004ff0117140000059f3000")
    await asyncio.sleep(0)
    assert _frames(scale.diagnostic_info)[0]["data"] == (
        "1212ff" + "xxxxxx" + "0004ff0117140000059f3000"
    )


@pytest.mark.asyncio
async def test_trace_is_capped_and_drops_the_oldest():
    scale = _qn_scale()
    cap = scale._trace.maxlen
    for i in range(cap + 10):
        # Distinct opcodes/payloads so nothing collapses.
        _feed(scale, f"7f05ff{i:04x}")
    frames = _frames(scale.diagnostic_info)
    assert len(frames) == cap
    assert frames[-1]["data"] == f"7f05ff{cap + 9:04x}"


@pytest.mark.asyncio
async def test_qn_resolver_outcome_is_traced():
    """Whether the detected user's profile was sent is the first question on
    a "no body composition" report, and no frame answers it."""
    scale = _qn_scale(profile=AsyncMock(return_value=None))
    _feed(scale, "100efffe012585000000000000c6")
    await scale._resolver_task
    events = [e["event"] for e in scale.diagnostic_info["trace"] if "event" in e]
    assert events == ["profile resolver returned none"]


@pytest.mark.asyncio
async def test_qn_flavor_and_vendor_byte_are_still_reported():
    scale = _qn_scale()
    _feed(scale, "120f15" + "00" * 12, "210515013c")
    info = scale.diagnostic_info
    assert info["flavor"] == "basic"
    assert info["vendor_byte"] == "0x15"
    for dropped in (
        "unit_frame",
        "pre_measurement_frame",
        "measurement_frame_length",
        "final_user_byte",
    ):
        assert dropped not in info


@pytest.mark.asyncio
async def test_qn_trace_spans_sessions_with_markers(monkeypatch):
    """A tap-to-wake or a reconnect must not erase the session that
    mattered, so the buffer is shared and sessions are marked."""
    scale = RenphoQNScale(
        ADDRESS, MagicMock(), WeightUnit.KG, bleak_scanner_backend=MagicMock()
    )
    scale._safe_write = AsyncMock()
    scale._populate_device_metadata = AsyncMock()
    # An earlier session's frames.
    _feed(scale, "120f15" + "00" * 12, "210515013c")

    client = MagicMock()
    client.is_connected = True
    client.start_notify = AsyncMock()
    monkeypatch.setattr(
        "renpho_escs20m.scale.establish_connection", AsyncMock(return_value=client)
    )
    await scale._advertisement_callback(_device(), _adv({0xFFFF: _QN_ADV_KNOWN}))
    scale._unavailable_callback(client)

    info = scale.diagnostic_info
    assert info["flavor"] == "basic"
    assert info["vendor_byte"] == "0x15"
    assert info["transport"] == "fff0"
    assert len(_frames(info)) == 2
    events = [e for e in info["trace"] if "event" in e]
    assert [e["event"] for e in events] == ["session start", "disconnected"]
    assert isinstance(events[0]["time"], int)


@pytest.mark.asyncio
async def test_failed_session_setup_is_traced(monkeypatch):
    """A scale whose GATT layout the library does not recognise never gets
    as far as a frame; the reason has to show up some other way."""
    scale = RenphoQNScale(
        ADDRESS, MagicMock(), WeightUnit.KG, bleak_scanner_backend=MagicMock()
    )
    scale._populate_device_metadata = AsyncMock()
    client = MagicMock()
    client.is_connected = True
    client.disconnect = AsyncMock()
    client.services.get_characteristic = MagicMock(return_value=None)
    monkeypatch.setattr(
        "renpho_escs20m.scale.establish_connection", AsyncMock(return_value=client)
    )
    await scale._advertisement_callback(_device(), _adv({0xFFFF: _QN_ADV_KNOWN}))

    events = [e["event"] for e in scale.diagnostic_info["trace"] if "event" in e]
    assert events[0] == "session start"
    assert events[1].startswith("session setup failed: ")
    assert "FFF1/FFE1" in events[1]


# --- 0x55aa ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_x55aa_flavor_comes_from_the_advertised_model_id():
    scale = Renpho55AAScale(ADDRESS, MagicMock(), bleak_scanner_backend=MagicMock())
    info = scale.diagnostic_info
    assert info["protocol"] == "x55aa"
    assert info["model_code"] is None
    assert info["flavor"] is None

    payload = bytes.fromhex("00040031") + bytes.fromhex("ff0400238c24") + b"004.022"
    scale._learn_model_id(_device(), _adv({0x1A10: payload}, name="Renpho"))

    info = scale.diagnostic_info
    assert info["model_code"] == "0x0031"
    assert info["flavor"] == "extended"
    assert "ESCS20MB2" in info["model_label"]
    json.dumps(info)


def test_x55aa_caller_supplied_model_id_is_reported():
    scale = Renpho55AAScale(
        ADDRESS, MagicMock(), bleak_scanner_backend=MagicMock(), model_id=0x0003
    )
    info = scale.diagnostic_info
    assert info["model_code"] == "0x0003"
    assert info["flavor"] == "basic"


# --- 0xaabb ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_aabb_reports_protocol_and_advertisement_only():
    scale = RenphoAABBScale(ADDRESS, MagicMock(), bleak_scanner_backend=MagicMock())
    payload = bytes.fromhex("aabb") + bytes.fromhex("ff0400238c24") + bytes(11)
    await scale._advertisement_callback(
        _device(), SimpleNamespace(manufacturer_data={0xFFFF: payload})
    )
    info = scale.diagnostic_info
    assert info["protocol"] == "aabb"
    assert info["model_code"] is None
    assert info["flavor"] is None
    assert (
        info["advertisement"]["manufacturer_data"]["0xffff"]
        == "aabb" + "ff0400" + "xxxxxx" + "00" * 11
    )
    json.dumps(info)


# --- 0x55aa / 0xaabb traces -------------------------------------------------


@pytest.mark.asyncio
async def test_x55aa_trace_records_raw_notifications_and_collapses_runs():
    scale = Renpho55AAScale(
        ADDRESS, MagicMock(), bleak_scanner_backend=MagicMock(), model_id=0x0003
    )
    live = [
        "55aa140007000000271002" + "8c" + "00",
        "55aa140007000000283c02" + "8c" + "00",
    ]
    final = "55aa14000701000028dc028cad"
    for frame in (*live, final):
        scale._notification_handler(
            MagicMock(), bytearray.fromhex(frame), "Renpho", ADDRESS
        )

    frames = _frames(scale.diagnostic_info)
    assert [e["dir"] for e in frames] == ["rx", "rx"]
    assert frames[0]["count"] == 2
    assert frames[0]["last"] == live[-1]
    assert frames[1]["data"] == final
    json.dumps(scale.diagnostic_info)


@pytest.mark.asyncio
async def test_aabb_trace_is_the_advertisement_burst_by_status():
    scale = RenphoAABBScale(ADDRESS, MagicMock(), bleak_scanner_backend=MagicMock())

    def adv(status: int, weight: int) -> SimpleNamespace:
        payload = bytearray(bytes.fromhex("aabb") + bytes.fromhex("ff0400238c24"))
        payload += bytes(7) + bytes([status]) + bytes(1) + weight.to_bytes(2, "little")
        return SimpleNamespace(manufacturer_data={0xFFFF: bytes(payload)})

    for status, weight in ((0x02, 100), (0x02, 7000), (0x02, 7050), (0x23, 7050)):
        await scale._advertisement_callback(_device(), adv(status, weight))

    frames = _frames(scale.diagnostic_info)
    assert [e["dir"] for e in frames] == ["adv", "adv"]
    assert frames[0]["count"] == 3
    assert "248c24" not in frames[0]["data"]


# --- masking edge cases ----------------------------------------------------

_UUID_ADDRESS = "E621E1F8-C36C-495A-93FC-0C247A3E6E5F"


@pytest.mark.asyncio
async def test_qn_mac_is_masked_when_the_address_is_not_a_mac():
    """macOS hands out a UUID, not the MAC. The payload still echoes the real
    MAC at a fixed place, so it is taken from there and masked everywhere."""
    scale = RenphoQNScale(_UUID_ADDRESS, MagicMock(), bleak_scanner_backend=MagicMock())
    scale._safe_write = AsyncMock()
    scale._handle_advertisement = AsyncMock()
    await scale._advertisement_callback(
        _device(_UUID_ADDRESS), _adv({0xFFFF: _QN_ADV_KNOWN})
    )
    scale._notification_handler(
        MagicMock(),
        bytearray.fromhex("1212ff248c230004ff0117140000059f3000"),
        "QN-Scale",
        _UUID_ADDRESS,
    )

    info = scale.diagnostic_info
    assert info["model_code"] == "0x099b"
    assert (
        info["advertisement"]["manufacturer_data"]["0xffff"]
        == "099b000000" + "xxxxxx" + "0004ff"
    )
    assert _frames(info)[0]["data"] == (
        "1212ff" + "xxxxxx" + "0004ff0117140000059f3000"
    )


@pytest.mark.asyncio
async def test_x55aa_mac_is_masked_when_the_address_is_not_a_mac():
    scale = Renpho55AAScale(
        _UUID_ADDRESS, MagicMock(), bleak_scanner_backend=MagicMock()
    )
    scale._handle_advertisement = AsyncMock()
    payload = bytes.fromhex("00040031") + bytes.fromhex("ff0400238c24") + b"004"
    await scale._advertisement_callback(_device(_UUID_ADDRESS), _adv({0x1A10: payload}))
    masked = scale.diagnostic_info["advertisement"]["manufacturer_data"]["0x1a10"]
    assert masked == "00040031" + "ff0400" + "xxxxxx" + b"004".hex()


@pytest.mark.asyncio
async def test_aabb_mac_is_masked_when_the_address_is_not_a_mac():
    scale = RenphoAABBScale(
        _UUID_ADDRESS, MagicMock(), bleak_scanner_backend=MagicMock()
    )
    payload = bytes.fromhex("aabb") + bytes.fromhex("ff0400238c24") + bytes(11)
    await scale._advertisement_callback(
        _device(_UUID_ADDRESS), SimpleNamespace(manufacturer_data={0xFFFF: payload})
    )
    masked = scale.diagnostic_info["advertisement"]["manufacturer_data"]["0xffff"]
    assert masked == "aabb" + "ff0400" + "xxxxxx" + "00" * 11


# --- model_label never raises ----------------------------------------------


def test_model_label_without_a_registry_is_none():
    assert model_label(ScaleProtocol.AABB, 0x1234) is None
    assert model_label("not-a-protocol", 0x1234) is None


# --- opt-in profile masking ------------------------------------------------
#
# The trace is verbatim by default. A consumer whose dumps get posted in
# public asks for the personal fields of outgoing profile frames to be
# masked — the frame itself stays, since *when* a profile was written is
# what the trace is for.


@pytest.mark.asyncio
async def test_qn_profile_frame_is_verbatim_by_default_and_masked_on_request():
    from renpho_escs20m import Profile, Sex

    scale = RenphoQNScale(
        ADDRESS,
        MagicMock(),
        WeightUnit.KG,
        profile=Profile(sex=Sex.Male, age=41, height_m=1.83, algorithm=0x03),
        bleak_scanner_backend=MagicMock(),
    )
    client = MagicMock()
    client.write_gatt_char = AsyncMock()
    scale._client = client
    _feed(scale, "2105ff0126")
    await asyncio.sleep(0)

    verbatim = _frames(scale.diagnostic_info)[1]["data"]
    assert verbatim.startswith("a00d02feffee") and "x" not in verbatim
    assert scale.diagnostic_info == scale.get_diagnostic_info()

    masked = _frames(scale.get_diagnostic_info(mask_profiles=True))
    # sex, age, height (2) masked; algorithm byte and trailer kept; checksum
    # masked, since it would otherwise give away the sum of the hidden bytes.
    assert masked[1]["data"] == "a00d02feffee" + "xxxxxxxx" + "0302" + "xx"
    assert masked[1]["dir"] == "tx"
    # Nothing else is touched.
    assert masked[0]["data"] == "2105ff0126"


@pytest.mark.asyncio
async def test_x55aa_profile_frame_masks_sex_birth_date_and_height():
    import datetime

    from renpho_escs20m import Sex, X55AAProfile, build_guest_profile_command

    scale = Renpho55AAScale(
        ADDRESS, MagicMock(), bleak_scanner_backend=MagicMock(), model_id=0x0031
    )
    client = MagicMock()
    client.write_gatt_char = AsyncMock()
    scale._client = client
    scale._command_char = MagicMock()
    frame = build_guest_profile_command(
        X55AAProfile(sex=Sex.Male, birthday=datetime.date(1985, 3, 14), height_m=1.83),
        80.0,
    )
    await scale._safe_write(frame, "resolved guest profile")

    verbatim = _frames(scale.diagnostic_info)[0]
    assert verbatim["data"] == frame.hex()
    assert verbatim["note"] == "resolved guest profile"

    masked = _frames(scale.get_diagnostic_info(mask_profiles=True))[0]["data"]
    raw = frame.hex()
    # Header kept; the sex nibble goes but the slot nibble beside it stays;
    # birth date (4) and height (2) go; last weight, flags and trailer stay.
    assert masked == (raw[:10] + "x" + raw[11] + "x" * 8 + "x" * 4 + raw[24:-2] + "xx")
    assert masked[11] == "9"  # guest slot


# --- 0x55aa resolver outcomes ----------------------------------------------
#
# A ``tx`` note says the placeholder went out, but not why. The reason is
# what separates a resolver bug from an honest "no match".


def _x55aa_resolving_scale(resolver) -> Renpho55AAScale:
    scale = Renpho55AAScale(
        ADDRESS,
        MagicMock(),
        bleak_scanner_backend=MagicMock(),
        model_id=0x0031,
        profile=resolver,
    )
    client = MagicMock()
    client.write_gatt_char = AsyncMock()
    scale._client = client
    scale._command_char = MagicMock()
    return scale


def _events(scale) -> list[str]:
    return [e["event"] for e in scale.diagnostic_info["trace"] if "event" in e]


@pytest.mark.asyncio
async def test_x55aa_resolver_returning_none_is_traced():
    scale = _x55aa_resolving_scale(AsyncMock(return_value=None))
    await scale._resolve_and_send_profile(80.0, ADDRESS, scale._client)
    assert _events(scale) == ["profile resolver returned none"]
    assert _frames(scale.diagnostic_info)[0]["note"] == "placeholder guest profile"


@pytest.mark.asyncio
async def test_x55aa_resolver_raising_is_traced():
    scale = _x55aa_resolving_scale(AsyncMock(side_effect=RuntimeError("boom")))
    await scale._resolve_and_send_profile(80.0, ADDRESS, scale._client)
    assert _events(scale) == ["profile resolver raised"]
    assert _frames(scale.diagnostic_info)[0]["note"] == "placeholder guest profile"
