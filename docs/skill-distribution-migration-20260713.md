# Skill Distribution Migration Closure — 2026-07-13

## Scope

The repository-local InstSci skill moved from the legacy `skill/` directory to
the standard `skills/instsci/` package layout. Documentation, regression tests,
the public package manifest, and `agents/openai.yaml` now use that canonical
path. `scripts/Install-InstSci.ps1` provides a local CLI/MCP/skill installation
with explicit `-DryRun`, `-Force`, `-SkipCli`, and tool-selection controls.

## Provenance

- Source commit: `0b2f7fe242348200775ec8bc881cb64635d9ae7d`
- Source path: `skill/`
- Destination path: `skills/instsci/`
- Closure time: `2026-07-13T05:35:19+08:00`
- Distribution artifact files: `12`
- Aggregate SHA-256: `0acbac5f2652e5bf70a7777f32114fb1b4182c86ab53819af1df0e524f14d74f`

The aggregate covers every file under `skills/instsci/` plus
`scripts/Install-InstSci.ps1`. It is calculated from sorted
`relative-path|file-sha256` rows encoded as UTF-8 and joined with LF.

## Validation

- Skill Creator `quick_validate.py`: passed (`Skill is valid!`)
- `skills/instsci/scripts/audit_skill.ps1`: passed, 11 files checked, 0 problems
- Installer distribution tests: 5 passed
- Full Python suite: 304 passed, 1 live publisher smoke test skipped
- Workspace `scripts/Check.ps1 -Full`: passed

The installer dry-run test uses a repository-local nonexistent Codex home and
confirms that no directory is created. No global Codex skill, Python tool
environment, browser profile, credential, or private evidence store is changed
by the validation.
