"""Golden regression tests for :mod:`renpho_escs20m.body_metrics`.

Each parameter set is a real stable-with-metrics BLE frame captured
from live Renpho ES-CS20M scales. The golden metric values match the
Renpho mobile app's displayed values to within ±0.1 pp on every
shipped metric.

If any assertion in this file breaks, either:

1. The ``BodyMetrics`` coefficients or formula have drifted. Investigate
   before adjusting golden values.
2. The rounding helper changed — once you've confirmed the new outputs
   are intentional, regenerate the golden values with the snippet at the
   bottom of this file.
"""

from __future__ import annotations

import pytest

from renpho_escs20m.body_metrics import BodyMetrics, Sex


# Profile fields: (label, sex, athlete, height_m, age, frame_hex).
# Note: "athlete" here is recorded for provenance only. BodyMetrics does
# not take an athlete parameter because the body fat value baked into
# the frame already reflects the scale's athlete-aware firmware
# calculation.
_VALIDATION_READINGS = [
    ('M43-1', Sex.Male,   False, 1.70, 43, '100eff01021dce01f001ee00d9c4'),
    ('M43-2', Sex.Male,   False, 1.70, 43, '100eff01021db501fe01fc00d8c6'),
    ('M43-3', Sex.Male,   False, 1.70, 43, '100eff01021e3701fd01fb00de4d'),
    ('F25-1', Sex.Female, False, 1.65, 25, '100eff04021dce01f301f1014035'),
    ('F25-2', Sex.Female, False, 1.65, 25, '100eff04021db501fb01f9013f2b'),
    ('F25-3', Sex.Female, False, 1.65, 25, '100eff04021e3701fb01f90146b5'),
    ('M55-1', Sex.Male,   False, 1.77, 55, '100eff03021db501fb01f900c5af'),
    ('M55-2', Sex.Male,   False, 1.77, 55, '100eff03021db501f801f600c5a9'),
    ('M55-3', Sex.Male,   False, 1.77, 55, '100eff03021e3701f101ef00cb24'),
    ('M36-1', Sex.Male,   True,  1.85, 36, '100eff05021dd301fd01fb008593'),
    ('M36-2', Sex.Male,   True,  1.85, 36, '100eff05021db001fc01fa00846d'),
    ('M36-3', Sex.Male,   True,  1.85, 36, '100eff05021e3701fe01fc0088fd'),
    ('F30-1', Sex.Female, True,  1.80, 30, '100eff06021dd301f201f000c3bc'),
    ('F30-2', Sex.Female, True,  1.80, 30, '100eff06021db001f301f100c29a'),
    ('F30-3', Sex.Female, True,  1.80, 30, '100eff06021e3701f101ef00c622'),
    ('F42-1', Sex.Female, False, 1.45, 42, '100eff07021dba01f101ef01b494'),
    ('F42-2', Sex.Female, False, 1.45, 42, '100eff0702207101fb01f901c371'),
    ('M21',   Sex.Male,   True,  1.99, 20, '100eff02021d6f01f501f30062f9'),
]


