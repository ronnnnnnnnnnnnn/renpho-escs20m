# Renpho ES-CS20M BLE

[![PyPI](https://img.shields.io/pypi/v/renpho-escs20m.svg)](https://pypi.org/project/renpho-escs20m/)
[![Python versions](https://img.shields.io/pypi/pyversions/renpho-escs20m.svg)](https://pypi.org/project/renpho-escs20m/)
[![CI](https://github.com/ronnnnnnnnnnnnn/renpho-escs20m/actions/workflows/ci-cd.yml/badge.svg)](https://github.com/ronnnnnnnnnnnnn/renpho-escs20m/actions/workflows/ci-cd.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)

This package provides a basic unofficial interface for interacting with Renpho's ES-CS20M scale using Bluetooth Low Energy (BLE).

## Version Status

**v0.1.1**:

- ✅ Initial release + metadata read support.
- ✅ Three operating modes: weight-only, fixed-user (with body fat), and async
  user-detection.
- ✅ Adds cached `battery_level` and `firmware_revision` metadata fields on scale
  instances.

**Disclaimer: This is an unofficial, community-developed library. It is not affiliated with, officially maintained by, or in any way officially connected with Renpho, its parent companies, subsidiaries, or affiliates. The official Renpho website can be found at https://www.renpho.com. The names "Renpho" and "ES-CS20M", as well as related names, marks, emblems, and images, are registered trademarks of their respective owners.**

[![Buy Me A Coffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://www.buymeacoffee.com/ronnnnnnn)

## Features

- Live weight and body fat readings from the scale's notification stream.
- Guest-mode protocol — coexists safely with users registered by the official Renpho app on the same scale.
- Three modes: fixed-user (with `Profile`), user-detection (with async resolver), and weight-only.
- `BodyMetrics` derives 9 body-composition metrics from a stable reading: BMI, fat-free weight, body water %, skeletal muscle %, muscle mass, bone mass, protein %, BMR, and a body fat % passthrough.
- Optional RX/TX payload logging via standard Python `logging`.

## Installation

```bash
pip install renpho-escs20m
```

PyPI uses the hyphenated name `renpho-escs20m`; the import name uses
underscores: `import renpho_escs20m`.

## Quick Start

### Weight only (no body fat)

```python
import asyncio
from renpho_escs20m import RenphoESCS20MScale, ScaleData, WEIGHT_KEY, WeightUnit


def notification_callback(data: ScaleData):
    print(f"weight={data.measurements[WEIGHT_KEY]} kg")


async def main():
    scale = RenphoESCS20MScale(
        'XX:XX:XX:XX:XX:XX', notification_callback, WeightUnit.KG,
    )
    await scale.async_start()
    await asyncio.sleep(30)
    await scale.async_stop()


asyncio.run(main())
```

### Fixed user + body metrics

```python
import asyncio
from renpho_escs20m import (
    BODY_FAT_KEY, BodyMetrics, Profile, RenphoESCS20MScale,
    ScaleData, Sex, WEIGHT_KEY, WeightUnit,
)


PROFILE = Profile(
    sex=Sex.Male,
    age=43,
    height_m=1.70,
    athlete=False,
    algorithm=0x04,        # see "Body fat algorithm" below
)


def notification_callback(data: ScaleData):
    weight = data.measurements.get(WEIGHT_KEY)
    body_fat = data.measurements.get(BODY_FAT_KEY)
    if weight is not None and body_fat is not None:
        m = BodyMetrics(
            weight_kg=weight,
            height_m=PROFILE.height_m,
            age=PROFILE.age,
            sex=PROFILE.sex,
            body_fat_percentage=body_fat,
        )
        print(
            f"weight={weight} kg  bmi={m.body_mass_index}  "
            f"bf%={m.body_fat_percentage}  bmr={m.basal_metabolic_rate}"
        )
    elif weight is not None:
        print(f"weight={weight} kg  bmi={round(weight / PROFILE.height_m**2, 1)}")


async def main():
    scale = RenphoESCS20MScale(
        'XX:XX:XX:XX:XX:XX',
        notification_callback,
        WeightUnit.KG,
        profile=PROFILE,
    )
    await scale.async_start()
    await asyncio.sleep(30)
    await scale.async_stop()


asyncio.run(main())
```

### User detection from weight

```python
import asyncio
from renpho_escs20m import (
    Profile, RenphoESCS20MScale, ScaleData, Sex, WEIGHT_KEY, WeightUnit,
)


KNOWN_USERS: dict[str, Profile] = {
    'alice': Profile(sex=Sex.Female, age=34, height_m=1.65),
    'bob':   Profile(sex=Sex.Male,   age=43, height_m=1.78),
}


async def resolve_user(weight_kg: float) -> Profile | None:
    """Pick the user whose typical weight is closest to the reading.

    Real implementations would do a DB lookup, talk to a Home
    Assistant entity, etc. The callback is async so I/O won't block
    the BLE event loop.
    """
    if weight_kg < 70:
        return KNOWN_USERS['alice']
    return KNOWN_USERS['bob']


def notification_callback(data: ScaleData):
    print(f"weight={data.measurements[WEIGHT_KEY]} kg")


async def main():
    scale = RenphoESCS20MScale(
        'XX:XX:XX:XX:XX:XX',
        notification_callback,
        WeightUnit.KG,
        profile=resolve_user,        # ← user-detection mode
    )
    await scale.async_start()
    await asyncio.sleep(30)
    await scale.async_stop()


asyncio.run(main())
```

The scale firmware will not start a measurement without a profile
reply, so the library always sends one in response to the scale's
`0x21 05 ff` profile request. In detection mode it sends a bootstrap
profile with `algorithm=0x00` (body fat calculation disabled) so the
measurement starts; on the first stable weight frame it awaits
`resolve_user(weight)` and writes the returned profile to the scale,
which then computes body fat and emits the stable-with-metrics frame.
Returning `None` from the resolver leaves the bootstrap profile in
place — the scale stays in weight-only mode for that session.

The resolver must return faster than the scale's internal body fat
commit window — empirically about **2 seconds** after the first stable
frame. If it doesn't, the scale will finalize the measurement against
the bootstrap profile (no body fat) before your resolved profile
lands. If the BLE session ends while the resolver is still in flight,
the library cancels the resolver task to avoid leaking work.

## API Reference

- `Profile(sex, age, height_m, athlete=False, algorithm=0x04)` —
  user-profile inputs the scale needs to compute body fat on-device.
  See `Profile`'s docstring for the wire semantics of each field.
- `ProfileResolver` — type alias for the async callback used in
  user-detection mode: `Callable[[float], Awaitable[Profile | None]]`.
  Receives the first stable weight in kg and returns the Profile to
  write (or `None` to skip).
- `RenphoESCS20MScale(address, callback, display_unit, *, profile, …)`
  — BLE scale client. The `profile` argument is one of:
  - a `Profile` (fixed-user mode),
  - a `ProfileResolver` (user-detection mode),
  - `None` (weight-only mode, default).
- `battery_level` — last successfully-read battery percentage (`int | None`).
  May be `None` until first successful read.
- `firmware_revision` — last successfully-read firmware revision string (`str | None`).
  May be `None` until first successful read or when response is empty.
- `ScaleData` — dataclass passed to the notification callback. Fields:
  `name`, `address`, `display_unit`, and `measurements` (a dict keyed
  by the constants in the next section).
- `BluetoothScanningMode` — `ACTIVE` (default) / `PASSIVE`. Passive
  scanning uses less power on platforms that support it.
- `BodyMetrics(weight_kg, height_m, age, sex, body_fat_percentage)` —
  derives body-composition metrics from a stable reading. Call it
  from the notification callback once a Profile is known. No `athlete`
  parameter: by the time a body fat value reaches this class, the
  scale's firmware has already applied the athlete adjustment.
- `calculate_body_fat(weight_kg, height_m, age, sex, resistance, *,
  algorithm=0x04, athlete=False)` — off-scale approximation of the
  on-device body fat formulas (algorithms `0x03` and `0x04` only).
  Useful for recomputing body fat after the fact when a slow
  user-detection lookup misses the scale's commit window.
- `Sex` — `Male` / `Female`.
- `WeightUnit` — `KG`, `LB`, `ST`, `ST_LB`.
- `build_user_profile_command(...)` — raw command builder for the
  guest-mode user-profile frame the scale expects. Most callers should
  construct a `Profile` and let `RenphoESCS20MScale` call this builder;
  use it directly only if you need to bypass the protocol state machine.

## Body fat algorithm (`Profile.algorithm`)

Selects which on-device body fat formula the scale runs. Most callers
should leave this at the default.

- `algorithm=0x04` (default) and `algorithm=0x03` are the two formulas
  Renpho's app selects from in normal use. The selection appears to
  depend on user region.
- `algorithm=0x00` disables the on-scale body fat calculation
  entirely; the scale streams weight only. This is what the library
  uses internally during user-detection bootstrap.
- Other values (`0x01`, `0x02`, `0x05`, `0x06`) are accepted by the
  scale but don't seem to be used by Renpho's app and aren't validated
  against it — treat them as experimental.

`Profile.athlete=True` is independent of `algorithm`: it switches the
firmware to its athlete-tuned curve regardless of which formula is
selected.

The library also ships an off-scale approximation of algorithms `0x03`
and `0x04` via `calculate_body_fat()` — useful when the scale's body
fat commit window closes before a slow user-detection lookup resolves.
The other algorithms aren't currently approximated in software.

## Measurement-dict keys (constants)

- `WEIGHT_KEY` (`"weight"`) — kg
- `BODY_FAT_KEY` (`"body_fat"`) — % (only on stable-with-metrics frames)
- `RESISTANCE_1_KEY`, `RESISTANCE_2_KEY` (`"resistance_1"`,
  `"resistance_2"`) — bioelectrical impedance in ohms (only on
  stable-with-metrics frames; the two readings are typically within a
  couple of ohms of each other and either can be fed to
  `calculate_body_fat()`).
- `BodyMetrics` exposes its own snake_case attributes
  (`body_mass_index`, `basal_metabolic_rate`, …).

## App-matching conventions

The Renpho app applies a few non-obvious transformations to profile
data before running the body fat calculation. This library matches
some of them automatically and intentionally diverges from one:

1. **Height precision: library passes through; app truncates to whole
   cm.** The Renpho app truncates a `170.7 cm` profile to `170 cm`
   before running the body fat calculation. This library passes the
   user's exact `height_m` through to the scale (rounded to the
   nearest mm), giving slightly more precise body fat from the scale's
   on-device curve.
   - If you want to reproduce the Renpho app's *displayed* values
     exactly (for cross-checking), pre-truncate the call site:
     `height_m = int(actual_cm) / 100`.
2. **Age is birthday-aware.** For a profile whose UI age shows *N*,
   the app uses *N* if the birthday has already occurred this year,
   else *N − 1*. `Profile.age` is a plain integer — callers wanting
   to match the app should compute this themselves before constructing
   the `Profile`.


## Compatibility

- Python 3.11+
- bleak 2.x (`bleak>=2.0.0,<3.0.0`)
- Tested on macOS (Apple Silicon)
- Linux via BlueZ should work through the standard bleak backend but is
  unverified
- Compatibility with Windows is unknown

## Troubleshooting

On Raspberry Pi (and possibly other Linux machines using BlueZ), if you
encounter a `org.bluez.Error.InProgress` error, try the following in
`bluetoothctl`:

```
power off
power on
scan on
```

(See https://github.com/home-assistant/core/issues/76186#issuecomment-1204954485)

## Support the Project

If you find this unofficial project helpful, consider buying me a
coffee! Your support helps maintain and improve this library.

[![Buy Me A Coffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://www.buymeacoffee.com/ronnnnnnn)

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file
for details.

## Disclaimer

This is an independent project developed by the community. It is not endorsed by, directly affiliated with, maintained, authorized, or sponsored by Renpho, or any of their affiliates or subsidiaries. All product and company names are the registered trademarks of their original owners. The use of any trade name or trademark is for identification and reference purposes only and does not imply any association with the trademark holder of their product brand.
