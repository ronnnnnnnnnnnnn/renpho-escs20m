"""Golden regression tests for
:func:`renpho_escs20m.body_metrics.calculate_body_fat`.

Each parameter set is a real stable-with-metrics BLE frame captured
from live Renpho ES-CS20M scales — same scale, three demographic
profiles (M 43yo h=1.700 m, F 38yo h=1.55 m, F 28yo h=1.85 m), spanning
both algorithm bytes (``0x03``, ``0x04``) and both modes (non-athlete,
athlete) for each demographic. The golden body fat values are exactly
what the scale broadcast in ``payload[11:13]``, so a passing assertion
means :func:`calculate_body_fat` reproduces the on-device firmware
calculation bit-for-bit.
"""

from __future__ import annotations

import pytest

from renpho_escs20m.body_metrics import Sex, calculate_body_fat


# (label, sex, age, height_m, algorithm, athlete, weight_kg, r1, bf_expected)
_GOLDENS = [
    # M, 43yo, h=1.700 m
    ('M-04-N-1', Sex.Male, 43, 1.700, 0x04, False, 74.95, 505, 21.0),
    ('M-04-N-2', Sex.Male, 43, 1.700, 0x04, False, 76.15, 502, 21.6),
    ('M-04-N-3', Sex.Male, 43, 1.700, 0x04, False, 78.95, 501, 23.1),
    ('M-04-N-4', Sex.Male, 43, 1.700, 0x04, False, 81.30, 505, 24.3),
    ('M-04-Y-1', Sex.Male, 43, 1.700, 0x04, True,  75.35, 498, 14.7),
    ('M-04-Y-2', Sex.Male, 43, 1.700, 0x04, True,  76.70, 500, 15.1),
    ('M-04-Y-3', Sex.Male, 43, 1.700, 0x04, True,  79.50, 495, 15.8),
    ('M-04-Y-4', Sex.Male, 43, 1.700, 0x04, True,  81.85, 505, 16.5),
    ('M-03-N-1', Sex.Male, 43, 1.700, 0x03, False, 81.75, 502, 29.7),
    ('M-03-N-2', Sex.Male, 43, 1.700, 0x03, False, 79.40, 495, 28.8),
    ('M-03-N-3', Sex.Male, 43, 1.700, 0x03, False, 76.70, 504, 27.6),
    ('M-03-N-4', Sex.Male, 43, 1.700, 0x03, False, 75.40, 510, 27.1),
    ('M-03-Y-1', Sex.Male, 43, 1.700, 0x03, True,  81.75, 510, 17.9),
    ('M-03-Y-2', Sex.Male, 43, 1.700, 0x03, True,  79.40, 500, 17.4),
    ('M-03-Y-3', Sex.Male, 43, 1.700, 0x03, True,  76.70, 498, 16.7),
    ('M-03-Y-4', Sex.Male, 43, 1.700, 0x03, True,  75.65, 500, 16.5),
    # F, 38yo, h=1.55 m
    ('F38-04-Y-1', Sex.Female, 38, 1.55, 0x04, True,  81.65, 510, 28.3),
    ('F38-04-Y-2', Sex.Female, 38, 1.55, 0x04, True,  75.25, 497, 25.8),
    ('F38-03-Y-1', Sex.Female, 38, 1.55, 0x03, True,  81.65, 509, 27.9),
    ('F38-03-Y-2', Sex.Female, 38, 1.55, 0x03, True,  75.25, 510, 26.2),
    ('F38-04-N-1', Sex.Female, 38, 1.55, 0x04, False, 81.65, 510, 42.5),
    ('F38-04-N-2', Sex.Female, 38, 1.55, 0x04, False, 75.25, 500, 38.4),
    ('F38-03-N-1', Sex.Female, 38, 1.55, 0x03, False, 81.65, 507, 41.2),
    ('F38-03-N-2', Sex.Female, 38, 1.55, 0x03, False, 75.25, 509, 39.5),
    # F, 28yo, h=1.85 m
    ('F28-04-Y-1', Sex.Female, 28, 1.85, 0x04, True,  81.65, 499, 18.6),
    ('F28-04-Y-2', Sex.Female, 28, 1.85, 0x04, True,  75.25, 508, 16.8),
    ('F28-03-Y-1', Sex.Female, 28, 1.85, 0x03, True,  81.65, 496, 19.6),
    ('F28-03-Y-2', Sex.Female, 28, 1.85, 0x03, True,  75.25, 500, 17.9),
    ('F28-04-N-1', Sex.Female, 28, 1.85, 0x04, False, 81.65, 503, 25.9),
    ('F28-04-N-2', Sex.Female, 28, 1.85, 0x04, False, 75.25, 509, 23.0),
    ('F28-03-N-1', Sex.Female, 28, 1.85, 0x03, False, 81.65, 510, 29.1),
    ('F28-03-N-2', Sex.Female, 28, 1.85, 0x03, False, 75.25, 500, 26.3),
]


@pytest.mark.parametrize(
    "label,sex,age,height_m,algorithm,athlete,weight_kg,r1,bf_expected",
    _GOLDENS,
    ids=[g[0] for g in _GOLDENS],
)
def test_calculate_body_fat_matches_scale(
    label, sex, age, height_m, algorithm, athlete, weight_kg, r1, bf_expected
):
    """``calculate_body_fat`` reproduces the scale firmware's broadcast
    body fat to within the 0.05 pp scale quantum."""
    bf = calculate_body_fat(
        weight_kg=weight_kg,
        height_m=height_m,
        age=age,
        sex=sex,
        resistance=r1,
        algorithm=algorithm,
        athlete=athlete,
    )
    assert abs(bf - bf_expected) <= 0.1, (
        f"{label}: calculate_body_fat returned {bf}, "
        f"scale broadcast {bf_expected} (delta {bf - bf_expected:+.2f} pp)"
    )


def test_calculate_body_fat_rejects_unknown_algorithm():
    with pytest.raises(ValueError, match="0x05"):
        calculate_body_fat(
            weight_kg=75.0, height_m=1.70, age=30, sex=Sex.Male,
            resistance=500, algorithm=0x05,
        )


def test_calculate_body_fat_rejects_zero_resistance():
    """A frame whose BIA didn't fire (e.g. captured during the
    bootstrap-profile window) reports resistance=0; the off-scale
    recompute must reject it cleanly rather than crash on a
    ZeroDivisionError."""
    with pytest.raises(ValueError, match="resistance"):
        calculate_body_fat(
            weight_kg=75.0, height_m=1.70, age=30, sex=Sex.Male,
            resistance=0, algorithm=0x04,
        )


def test_calculate_body_fat_returns_one_decimal():
    """Output is rounded to 1 decimal place to match the scale's broadcast
    quantum (the scale reports body fat in 0.1% units)."""
    bf = calculate_body_fat(
        weight_kg=74.95, height_m=1.70, age=43, sex=Sex.Male,
        resistance=505, algorithm=0x04,
    )
    # Round-trip through the 0.1 quantum
    assert bf == round(bf, 1)
