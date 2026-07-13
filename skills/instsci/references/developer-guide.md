# InstSci Developer Guide

Use this guide when changing publisher profiles, PDF routing, browser doctor rules, or batch behavior.

## Modes

Supported run modes:

- `user`: default, quiet, low-token, full doctor only on failure.
- `diagnose`: failure reproduction with screenshot-backed after-run doctor.
- `dev`: verbose maintenance mode for adapter and rule changes.

Precedence:

1. explicit CLI flags
2. `INSTSCI_MODE`
3. skill default `user`

Explicit legacy flags such as `--watch-browser`, `--browser-doctor`, and `--no-pause-on-blocker` must continue to override mode defaults.

## Publisher Capability Matrix

The runtime matrix lives at:

```text
instsci/data/publisher_public_capability_summary.json
```

Allowed statuses:

- `ready`
- `prewarm_required`
- `waf_risky`
- `access_side_check_needed`
- `route_not_published`
- `unclassified`

Batch policy should be conservative:

- `allow`: normal runs
- `single_only`: block multi-DOI batch unless `--force`
- `skip`: block by default

Never store secrets, cookies, emails, institution-specific credentials, or private DOI lists in the matrix.

## PDF Candidate Ranking

Main article PDFs should outrank supplementary or citation PDFs.

Positive signals:

- candidate belongs to the current article
- DOI appears in URL
- publisher main PDF paths such as `/doi/pdf/`, `/doi/epdf/`, `/content/pdf/`, `/article-pdf/`, `/content/articlepdf/`, `/pdfft`
- known viewer/download PDF routes

Negative signals:

- `supplement`, `supplementary`, `supporting-information`, `suppl`, `appendix`
- `rightslink`, `citation`, `reference`, `permissions`
- asset/mediaobject routes that are likely supplementary files

For Springer/Nature regressions, verify the main article PDF is selected before supplementary PDFs.

## Browser Doctor Rules

Doctor classification should be stable and action-oriented:

- distinguish `human_verification_required` from `waf_blocked`
- avoid treating generic `Download` text as authorized by itself
- classify visible entitlement denial as `access_unavailable`
- keep `publisher_error` separate from route unsupported

Full screenshot capture is for `diagnose`/`dev` or failure paths, not every successful `user` run.

Legacy or helper-only labels must be normalized before they reach manifests:

- `captcha_or_waf` with a visible user challenge -> `human_verification_required`
- `captcha_or_waf` with a WAF/security loop and no reliable user action -> `waf_blocked`
- `pdf_or_authorized` is browser state evidence only, not a final success status

## Manifest Contract

The complete manifest should include:

- `doi`
- `status`
- `standard_status`
- `result_evidence`
- `reason`
- `pdf_path`
- `pdf_url`
- `diagnostic_path`
- `next_action`
- `verified_match`

Use three separate concepts:

- `file_status`: `success`, `unverified`, or `missing`
- `standard_status`: `success`, `auth_required`, `human_verification_required`, `waf_blocked`, `access_unavailable`, `publisher_error`, `pdf_candidate_conflict`, `capture_failed`, or `unsupported_publisher`
- `result_evidence`: `oa_direct`, `publisher_open_pdf`, `browser_verified`, `http_preflight`, or `not_verified`

`success` means downloaded and verified. `unverified` means a PDF exists but text/DOI verification is insufficient. `missing` means no PDF was captured.

Final reports should use the same fields. Human-readable labels may be added in prose, but they must not replace `file_status`, `standard_status`, or `result_evidence`.

## OA-First Batch Policy

`instsci papers` should run a true OA-only prefilter before browser work. The prefilter may use cache, Unpaywall/OA metadata, arXiv, repository URLs, and openly reachable publisher PDF URLs. It must not fall through to institution login, WebVPN, CARSI, broker jobs, or visible browser capture.

Only unresolved records should enter publisher matrix checks, browser preflight, browser watcher, session broker, or `PublisherBatchDownloader`. If every record resolves through OA-first, write the complete manifest and return without browser diagnostics.

OA success evidence is `oa_direct` or `publisher_open_pdf`; closed-access success evidence remains `browser_verified`.

For Wiley/AGU ePDF, treat `/doi/epdf/` as an asynchronous viewer route. Wait for the viewer page/download control to stabilize before retrying candidate PDF URLs; a short `processing` overlay is not a failure.

## Regression Checks

Run these after behavior changes:

```powershell
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\.codex\skills\instsci\scripts\audit_skill.ps1"
python -m unittest instsci.tests.test_status_contract -v
python -m py_compile instsci\cli.py instsci\browser_doctor.py instsci\publisher_matrix.py instsci\publisher_pdf_router.py instsci\publisher_batch.py
instsci papers --help
instsci publisher-batch --help
instsci browser-doctor --help
```

Fixture expectations:

- ACS, Wiley, IEEE, Springer/Nature smoke DOI remains `success`.
- Springer/Nature main PDF wins over supplementary candidates.
- OnePetro-like Cloudflare loop becomes `waf_blocked`.
- GSW-like entitlement miss becomes `access_unavailable`.
- Normal user success prints concise summary and does not run full after-run doctor.
- Failure path runs at most one full after-run doctor.

Runtime installs should also ship or preserve `instsci.tests.test_status_contract`; it prevents regressions where legacy browser states leak into final manifests.

