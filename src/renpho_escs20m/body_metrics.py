"""Body composition metrics for the Renpho ES-CS20M.

The scale computes body fat on-device and publishes it in
the BLE measurement frame; :class:`BodyMetrics` takes that body fat
value as input and derives the remaining metrics (BMI, fat-free mass,
water, skeletal muscle, bone, muscle, protein, BMR) from it.
"""

from __future__ import annotations

from enum import IntEnum
from functools import cached_property
from math import floor


class Sex(IntEnum):
    Male = 0
    Female = 1


def _round(x: float, ndigits: int) -> float:
    """Round-half-up (away from zero) to ``ndigits`` decimal places."""
    scale = 10 ** ndigits
    return floor(x * scale + 0.5) / scale


class BodyMetrics:
    """
    Class for calculating body composition metrics derived from weight,
    height, age, sex, and the scale-reported body fat percentage.
    """

    def __init__(
        self,
        weight_kg: float,
        height_m: float,
        age: int,
        sex: Sex,
        body_fat_percentage: float,
    ) -> None:
        """
        Initialize body metrics calculator.

        Args:
            weight_kg: Weight in kilograms
            height_m: Height in meters. The Renpho app truncates to whole
                centimetres; pass ``int(cm)/100`` to match the app's displayed values more closely.
            age: Age in years. The Renpho app uses birthday-aware age
                (UI age N if the birthday has occurred this year,
                otherwise N-1).
            sex: Biological sex (Male or Female)
            body_fat_percentage: Body fat percentage reported by the scale

        Raises:
            ValueError: If ``weight_kg`` or ``height_m`` is not positive.
        """
        if weight_kg <= 0:
            raise ValueError("weight_kg must be positive")
        if height_m <= 0:
            raise ValueError("height_m must be positive")
        self.weight = weight_kg
        self.height = height_m
        self.age = int(age)
        self.sex = Sex(sex)
        self.body_fat = float(body_fat_percentage)

    @cached_property
    def body_mass_index(self) -> float:
        """
        Calculate Body Mass Index (BMI).

        BMI is a measure of body fat based on height and weight.

        Returns:
            float: The calculated BMI value, rounded to 1 decimal place.
        """
        return _round(self.weight / (self.height ** 2), 1)

    @cached_property
    def body_fat_percentage(self) -> float:
        """
        Body Fat Percentage (BFP) as reported by the scale.

        Returns:
            float: The BFP value passed in at construction (the scale
            firmware computes BFP from impedance on-device).
        """
        return self.body_fat

    @cached_property
    def fat_free_mass(self) -> float:
        """
        Calculate Fat-Free Mass (FFM).

        FFM is the difference between total body mass and fat mass.

        Returns:
            float: The calculated FFM value in kg, clamped to [5, 200].
        """
        ffm = _round(self.weight * (100.0 - self.body_fat) / 100.0, 2)
        return max(5.0, min(200.0, ffm))

    @cached_property
    def body_water_percentage(self) -> float:
        """
        Calculate Body Water Percentage (BWP).

        BWP is the total amount of water in the body as a percentage of
        total weight.

        Returns:
            float: The calculated BWP value, clamped to [20, 80].
        """
        bf_factor = [-0.72223, -0.68725]
        constant = [72.202, 68.651]
        bwp = _round(constant[self.sex] + bf_factor[self.sex] * self.body_fat, 1)
        return max(20.0, min(80.0, bwp))

    @cached_property
    def skeletal_muscle_percentage(self) -> float:
        """
        Calculate Skeletal Muscle Percentage.

        Skeletal muscle is the muscle tissue directly connected to bones.

        Returns:
            float: The calculated skeletal muscle percentage value,
            clamped to [17.5, 70].
        """
        bf_factor = [-0.65508, -0.58654]
        constant = [64.713, 58.390]
        smp = _round(constant[self.sex] + bf_factor[self.sex] * self.body_fat, 1)
        return max(17.5, min(70.0, smp))

    @cached_property
    def bone_mass(self) -> float:
        """
        Calculate Bone Mass.

        Bone mass is the total mass of the bones in the body.

        Returns:
            float: The calculated Bone Mass value in kg, clamped to
            [1, 7].
        """
        bf_factor = [-0.94969, -0.93960]
        constant = [94.992, 93.988]
        soft_lean_pct = _round(constant[self.sex] + bf_factor[self.sex] * self.body_fat, 1)
        soft_lean_kg = max(3.75, min(110.0, _round(self.weight * soft_lean_pct / 100.0, 2)))
        bf_kg = self.body_fat * self.weight / 100.0
        return max(1.0, min(7.0, _round(self.weight - soft_lean_kg - bf_kg, 2)))

    @cached_property
    def muscle_mass(self) -> float:
        """
        Calculate Muscle Mass.

        Returns:
            float: The calculated muscle mass value in kg.
        """
        bf_kg = self.body_fat * self.weight / 100.0
        return _round(self.weight - self.bone_mass - bf_kg, 2)

    @cached_property
    def protein_percentage(self) -> float:
        """
        Calculate Protein Percentage.

        Protein percentage is the percentage of total body weight that
        is made up of proteins.

        Returns:
            float: The calculated protein percentage value, clamped to
            [5, 24].
        """
        bf_factor = [-0.22735, -0.30245]
        constant = [22.787, 25.340]
        bpp = _round(constant[self.sex] + bf_factor[self.sex] * self.body_fat, 1)
        return max(5.0, min(24.0, bpp))

    @cached_property
    def basal_metabolic_rate(self) -> int:
        """
        Calculate Basal Metabolic Rate (BMR).

        BMR is the number of calories required to keep your body
        functioning at rest. Unlike every other metric here, BMR is a
        linear regression on bone mass, not on body fat.

        Returns:
            int: The calculated BMR value in kcal/day, clamped to
            [900, 2500].
        """
        bone_factor = [430.9015, 359.6167]
        constant = [372.7023, 370.5818]
        bmr = int(_round(constant[self.sex] + bone_factor[self.sex] * self.bone_mass, 0))
        return max(900, min(2500, bmr))



