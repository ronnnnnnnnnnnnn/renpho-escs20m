# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
