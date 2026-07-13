# InstSci Publisher PDF Workflow

This reference is for InstSci publisher PDF retrieval, closed-access verification, publisher capability matrices, and DOI batch recovery tasks.

## Decision Ladder

1. Classify the request.
   - Metadata search, DOI normalization, OA lookup, and route discovery can use normal HTTP tools.
   - Publisher PDF download, closed-access verification, final publisher support verdicts, and capability matrices require visible CloakBrowser evidence.
2. Load policy before identity decisions.
   - Read `instsci/data/institutional_identity_policy.json` from the repository root or run `instsci identity-policy`.
3. Choose identity route.
   - Prefer explicit `--institution`.
   - Then use configured `carsi_idp_name`.
   - Then use configured `school`.
   - If none exists, ask for the user's subscription institution.
4. Use least-surprising access.
   - Prefer publisher broker, Shibboleth, OpenAthens, CARSI, and configured WAYFless links.
   - Use WebVPN only for configured institutions with browser-verified WebVPN routes.
   - If WebVPN fails, try the publisher article-page institutional login flow before marking failure.
5. Preserve visible browser context.
   - Reuse `browser_profile_dir`, `carsi_cookie_dir/<publisher>.json`, and `attempt_cache` when available.
   - Do not hide CloakBrowser while the user may need to complete SSO, 2FA, CAPTCHA, or WAF checks.
6. Prewarm before batch.
   - For each publisher batch, run one DOI first with the exact same `--browser-profile`.
   - Let the user complete visible institution login, CAPTCHA/WAF, and PDF viewer steps.
   - Continue to the batch only after the one-DOI run produces a verified PDF.
7. Verify visually.
   - Screenshots are required after important UI actions.
   - DOM events, URLs, logs, and cookies are supporting evidence only.

## Optional MCP And Skill Handoff

- Let the skill decide whether a task is metadata/OA work, HTTP preflight, or final publisher PDF evidence.
- If the current environment provides InstSci MCP tools, use them for structured context first: `get_institutional_identity_policy`, `get_publisher_access_catalog`, and `get_publisher_capability_summary`.
- If available, use `plan_publisher_pdf_workflow` to produce the visible CLI command and to detect missing subscription institution context.
- If those MCP tools are unavailable, proceed from repository files and CLI output instead of blocking.
- Do not use MCP `fetch_paper`, HTTP probes, cookie replay, or direct request results as final closed-access publisher PDF verdicts.
- Once a task needs final PDF evidence, start or resume `instsci papers` / `instsci publisher-batch` and keep the CloakBrowser visible for user login and visual checkpoints.
- If MCP context differs from `AGENTS.md` or `instsci/data/*.json`, prefer the repository files and report the mismatch.

## Recent Gotchas

- A fresh profile may stall on Cloudflare/CAPTCHA during the first batch article. Use a one-DOI prewarm run with the same profile, then reuse that profile for the batch.
- Elsevier institution entry must not click `Go to Elsevier Homepage` while trying to select an institution.
- Publisher-specific WAYFless/ShibAuth links can bypass unstable organization search when explicitly configured for the user's selected institution.
- Elsevier article pages may stall on a visible `Access through your organization` button even while the CLI process is alive. If browser automation does not advance, inspect the visible CloakBrowser window and click the public institution-access control.
- ScienceDirect may show a Chrome PDF viewer while automated download capture times out. Label this as viewer/download-capture failure with screenshot evidence, not as publisher unsupported.
- When the ScienceDirect PDF viewer displays `PDF loaded` but the Playwright download event is still waiting, use the viewer toolbar `Download` button as a browser-verified fallback; this can let `publisher-batch` capture the download event and save the PDF.
- If `publisher-batch` has already written `summary.json` with `pdf_not_captured` and the Python process has exited, clicking the still-open viewer's `Download` button will not be captured by InstSci. Start a new short single-DOI run with the same browser profile, then click `Download` while that listener is alive.
- ScienceDirect may return `CPE00001 / There was a problem providing the content you requested`; report the exact blocker and screenshot path.
- ACS has worked with an explicitly selected English institution name; do not generalize any institution into a default.
- Wiley institutional login, full-text entry, and PDF entry paths have had recent fixes; verify with visible browser evidence before changing status.
- `accept_downloads=True` is required in the built-in browser context for reliable Playwright download capture.
- Browser PDF viewer toolbar download fallback can matter when publisher PDF links open in a viewer instead of triggering a direct download.

## Browser UI Fallback And Recovery

Keep this workflow file focused on policy and decision order. For detailed Windows UI Automation commands, open `windows-uia-playbook.md`. If the browser is already at a PDF viewer and the file was not captured, open `pdf-viewer-recovery.md`.

Core rules still apply:

- use UIA only for public page controls and browser chrome controls;
- do not enter account passwords, OTPs, recovery codes, or other account secrets;
- CAPTCHA or Cloudflare checkbox clicks require explicit user confirmation, or the user should click them manually;
- after any manual or UIA action, re-read browser state and run artifacts before reporting success.

## Command Patterns

Run from the repository root.

```powershell
instsci identity-policy
instsci papers .\dois.txt --publisher auto --institution "Institution Name" --output .\runs\papers
instsci publisher-batch .\dois.txt --publisher elsevier --institution "Institution Name" --output .\runs\elsevier
python -m unittest tests.test_acs_batch
git diff --check
```

Prewarm-and-batch pattern:

```powershell
$profile = ".\profiles\elsevier"
instsci papers ".\doi_one.txt" --publisher elsevier --institution "Institution Name" --browser-profile $profile --output ".\runs\elsevier_prewarm" --concurrency 1 --no-broker --watch-browser focus --pause-on-blocker
instsci papers ".\dois.txt" --publisher elsevier --institution "Institution Name" --browser-profile $profile --output ".\runs\elsevier_batch" --concurrency 1 --no-broker --watch-browser focus --pause-on-blocker
```

If R is needed in this repo, use:

```powershell
Rscript script.R
```

## Report Template

Use this structure for each DOI or publisher:

```text
publisher:
doi:
route_attempted:
institution:
result:
evidence:
next_action:
```

Use these report fields:

- `file_status`: `success`, `unverified`, or `missing`
- `standard_status`: `success`, `auth_required`, `human_verification_required`, `waf_blocked`, `access_unavailable`, `publisher_error`, `pdf_candidate_conflict`, `capture_failed`, or `unsupported_publisher`
- `result_evidence`: `oa_direct`, `publisher_open_pdf`, `browser_verified`, `http_preflight`, or `not_verified`

Do not mark `unsupported_publisher`, `capture_failed`, or `success` from HTTP-only evidence. HTTP-only evidence can support route discovery or `http_preflight`, but not a final closed-access publisher PDF verdict.

## Manifest Consistency

Keep `final_report.md`, `final_manifest.csv`, and `final_manifest.json` aligned.

- `success`: PDF exists and verification passed.
- `unverified`: PDF exists but DOI/text verification did not pass or was inconclusive.
- `missing`: no final PDF was captured.

When a PDF is present but automation cannot prove DOI/text match, do not count it as missing.

## Security And State

- Do not write tokens, MCP endpoints, cookies, exported credentials, SSO screenshots with sensitive fields, or institutional secrets into skill files, docs, logs, or commits.
- Cookie jars are not full browser login state. Full state may include localStorage, IndexedDB, service workers, cache, TLS sessions, WAF challenge state, browser fingerprint state, and page-generated PDF tokens.
- Keep live CloakBrowser context open while it is serving as the active access broker.
- Do not manually call local/private notification scripts; task status notification is handled elsewhere.