GOLDEN_METRICS = {
    'M43-1': {'body_mass_index': 26.4, 'body_fat_percentage': 21.7, 'fat_free_weight': 59.74, 'body_water_percentage': 56.5, 'skeletal_muscle_percentage': 50.5, 'bone_mass': 2.97, 'muscle_mass': 56.77, 'protein_percentage': 17.9, 'basal_metabolic_rate': 1652},
    'M43-2': {'body_mass_index': 26.3, 'body_fat_percentage': 21.6, 'fat_free_weight': 59.62, 'body_water_percentage': 56.6, 'skeletal_muscle_percentage': 50.6, 'bone_mass': 2.96, 'muscle_mass': 56.66, 'protein_percentage': 17.9, 'basal_metabolic_rate': 1648},
    'M43-3': {'body_mass_index': 26.8, 'body_fat_percentage': 22.2, 'fat_free_weight': 60.18, 'body_water_percentage': 56.2, 'skeletal_muscle_percentage': 50.2, 'bone_mass': 3.02, 'muscle_mass': 57.16, 'protein_percentage': 17.7, 'basal_metabolic_rate': 1674},
    'F25-1': {'body_mass_index': 28.0, 'body_fat_percentage': 32.0, 'fat_free_weight': 51.88, 'body_water_percentage': 46.7, 'skeletal_muscle_percentage': 39.6, 'bone_mass': 3.12, 'muscle_mass': 48.76, 'protein_percentage': 15.7, 'basal_metabolic_rate': 1493},
    'F25-2': {'body_mass_index': 27.9, 'body_fat_percentage': 31.9, 'fat_free_weight': 51.79, 'body_water_percentage': 46.7, 'skeletal_muscle_percentage': 39.7, 'bone_mass': 3.12, 'muscle_mass': 48.67, 'protein_percentage': 15.7, 'basal_metabolic_rate': 1493},
    'F25-3': {'body_mass_index': 28.4, 'body_fat_percentage': 32.6, 'fat_free_weight': 52.13, 'body_water_percentage': 46.2, 'skeletal_muscle_percentage': 39.3, 'bone_mass': 3.09, 'muscle_mass': 49.04, 'protein_percentage': 15.5, 'basal_metabolic_rate': 1482},
    'M55-1': {'body_mass_index': 24.3, 'body_fat_percentage': 19.7, 'fat_free_weight': 61.07, 'body_water_percentage': 58.0, 'skeletal_muscle_percentage': 51.8, 'bone_mass': 3.04, 'muscle_mass': 58.03, 'protein_percentage': 18.3, 'basal_metabolic_rate': 1683},
    'M55-2': {'body_mass_index': 24.3, 'body_fat_percentage': 19.7, 'fat_free_weight': 61.07, 'body_water_percentage': 58.0, 'skeletal_muscle_percentage': 51.8, 'bone_mass': 3.04, 'muscle_mass': 58.03, 'protein_percentage': 18.3, 'basal_metabolic_rate': 1683},
    'M55-3': {'body_mass_index': 24.7, 'body_fat_percentage': 20.3, 'fat_free_weight': 61.65, 'body_water_percentage': 57.5, 'skeletal_muscle_percentage': 51.4, 'bone_mass': 3.10, 'muscle_mass': 58.55, 'protein_percentage': 18.2, 'basal_metabolic_rate': 1708},
    'M36-1': {'body_mass_index': 22.3, 'body_fat_percentage': 13.3, 'fat_free_weight': 66.20, 'body_water_percentage': 62.6, 'skeletal_muscle_percentage': 56.0, 'bone_mass': 3.29, 'muscle_mass': 62.91, 'protein_percentage': 19.8, 'basal_metabolic_rate': 1790},
    'M36-2': {'body_mass_index': 22.2, 'body_fat_percentage': 13.2, 'fat_free_weight': 65.97, 'body_water_percentage': 62.7, 'skeletal_muscle_percentage': 56.1, 'bone_mass': 3.27, 'muscle_mass': 62.70, 'protein_percentage': 19.8, 'basal_metabolic_rate': 1782},
    'M36-3': {'body_mass_index': 22.6, 'body_fat_percentage': 13.6, 'fat_free_weight': 66.83, 'body_water_percentage': 62.4, 'skeletal_muscle_percentage': 55.8, 'bone_mass': 3.33, 'muscle_mass': 63.50, 'protein_percentage': 19.7, 'basal_metabolic_rate': 1808},
    'F30-1': {'body_mass_index': 23.6, 'body_fat_percentage': 19.5, 'fat_free_weight': 61.46, 'body_water_percentage': 55.2, 'skeletal_muscle_percentage': 47.0, 'bone_mass': 3.66, 'muscle_mass': 57.80, 'protein_percentage': 19.4, 'basal_metabolic_rate': 1687},
    'F30-2': {'body_mass_index': 23.5, 'body_fat_percentage': 19.4, 'fat_free_weight': 61.26, 'body_water_percentage': 55.3, 'skeletal_muscle_percentage': 47.0, 'bone_mass': 3.65, 'muscle_mass': 57.61, 'protein_percentage': 19.5, 'basal_metabolic_rate': 1683},
    'F30-3': {'body_mass_index': 23.9, 'body_fat_percentage': 19.8, 'fat_free_weight': 62.03, 'body_water_percentage': 55.0, 'skeletal_muscle_percentage': 46.8, 'bone_mass': 3.71, 'muscle_mass': 58.32, 'protein_percentage': 19.4, 'basal_metabolic_rate': 1705},
    'F42-1': {'body_mass_index': 36.2, 'body_fat_percentage': 43.6, 'fat_free_weight': 42.92, 'body_water_percentage': 38.7, 'skeletal_muscle_percentage': 32.8, 'bone_mass': 2.59, 'muscle_mass': 40.33, 'protein_percentage': 12.2, 'basal_metabolic_rate': 1302},
    'F42-2': {'body_mass_index': 39.5, 'body_fat_percentage': 45.1, 'fat_free_weight': 45.59, 'body_water_percentage': 37.7, 'skeletal_muscle_percentage': 31.9, 'bone_mass': 2.74, 'muscle_mass': 42.85, 'protein_percentage': 11.7, 'basal_metabolic_rate': 1356},
    'M21':   {'body_mass_index': 19.0, 'body_fat_percentage':  9.8, 'fat_free_weight': 67.97, 'body_water_percentage': 65.1, 'skeletal_muscle_percentage': 58.3, 'bone_mass': 3.40, 'muscle_mass': 64.57, 'protein_percentage': 20.6, 'basal_metabolic_rate': 1838},
}


