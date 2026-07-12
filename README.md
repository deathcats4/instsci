# InstSci Workflow

InstSci Workflow is a public preview build for researchers who want a cleaner way to collect papers, verify access, and hand finished PDFs into Zotero.

It is based on the MIT-licensed InstSci project and keeps the core idea simple: try Open Access first, use a visible browser only when publisher access really needs it, and leave behind a manifest that explains what happened instead of a pile of mystery folders.

> Public preview: useful for real DOI batches, but not a promise of universal publisher automation. Different institutions, networks, SSO flows, and publisher WAF rules will change results.

## What It Does

- Searches for candidate papers and exports reviewable JSON or CSV results.
- Turns selected search results into a deduplicated DOI file for acquisition.
- Finds Open Access PDFs first, without opening a browser when OA is enough.
- Uses a visible CloakBrowser flow for closed-access publisher checks.
- Keeps SSO, CAPTCHA, 2FA, and WAF decisions in the user's hands.
- Writes auditable manifests with `file_status`, `standard_status`, and `result_evidence`.
- Groups unresolved publisher work so large DOI batches are easier to resume.
- Syncs successful items into Zotero and links the matching local PDF.

## What It Is Not

- Not a publisher bypass tool.
- Not a scraper that ignores access rules.
- Not a guarantee that every DOI will download.
- Not a replacement for your library subscription, institutional login, ILL, or author-request workflow.

## Repository Layout

| Path | Purpose |
| --- | --- |
| `instsci/` | Runnable Python package and CLI implementation. |
| `skill/` | Codex skill instructions for agent-assisted InstSci workflows. |
| `README_REVIEW.md` | External review checklist and validation commands. |
| `NOTICE_MODIFIED.md` | Attribution and modified-build notice. |
| `LICENSE` | Original MIT license notice retained from InstSci. |

## Install

For review or local testing:

```powershell
git clone https://github.com/deathcats4/instsci-workflow.git
cd instsci-workflow
python -m pip install -e .
```

Recommended environment:

- Windows 10/11 for the visible-browser doctor workflow.
- Python 3.10 or newer.
- PowerShell.
- Network access to DOI/OA services and publisher sites.
- Your own legal institutional access when closed-access papers are involved.
- Optional: Zotero Desktop for long-term paper and PDF management.

For Elsevier and ScienceDirect retrieval, the Elsevier API key is a project-wide global setting. Configure it once with `instsci elsevier-setup --api-key YOUR_KEY --validate`. Inst Token is optional and should only be configured when your library explicitly provides one. The preferred retrieval route is `view=FULL XML -> object/eid -> PDF`.

## First Run

Start with diagnostics:

```powershell
instsci doctor --full
instsci publisher-doctor --matrix
```

Then test a small DOI list:

```powershell
instsci papers .\dois.txt --publisher auto --output .\runs\papers_demo
```

Do not start with hundreds of DOI. A good first test is 5-10 mixed papers so you can see OA success, browser-required groups, and failure states.

## Typical Workflow

```powershell
# 1. Search and save a reviewable candidate set
instsci search "pyrite sulfur isotope uranium" --limit 50 --year 2020- --output .\runs\search.json

# 2. Select one-based rows; omit --indices to keep every unique DOI record
instsci select .\runs\search.json --indices "1,3-8,12" --output .\runs\selected_dois.txt

# 3. Run OA-first acquisition and auto publisher grouping
instsci papers .\runs\selected_dois.txt --publisher auto --output .\runs\papers

# 4. Inspect publisher readiness and unresolved groups
instsci publisher-doctor --matrix

# 5. Run a specific publisher group when browser access is needed
instsci papers .\runs\papers\browser_groups\springer_dois.txt --publisher springer --no-oa-first --output .\runs\springer

# 6. Build a next-step plan for failures or unresolved rows
instsci workflow-plan .\runs\papers

# 7. Send successful items to Zotero and link local PDFs
instsci zotero handoff .\runs\papers --tags project/example
instsci zotero sync .\runs\papers --attachment-mode linked_file
```

`search` queries Semantic Scholar, OpenAlex, and Crossref by default; use
`--sources` to choose a subset. Results are merged by normalized DOI, with a
title-and-year fallback when one provider lacks a DOI. Its JSON output preserves
the query, contributing sources, source-specific citation counts, result indices,
metadata, and identifiers. `select` writes both the DOI
file consumed by `papers` and a neighboring `.selection.json` report that records
selected, skipped, missing-DOI, and duplicate-DOI rows.

## Reading Results

InstSci uses a three-layer result contract:

| Field | Meaning | Examples |
| --- | --- | --- |
| `file_status` | Whether a usable PDF file exists. | `success`, `unverified`, `missing` |
| `standard_status` | What the user or workflow should understand. | `success`, `auth_required`, `access_unavailable`, `waf_blocked`, `publisher_error` |
| `result_evidence` | How the conclusion was established. | `oa_direct`, `publisher_open_pdf`, `browser_verified`, `http_preflight`, `not_verified` |

The important rule: HTTP checks are preflight only. Final closed-access publisher conclusions should come from visible-browser evidence.

## Zotero

The preferred Zotero flow is boring on purpose: item plus matching PDF attachment.

```powershell
instsci zotero sync .\runs\papers --attachment-mode linked_file
```

By default, Zotero sync only includes successful rows with an existing PDF. Process state stays in InstSci manifests; Zotero stays clean.

This pairs well with local attachment-management tools such as Zotero Attanger, because the InstSci manifest keeps the original PDF path and Zotero becomes the long-term literature interface.

## Review Checks

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
python -B -m py_compile (Get-ChildItem .\instsci -Recurse -Filter *.py | ForEach-Object FullName)
python -B -m unittest discover -s instsci/tests -v
python -B -m instsci.cli public-audit .
python -B -m instsci.cli doctor --full --package-path .
```

Current package validation before publication:

- Python compile: 73/73 files passed.
- Unit and regression tests: 296/296 passed (`1` live publisher smoke test skipped unless explicitly enabled).
- Public package audit: passed.
- Zip hygiene scan: passed.
- Institution-specific residue scan: passed.

## Access and Compliance

InstSci Workflow is intended to make lawful literature acquisition more batchable and diagnosable. Users must rely on Open Access routes, their own library subscriptions, institutional entitlements, interlibrary loan, or other lawful access paths.

The tool does not ask for passwords and should not automate credentials, OTPs, CAPTCHA, or publisher challenges. When those appear, the user completes them manually in the visible browser.

Login persistence is local. InstSci reuses a persistent CloakBrowser profile and long-lived publisher broker, but it does not store your institution password. Exported cookies are not treated as a complete login state, and runtime profiles, cookies, broker queues, and run outputs are ignored by Git.

## Attribution

This repository is a modified public preview build based on the MIT-licensed InstSci project. See `LICENSE` and `NOTICE_MODIFIED.md` for attribution and license details.
