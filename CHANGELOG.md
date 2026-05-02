# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