def _decode_frame(frame_hex: str) -> tuple[float, float]:
    raw = bytes.fromhex(frame_hex)
    weight = int.from_bytes(raw[5:7], 'big') / 100.0
    body_fat = int.from_bytes(raw[11:13], 'big') / 10.0
    return weight, body_fat


def test_body_metrics_rejects_zero_height():
    with pytest.raises(ValueError, match="height_m"):
        BodyMetrics(
            weight_kg=75.0, height_m=0.0, age=30, sex=Sex.Male,
            body_fat_percentage=20.0,
        )


def test_body_metrics_rejects_zero_weight():
    with pytest.raises(ValueError, match="weight_kg"):
        BodyMetrics(
            weight_kg=0.0, height_m=1.70, age=30, sex=Sex.Male,
            body_fat_percentage=20.0,
        )


@pytest.mark.parametrize(
    'label,sex,athlete,height_m,age,frame_hex', _VALIDATION_READINGS,
    ids=[r[0] for r in _VALIDATION_READINGS],
)
def test_body_metrics_matches_golden(label, sex, athlete, height_m, age, frame_hex):
    del athlete  # retained for provenance; see comment on _VALIDATION_READINGS
    weight_kg, body_fat = _decode_frame(frame_hex)
    metrics = BodyMetrics(
        weight_kg=weight_kg,
        height_m=height_m,
        age=age,
        sex=sex,
        body_fat_percentage=body_fat,
    )

    expected = GOLDEN_METRICS[label]
    actual = {
        'body_mass_index':            metrics.body_mass_index,
        'body_fat_percentage':        metrics.body_fat_percentage,
        'fat_free_weight':            metrics.fat_free_weight,
        'body_water_percentage':      metrics.body_water_percentage,
        'skeletal_muscle_percentage': metrics.skeletal_muscle_percentage,
        'bone_mass':                  metrics.bone_mass,
        'muscle_mass':                metrics.muscle_mass,
        'protein_percentage':         metrics.protein_percentage,
        'basal_metabolic_rate':       metrics.basal_metabolic_rate,
    }
    assert actual == expected, f'mismatch on {label}'


def test_bmi_clamp_low_weight():
    # Any weight against a large height still clips through gracefully.
    m = BodyMetrics(
        weight_kg=40.0, height_m=2.00, age=30, sex=Sex.Male,
        body_fat_percentage=10.0,
    )
    assert m.body_mass_index == 10.0


def test_clamps_apply_at_extremes():
    # Construct a (non-physical) scenario that drives linear fits past
    # their clamp boundaries and confirm the shipped clamps are enforced.
    m = BodyMetrics(
        weight_kg=70.0, height_m=1.70, age=30, sex=Sex.Male,
        body_fat_percentage=80.0,
    )
    # Water clamp low at 20
    assert m.body_water_percentage == 20.0
    # Muscle% clamp low at 17.5
    assert m.skeletal_muscle_percentage == 17.5
    # Protein clamp low at 5
    assert m.protein_percentage == 5.0


def test_bmr_clamps():
    # Drive bone_mass to a very low value -> BMR should clip at 900
    m = BodyMetrics(
        weight_kg=35.0, height_m=1.70, age=30, sex=Sex.Female,
        body_fat_percentage=60.0,
    )
    # bone clamps to 1.0; bmr = 370.5817518 + 359.6166936*1.0 ~= 730 -> clipped to 900
    assert m.basal_metabolic_rate == 900


def test_cached_property_single_evaluation():
    m = BodyMetrics(75.0, 1.75, 40, Sex.Male, 20.0)
    first = m.body_mass_index
    second = m.body_mass_index
    assert first is second  # same cached float


# To regenerate GOLDEN_METRICS above, run:
#
#   python -c "
#   from tests.test_body_metrics import _VALIDATION_READINGS, _decode_frame
#   from renpho_escs20m.body_metrics import BodyMetrics
#   for label, sex, _athlete, h, age, frame in _VALIDATION_READINGS:
#       w, bf = _decode_frame(frame)
#       m = BodyMetrics(w, h, age, sex, bf)
#       ...
#   "
