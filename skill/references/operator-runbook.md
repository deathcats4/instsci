# InstSci Operator Runbook

This runbook is for agents operating InstSci publisher PDF workflows. It is not an external handout, tutorial script, or private process note.

## Default Workflow

Use `user` mode unless you are debugging. `instsci papers` is OA-first by default: it tries cache, Open Access, and open publisher PDF routes before opening the visible browser.

```powershell
instsci papers dois.txt --publisher auto --institution "Institution Name" --mode user --output .\runs\papers
```

Force browser-only testing only when you are intentionally validating closed-access behavior:

```powershell
instsci papers dois.txt --publisher wiley --mode user --no-oa-first --output .\runs\wiley_browser_only
```

For a publisher-specific batch:

```powershell
instsci publisher-batch dois.txt --publisher acs --institution "Institution Name" --mode user --output .\runs\acs
```

The default user experience is intentionally quiet:

- OA PDFs are saved directly without browser evidence
- lightweight browser preflight before launch
- runtime browser watch in `quiet` mode
- full doctor only when a run fails or needs attention
- concise terminal summary with manifest path

## Prewarm Before Batch

When using a fresh browser profile, a new network route, or a publisher marked `prewarm_required`, run one DOI first with the same browser profile:

```powershell
$profile = ".\profiles\publisher-name"
instsci papers .\doi_one.txt --publisher publisher-name --browser-profile $profile --mode user --output .\runs\publisher_prewarm
instsci papers .\doi_batch.txt --publisher publisher-name --browser-profile $profile --mode user --output .\runs\publisher_batch
```

Prewarm lets the visible browser collect institution, WAF, verification, viewer, and preference state before a larger batch.

## When the Browser Needs You

If CloakBrowser stops on SSO, institution login, 2FA, CAPTCHA, Cloudflare, or a human-verification checkbox:

1. Complete only the visible user step yourself.
2. Leave the browser open.
3. Let InstSci continue automatically.

InstSci must not type passwords, OTPs, CAPTCHA answers, or recovery codes.

## Reading Results

The final manifest includes:

- `status`: final file status, such as `success`, `unverified`, or `missing`
- `standard_status`: normalized reason, such as `auth_required`, `waf_blocked`, or `capture_failed`
- `result_evidence`: `oa_direct`, `publisher_open_pdf`, `browser_verified`, `http_preflight`, or `not_verified`
- `pdf_path`: copied PDF path when a file was captured
- `pdf_url`: source PDF URL when known
- `diagnostic_path`: saved diagnostic evidence when available
- `next_action`: the next useful user or developer action

Evidence meanings:

- `oa_direct`: open PDF found through OA metadata, repository, arXiv, or another legal open route.
- `publisher_open_pdf`: publisher-hosted PDF was openly reachable without institution browser state.
- `browser_verified`: closed-access or institution-mediated route was verified in visible browser.
- `http_preflight`: probe evidence only; do not treat as final closed-access success.
- `not_verified`: no reliable PDF evidence was captured.

Keep `status`, `standard_status`, and `result_evidence` separate. A PDF file with weak DOI/text matching is `status=unverified`, not `status=missing`.

Common next actions:

- `complete_institution_login_in_visible_browser_then_retry`
- `complete_visible_human_verification_then_retry`
- `stop_batch_and_retry_later_or_use_manual_browser`
- `check_access_in_regular_browser_or_library_subscription`
- `rerun_in_diagnose_mode_and_inspect_pdf_candidates`

## Publisher Matrix Behavior

InstSci keeps a local capability matrix for site-level experience:

- `ready`: run normally
- `prewarm_required`: single DOI prewarm is recommended
- `waf_risky`: avoid batch by default
- `access_side_check_needed`: verify access in a regular browser first
- `unsupported`: route is not maintained yet

The matrix stores only reusable site-level notes. It must not store credentials, cookies, personal emails, private institution details, or private DOI lists.

Use `--force` only for deliberate retesting:

```powershell
instsci papers dois.txt --publisher onepetro --mode diagnose --force
```

## Public Sharing Notes

For external tutorials or distribution notes outside this repository, keep only:

- general problem pattern
- browser/VPN/routing concept
- command shape
- status meanings
- safe troubleshooting order

Remove:

- personal account or email
- school-specific login screenshots
- credential, cookie, token, or VPN configuration details
- private research topics and DOI lists
- institution entitlement claims that are not generally reproducible

