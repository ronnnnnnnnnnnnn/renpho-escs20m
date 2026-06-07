# Renpho ES-CS20M BLE

[![PyPI](https://img.shields.io/pypi/v/renpho-escs20m.svg)](https://pypi.org/project/renpho-escs20m/)
[![Python versions](https://img.shields.io/pypi/pyversions/renpho-escs20m.svg)](https://pypi.org/project/renpho-escs20m/)
[![CI](https://github.com/ronnnnnnnnnnnnn/renpho-escs20m/actions/workflows/ci-cd.yml/badge.svg)](https://github.com/ronnnnnnnnnnnnn/renpho-escs20m/actions/workflows/ci-cd.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)

This package provides an unofficial interface for interacting with
Renpho's ES-CS20M scale (and other Renpho scales that share the same
QN-series protocol) over Bluetooth Low Energy. See the [Device
compatibility](#device-compatibility) section for the current list of
confirmed-working models.

> **Disclaimer:** This is an unofficial, community-developed library.
> It is not affiliated with, endorsed by, or connected to Renpho, its
> parent companies, subsidiaries, or affiliates. The official Renpho
> website can be found at <https://www.renpho.com>. "Renpho",
> "ES-CS20M", and other model names referenced here, along with
> related marks, emblems, and images, are property of their respective
> owners. Use of any trade name or trademark is for identification and
> reference purposes only and does not imply any association with the
> trademark holder.

[![Buy Me A Coffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://www.buymeacoffee.com/ronnnnnnn)

## Features

- Live weight and body fat readings from the scale's notification stream.
- Guest-mode protocol — coexists safely with users registered by the official Renpho app on the same scale.
- Three modes: fixed-user (with `Profile`), user-detection (with async resolver), and weight-only.
- `BodyMetrics` derives 9 body-composition metrics from a stable reading: BMI, fat-free mass, body water %, skeletal muscle %, muscle mass, bone mass, protein %, BMR, and a body fat % passthrough.
- Optional RX/TX payload logging via standard Python `logging`.

## Installation

```bash
pip install renpho-escs20m
```

PyPI uses the hyphenated name `renpho-escs20m`; the import name uses
underscores: `import renpho_escs20m`.

## Device compatibility

This library targets a specific **QN-series BLE protocol**, which several
Renpho scales share — but compatibility doesn't strictly track the marketed
model name. Some ES-CS20M *hardware revisions* speak a different protocol and
aren't supported; some other Renpho models happen to share hardware with the
ES-CS20M and work fine. The reliable discriminator seems to be the
**HVIN** (Hardware Version Identification Number) printed on the
regulatory sticker on the back of the scale, including its trailing
revision code (e.g. `…MA2` vs `…MB2` vs `…MN`). Some stickers don't
print HVIN as a separate field — in that case the same identifier is
embedded as the trailing portion of the **FCC ID** (e.g. FCC ID
`2A26P-ESCS20M` → device code `ESCS20M`). The FCC ID column below
lets you match on either.

Confirmed-working:

| Marketed model | HVIN        | FCC ID              |
|----------------|-------------|---------------------|
| ES-CS20M       | `ESCS20MA2` | `2A26P-ESCS20MA2`   |
| ES-CS20M       | `ESCS20MN`  | `2A26P-ESCS20MN`    |
| ES-CS20M       | -           | `2A26P-ESCS20M`     |
| ES-26M         | `ESCS20MA2` | `2A26P-ESCS20MA2`   |
| ES-30M         | `ES30MA2`   | `2A26P-ES30MA2`     |
| ES-32MD        | `ESCS20MA2` | `2A26P-ESCS20MA2`   |


Known-incompatible:

| Marketed model | HVIN        | FCC ID              |
|----------------|-------------|---------------------|
| ES-CS20M       | `ESCS20MB2` | `2A26P-ESCS20MB2`   |

The pattern so far: marketed model name is unreliable, but the HVIN — and specifically its revision suffix (`A2`, `B2`, `N`…) — tracks the actual hardware and apparently also the protocol. If your Renpho scale HVIN ends in `A2` or `N`, this library will likely work with it; if it ends in some other suffix, try it out to see if it works and report back on the issue tracker.

> This library may also work with other QN-Scale varieties utilizing the same protocol, including non-Renpho ones. Feel free to report compatibility results on the issue tracker.

### Reporting a compatibility result

If your scale isn't in either table, open an issue at
[github.com/ronnnnnnnnnnnnn/renpho-escs20m/issues](https://github.com/ronnnnnnnnnnnnn/renpho-escs20m/issues)
with:

- Marketed model (e.g., ES-CS20M)
- HVIN from the back-of-device sticker (including the revision suffix)
- Whether the library actually drives the scale correctly (live weight
  notifications, body fat values, etc.)

The library itself doesn't gate or warn on compatibility at runtime — it'll
attempt the handshake against any device. This section is the canonical
compatibility record.

## Quick start

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
    age=35,
    height_m=1.80,
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

## API reference

### Scale client

- `RenphoESCS20MScale(address, callback, display_unit, *, profile=None,
  scanning_mode=BluetoothScanningMode.ACTIVE, …)` — BLE scale client.
  The `profile` argument is one of:
  - a `Profile` (fixed-user mode),
  - a `ProfileResolver` (user-detection mode),
  - `None` (weight-only mode, default).

  Additional keyword arguments (`adapter`, `cooldown_seconds`,
  `max_connect_attempts`, `bleak_scanner_backend`, `logger`) are
  available for advanced use — see the class docstring.
- `callback` (passed to `RenphoESCS20MScale`) — invoked only on the
  final `stable-with-metrics` frame the scale emits at the end of a
  measurement. In user-detection mode, the earlier `stable` frame is
  used only to trigger the profile resolver and does not reach the
  callback. Within the frame, `ScaleData.measurements` always contains
  `WEIGHT_KEY`; `BODY_FAT_KEY` and the two `RESISTANCE_*_KEY` entries
  are present only when the scale actually produced non-zero values
  for them — they will be absent in weight-only mode, in user-detection
  mode if the resolver returned `None`, and any time `algorithm=0x00`.
- `scale.battery_level` — last successfully-read battery percentage
  (`int | None`). May be `None` until first successful read.
- `scale.firmware_revision` — last successfully-read firmware revision
  string (`str | None`). May be `None` until first successful read or
  when response is empty.
- `BluetoothScanningMode` — `ACTIVE` (default) / `PASSIVE`, passed via
  the `scanning_mode` kwarg. `PASSIVE` only takes effect on Linux
  (BlueZ); other platforms fall back to active.

### Profiles

- `Profile(sex, age, height_m, athlete=False, algorithm=0x04)` —
  user-profile inputs the scale needs to compute body fat on-device.
  See `Profile`'s docstring for the wire semantics of each field.
- `ProfileResolver` — type alias for the async callback used in
  user-detection mode: `Callable[[float], Awaitable[Profile | None]]`.
  Receives the first stable weight in kg and returns the Profile to
  write (or `None` to skip).

### Measurements

- `ScaleData` — dataclass passed to the notification callback. Fields:
  `name`, `address`, `display_unit`, and `measurements` (a dict keyed
  by the constants below).
- `WeightUnit` — `KG`, `LB`, `ST`, `ST_LB`.
- Measurement-dict keys (constants importable from `renpho_escs20m`):
  - `WEIGHT_KEY` (`"weight"`) — kg
  - `BODY_FAT_KEY` (`"body_fat"`) — % (only on stable-with-metrics frames)
  - `RESISTANCE_1_KEY`, `RESISTANCE_2_KEY` (`"resistance_1"`,
    `"resistance_2"`) — bioelectrical impedance in ohms (only on
    stable-with-metrics frames; the two readings are typically within a
    couple of ohms of each other and either can be fed to
    `calculate_body_fat()`).

### Body composition

- `BodyMetrics(weight_kg, height_m, age, sex, body_fat_percentage)` —
  derives body-composition metrics from a stable reading. Call it
  from the notification callback once a `Profile` is known. No
  `athlete` parameter: by the time a body fat value reaches this
  class, the scale's firmware has already applied the athlete
  adjustment. Exposes these snake_case attributes:
  - `body_mass_index` — BMI
  - `body_fat_percentage` — passthrough of the constructor input
  - `fat_free_mass` (kg)
  - `body_water_percentage`
  - `skeletal_muscle_percentage`
  - `bone_mass` (kg)
  - `muscle_mass` (kg)
  - `protein_percentage`
  - `basal_metabolic_rate` (kcal/day, integer)
- `calculate_body_fat(weight_kg, height_m, age, sex, resistance, *,
  algorithm=0x04, athlete=False)` — off-scale approximation of the
  on-device body fat formulas (algorithms `0x03` and `0x04` only).
  Complements `BodyMetrics`: `BodyMetrics` takes an already-computed
  body fat value as input, while `calculate_body_fat` computes one
  from raw impedance. The typical pairing is to feed
  `calculate_body_fat`'s output into `BodyMetrics` when a slow
  user-detection lookup misses the scale's commit window and body fat
  needs to be recomputed from `RESISTANCE_1_KEY` after the fact.

### Low-level

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

## App-matching conventions

The Renpho app applies a few non-obvious transformations to profile
data before running the body fat calculation. The library diverges
from one and leaves the other to the caller:

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


## Platform compatibility

- Python 3.11+
- bleak 2.x or 3.x (`bleak>=2.0.0,<4.0.0`)
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

(See [home-assistant/core#76186 (comment)](https://github.com/home-assistant/core/issues/76186#issuecomment-1204954485) for context.)

## Support the project

If you find this unofficial project helpful, consider buying me a
coffee! Your support helps maintain and improve this library.

[![Buy Me A Coffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://www.buymeacoffee.com/ronnnnnnn)

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file
for details.
