# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- FFE0 GATT transport support on `RenphoQNScale`: QN-Scale hardware ships
  the same wire protocol on two GATT layouts, and the library now falls
  back to the FFE0 service (FFE1 notify / FFE2 indicate / FFE3+FFE4
  write — e.g. the Arboleaf CS20M) when the renpho FFF0 service is absent,
  verified against a captured session. Previously these scales failed with
  "notification characteristic not found" (#5).
- `clear_stored_measurements` option on `RenphoQNScale` (default off):
  drains the scale's store of offline measurements — readings taken while
  nothing was connected — once per session. Receiving a stored reading
  deletes it from the scale (the protocol has no separate delete command),
  so this is opt-in: enabling it hides those readings from the official
  Renpho app. Drained readings are logged at debug level and discarded for now. 
  Each flavor is queried with its own command form.
- `RenphoAABBScale` — experimental, weight-only support for the
  broadcast-only (`0xaabb`) ES-CS20M subvariant (FCC ID `2APXUES-CS20M`).
  This scale is non-connectable: weight is read from its BLE advertisements.
  No body composition (it performs no impedance/BIA), and its display unit is
  observed-only (cannot be set).
- Exported the transport base classes `RenphoScale`, `GattScale`, and
  `AdvertisementScale` for adding further protocol variants.

### Changed

- Restructured the library to host multiple protocols: shared base classes in
  `scale.py`, with the QN-series (`RenphoQNScale`) and broadcast
  (`RenphoAABBScale`) protocols in the `qn/` and `xaabb/` subpackages.
  `body_metrics` moved into `qn/`. **The public API is unchanged** — every
  previous top-level import still works.
- Renamed `RenphoESCS20MScale` to `RenphoQNScale`, naming the protocol it
  drives rather than the marketed model (the ES-CS20M ships in several
  variants that speak different protocols). `RenphoESCS20MScale` remains a
  backward-compatible alias.

## [0.2.0] - 2026-05-02

### Added

- New keyword argument `max_connect_attempts` on `RenphoESCS20MScale` (default `2`).
  Caps the retry count passed to `bleak_retry_connector.establish_connection`,
  shortening the worst case when a stale advertisement triggers a connect
  attempt against an already-offline scale (was ~57s with the upstream default
  of 10 attempts; now ~12s). Constructor raises `ValueError` if the value is
  less than 1.

### Changed

- **BREAKING:** Renamed `BodyMetrics.fat_free_weight` to
  `BodyMetrics.fat_free_mass`. The new name matches Renpho's terminology, the
  standard sports-science term (FFM), and the library's own naming convention
  for absolute-mass quantities (`bone_mass`, `muscle_mass`). Consumers reading
  this property must rename their access.

## [0.1.2] - 2026-05-01

### Changed

- Clarified and enforced strict separation of stable measurement behavior: 
- `_MEASUREMENT_STATUS_STABLE` now only triggers profile-resolution flow in user-detection mode.
- `_MEASUREMENT_STATUS_STABLE_WITH_METRICS` now handles callback reporting and finalization.

## [0.1.1] - 2026-04-30

### Added

- Added cached device metadata properties for battery level and firmware revision.
- Added best-effort reads for battery level and firmware revision during scale
  session startup.

## [0.1.0] - 2026-04-28

Initial release.