_ALGO_0X04 = {
    # (sex, athlete) -> (c_BMI, c_age, c_int)
    (Sex.Male,   False): (1.524, 0.103, -21.992),
    (Sex.Female, False): (1.545, 0.097, -12.689),
    (Sex.Male,   True):  (0.7678, 0.0292, -6.5417),
    (Sex.Female, True):  (0.9310, 0.0326, -4.5527),
}


_ALGO_0X03_NONATH = {
    # sex -> (c_h2, c_w, c_r, c_age, c_int_m_plus_extra)
    Sex.Male:   (0.0009, 0.392, -0.00095, -0.0693, 2.877),
    Sex.Female: (0.00089, 0.39, -0.001, -0.08, -3.3 + 1.662),
}


_ALGO_0X03_ATH = {
    # (sex, bmi_ge_25) -> (c_BMI², c_BMI, c_age, c_w, c_h, c_int)
    (Sex.Male,   True):  (-0.0088225, 1.1402243, 0.023917, 0.003917, -0.004927, -7.809911),
    (Sex.Male,   False): (-0.027341, 2.0040585, 0.0282436, 0.02382, -0.019248, -17.06006),
    (Sex.Female, True):  (-0.0060424,  0.999226, 0.03461369, 0.0179066, -0.044369, 5.04487),
    (Sex.Female, False): (-0.02172, 1.62807, 0.045364, 0.0857724, -0.09616912, 2.5939906),
}


def calculate_body_fat(
    weight_kg: float,
    height_m: float,
    age: int,
    sex: Sex,
    resistance: int,
    *,
    algorithm: int = 0x04,
    athlete: bool = False,
) -> float:
    """
    Compute body fat percentage using formulas approximating the on-device calculation, 
    given a known user profile and a stable measurement frame's weight and resistance.

    Useful when a user's identity is determined *after* a measurement
    (e.g. a slow user-detection lookup that misses the scale's body fat calculation
    commit window): capture ``weight_kg``, ``resistance_1`` (or ``_2``),
    and the algorithm/athlete flags off the BLE frame, then call this
    function with the resolved profile to recompute body fat.

    Args:
        weight_kg: Weight in kilograms.
        height_m: Height in meters.
        age: Age in years.
        sex: Biological sex (Male or Female).
        resistance: Bioelectrical impedance reading in ohms (use
            ``resistance_1`` from the BLE frame; ``resistance_2`` is
            usually within a couple of ohms).
        algorithm: Body fat calculation algorithm selector 
            (currently supported values are 0x03 and 0x04).
        athlete: If True, use the athlete-tuned curve.

    Returns:
        float: Body fat percentage, rounded to 1 decimal place.

    Raises:
        ValueError: If ``weight_kg``, ``height_m``, or ``resistance``
            is not positive, or if ``algorithm`` is not 0x03 or 0x04.
    """
    if weight_kg <= 0:
        raise ValueError("weight_kg must be positive")
    if height_m <= 0:
        raise ValueError("height_m must be positive")
    if resistance <= 0:
        raise ValueError("resistance must be positive")
    sex = Sex(sex)
    age = int(age)
    bmi = weight_kg / (height_m ** 2)
    height_cm = height_m * 100

    if algorithm == 0x04:
        c_bmi, c_age, c_int = _ALGO_0X04[(sex, athlete)]
        bf = c_bmi * bmi + c_age * age + c_int
        if not athlete:
            bf -= 500.0 / resistance
        return _round(bf, 1)

    if algorithm == 0x03:
        if not athlete:
            c_h2, c_w, c_r, c_age, c_int = _ALGO_0X03_NONATH[sex]
            lbm = (c_h2 * height_cm ** 2 + c_w * weight_kg
                   + c_r * resistance + c_age * age + c_int)
            return _round((weight_kg - lbm) / weight_kg * 100, 1)

        c_bmi2, c_bmi, c_age, c_w, c_h, c_int = _ALGO_0X03_ATH[(sex, bmi >= 25)]
        bf = (c_bmi2 * bmi * bmi + c_bmi * bmi + c_age * age
              + c_w * weight_kg + c_h * height_cm + c_int)
        return _round(bf, 1)

    raise ValueError(f"unsupported algorithm 0x{algorithm:02x}")


__all__ = [
    "Sex",
    "BodyMetrics",
    "calculate_body_fat",
]
