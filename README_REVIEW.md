# Review Guide

This repository is prepared for external review before wider public-beta use.

## Scope

Review this as a modified InstSci workflow build, not as a claim of final stable upstream release. The most important questions are:

- Does the package stay clean of local/private artifacts?
- Does the source compile in a fresh environment?
- Are result states consistent and understandable?
- Does Zotero sync keep item/PDF matching clear?
- Does the documentation avoid implying publisher bypass behavior?

## Included

- Runnable Python source in `instsci/`.
- Codex skill instructions in `skill/`.
- MIT license and modified-build attribution.

## Not Included

- Local browser profiles.
- Cookies or credential material.
- PDF outputs.
- Run folders.
- Build artifacts or wheel files.
- Bundled CloakBrowser binaries.
- Private process notes.
- Non-repository tutorials, walkthroughs, and distribution notes.

## Setup

```powershell
python -m pip install -e .
```

If your environment writes Python bytecode by default, disable it while auditing:

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
```

## Validation Commands

Run these from the repository root:

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
python -B -m py_compile (Get-ChildItem .\instsci -Recurse -Filter *.py | ForEach-Object FullName)
python -B -m unittest discover -s instsci/tests -v
python -B -m instsci.cli public-audit .
python -B -m instsci.cli doctor --full --package-path .
```

## Expected Public-Beta Positioning

Good wording:

- public preview
- public beta
- release candidate
- modified InstSci workflow build
- legal literature acquisition workflow

Avoid wording like:

- final stable
- universal downloader
- publisher bypass
- no-permission PDF downloader
- fully automated closed-access retrieval

## Reviewer Notes

A failed DOI is not automatically a bug. Useful review feedback should identify the cause when possible: invalid DOI, unsupported publisher, access unavailable, auth required, WAF/human verification, publisher error, or PDF candidate conflict.
