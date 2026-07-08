"""Tests for the 0xaabb broadcast-only variant.

Golden vectors are real advertisement payloads captured from two scales (one
set to kg, one to lb), indexed from the ``aa bb`` prefix.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from renpho_escs20m import RenphoAABBScale, WeightUnit
from renpho_escs20m.const import WEIGHT_KEY
from renpho_escs20m.xaabb.protocol import (
    MANUFACTURER_ID,
    decode_display_unit,
    is_final,
    parse_broadcast,
)

# --- real captured frames (manufacturer-data payload, from the aabb prefix) ---

# kg scale (MAC ed:67:39:c5:cb:8f)
KG_MAC = "ed:67:39:c5:cb:8f"
KG_FINAL = bytes.fromhex("aabbed6739c5cb8f6d3e236a948b1f23008e214c0903f33e")  # 85.90 kg
KG_SETTLING = bytes.fromhex("aabbed6739c5cb8f6d3e236a948b1f0200a7214c0903ef3e")  # 0x02
KG_TARE = bytes.fromhex("aabbed6739c5cb8f5b3e236a948b1f030000004c0903ec3e")  # 0x03, 0

# lb scale (MAC ed:67:3b:1a:46:8d)
LB_MAC = "ed:67:3b:1a:46:8d"
LB_FINAL = bytes.fromhex("aabbed673b1a468d7986066a40b64b2500931c4c4403691f")  # 73.15 kg
# 0x64 provisional/held frame: sets the 0x20 stable bit but is NOT final.
LB_PROVISIONAL = bytes.fromhex("aabbed673b1a468d1387066a40b64b6422841c4c44030620")


def test_final_kg_frame_decodes_weight_and_unit():
    reading = parse_broadcast(MANUFACTURER_ID, bytearray(KG_FINAL))
    assert reading is not None
    assert reading.weight_kg == 85.90
    assert reading.display_unit is WeightUnit.KG


def test_final_lb_frame_weight_is_kg_but_unit_is_lb():
    # Weight on the wire is always kg; only the display unit differs.
    reading = parse_broadcast(MANUFACTURER_ID, bytearray(LB_FINAL))
    assert reading is not None
    assert reading.weight_kg == 73.15
    assert reading.display_unit is WeightUnit.LB


@pytest.mark.parametrize("frame", [KG_SETTLING, KG_TARE, LB_PROVISIONAL])
def test_non_final_frames_are_ignored(frame):
    assert parse_broadcast(MANUFACTURER_ID, bytearray(frame)) is None


def test_stability_requires_both_stable_and_committed_bits():
    # 0x23 (kg final) and 0x25 (lb final) are final; 0x64 (0x20 set, 0x01 clear)
    # and the settling/tare states are not.
    assert is_final(0x03)
    assert is_final(0x05)
    assert is_final(0x23)
    assert is_final(0x25)
    assert not is_final(0x64)  # provisional: 0x20 set but 0x01 clear
    assert not is_final(0x02)


def test_unit_decode():
    assert decode_display_unit(0x23) is WeightUnit.KG  # (0x23>>1)&7 == 1
    assert decode_display_unit(0x25) is WeightUnit.LB  # (0x25>>1)&7 == 2
    assert decode_display_unit(0x64) is WeightUnit.LB  # (0x64>>1)&7 == 2
    assert decode_display_unit(0x03) is WeightUnit.KG


def test_wrong_company_id_rejected():
    assert parse_broadcast(0x004C, bytearray(KG_FINAL)) is None


def test_wrong_magic_rejected():
    bad = bytearray(KG_FINAL)
    bad[0:2] = b"\xcc\xdd"
    assert parse_broadcast(MANUFACTURER_ID, bad) is None


def test_short_payload_rejected():
    assert parse_broadcast(MANUFACTURER_ID, bytearray(KG_FINAL[:10])) is None


@pytest.mark.asyncio
async def test_scale_emits_kg_end_to_end():
    callback = MagicMock()
    scale = RenphoAABBScale(KG_MAC, callback, bleak_scanner_backend=MagicMock())

    device = SimpleNamespace(address=KG_MAC, name="Renpho Scale")
    adv = SimpleNamespace(manufacturer_data={MANUFACTURER_ID: bytes(KG_FINAL)})

    await scale._advertisement_callback(device, adv)

    assert callback.call_count == 1
    data = callback.call_args[0][0]
    assert data.measurements == {WEIGHT_KEY: 85.90}
    assert data.display_unit is WeightUnit.KG
    # The observed unit is cached on the scale, too.
    assert scale.display_unit is WeightUnit.KG


@pytest.mark.asyncio
async def test_scale_emits_weight_and_observed_unit_end_to_end():
    callback = MagicMock()
    scale = RenphoAABBScale(LB_MAC, callback, bleak_scanner_backend=MagicMock())

    device = SimpleNamespace(address=LB_MAC, name="Renpho Scale")
    adv = SimpleNamespace(manufacturer_data={MANUFACTURER_ID: bytes(LB_FINAL)})

    await scale._advertisement_callback(device, adv)

    assert callback.call_count == 1
    data = callback.call_args[0][0]
    assert data.measurements == {WEIGHT_KEY: 73.15}
    assert data.display_unit is WeightUnit.LB
    assert data.address == LB_MAC


@pytest.mark.asyncio
async def test_scale_ignores_advertisement_from_other_address():
    # Device identity is enforced by the base address filter: an advertisement
    # from a different address is dropped even if the payload is a valid frame.
    callback = MagicMock()
    scale = RenphoAABBScale(LB_MAC, callback, bleak_scanner_backend=MagicMock())
    device = SimpleNamespace(address="00:11:22:33:44:55", name="Renpho Scale")
    adv = SimpleNamespace(manufacturer_data={MANUFACTURER_ID: bytes(LB_FINAL)})

    await scale._advertisement_callback(device, adv)

    assert callback.call_count == 0


@pytest.mark.asyncio
async def test_repeated_final_frames_within_cooldown_deliver_once():
    # A weigh-in re-broadcasts the final frame for the whole advertising
    # burst; delivering a reading arms the cooldown so only one lands.
    callback = MagicMock()
    scale = RenphoAABBScale(KG_MAC, callback, bleak_scanner_backend=MagicMock())
    device = SimpleNamespace(address=KG_MAC, name="Renpho Scale")
    adv = SimpleNamespace(manufacturer_data={MANUFACTURER_ID: bytes(KG_FINAL)})

    await scale._advertisement_callback(device, adv)
    await scale._advertisement_callback(device, adv)

    assert callback.call_count == 1


@pytest.mark.asyncio
async def test_final_frame_after_cooldown_expiry_delivers_again():
    callback = MagicMock()
    scale = RenphoAABBScale(KG_MAC, callback, bleak_scanner_backend=MagicMock())
    device = SimpleNamespace(address=KG_MAC, name="Renpho Scale")
    adv = SimpleNamespace(manufacturer_data={MANUFACTURER_ID: bytes(KG_FINAL)})

    await scale._advertisement_callback(device, adv)
    scale._cooldown_end_time = 0  # simulate the window elapsing
    await scale._advertisement_callback(device, adv)

    assert callback.call_count == 2


@pytest.mark.asyncio
async def test_zero_cooldown_delivers_every_final_frame():
    callback = MagicMock()
    scale = RenphoAABBScale(
        KG_MAC, callback, bleak_scanner_backend=MagicMock(), cooldown_seconds=0
    )
    device = SimpleNamespace(address=KG_MAC, name="Renpho Scale")
    adv = SimpleNamespace(manufacturer_data={MANUFACTURER_ID: bytes(KG_FINAL)})

    await scale._advertisement_callback(device, adv)
    await scale._advertisement_callback(device, adv)

    assert callback.call_count == 2


@pytest.mark.asyncio
async def test_non_final_frame_does_not_arm_cooldown():
    callback = MagicMock()
    scale = RenphoAABBScale(KG_MAC, callback, bleak_scanner_backend=MagicMock())
    device = SimpleNamespace(address=KG_MAC, name="Renpho Scale")
    settling = SimpleNamespace(manufacturer_data={MANUFACTURER_ID: bytes(KG_SETTLING)})
    final = SimpleNamespace(manufacturer_data={MANUFACTURER_ID: bytes(KG_FINAL)})

    await scale._advertisement_callback(device, settling)
    await scale._advertisement_callback(device, final)

    assert callback.call_count == 1


@pytest.mark.asyncio
async def test_scale_ignores_non_final_advertisement():
    callback = MagicMock()
    scale = RenphoAABBScale(LB_MAC, callback, bleak_scanner_backend=MagicMock())
    device = SimpleNamespace(address=LB_MAC, name="Renpho Scale")
    adv = SimpleNamespace(manufacturer_data={MANUFACTURER_ID: bytes(LB_PROVISIONAL)})

    await scale._advertisement_callback(device, adv)

    assert callback.call_count == 0
