# Upstream Regression Migration Closure — 2026-07-13

## Provenance

- Source repository: `https://github.com/Rimagination/instsci`
- Source commit: `836cd6b65ad74136b7a1ff17672816a3b8b789aa`
- Target repository base: `e98ef801657af5aad5e4836cf03432babb4fc5f1`
- Migration time: `2026-07-13T05:13:31+08:00`

## Scope

Seventeen upstream regression-test and test-helper files were migrated into
`instsci/tests`. The migration keeps the original behavioral coverage while
adapting package imports, repository-root discovery, public-preview fixture
names, current skill layout, and the intentional separation between public
capability summaries and private browser evidence.

The restored suite exposed and fixed regressions in IEEE auth-wall detection,
Wiley PDF ownership checks, RSC source-derived PDF candidate priority, and
publisher access-catalog coverage for eLife, GeoScienceWorld, and OnePetro.

## Validation

- `python -B -m unittest discover -s instsci/tests -q`
  - `285` tests passed
  - `1` live publisher smoke test skipped by default
- `scripts/Check.ps1 -Full`
  - selected contract tests passed
  - public package audit passed with `0` issues
  - full doctor passed
  - publisher capability-matrix command passed

The live publisher smoke test remains opt-in through
`INSTSCI_LIVE_PUBLISHER_TESTS=1`; this migration does not claim a fresh
publisher-access verdict.

## Artifact Hash

Aggregate SHA-256 over the sorted migrated filename/SHA-256 manifest:

`ad90003980ea9528cd65338d729c5ce09201994df72fad22062166296810ee91`
