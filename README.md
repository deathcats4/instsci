# InstSci Workflow

[![Windows CI](https://github.com/deathcats4/instsci-workflow/actions/workflows/ci.yml/badge.svg)](https://github.com/deathcats4/instsci-workflow/actions/workflows/ci.yml)

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
| `skills/instsci/` | Standard Codex skill package for agent-assisted InstSci workflows. |
| `scripts/Install-InstSci.ps1` | Local installer for the CLI, MCP server, and Codex skill. |
| `README_REVIEW.md` | External review checklist and validation commands. |
| `NOTICE_MODIFIED.md` | Attribution and modified-build notice. |
| `LICENSE` | Original MIT license notice retained from InstSci. |

## Install

For a local Windows installation of the CLI, MCP server, and Codex skill:

```powershell
git clone https://github.com/deathcats4/instsci-workflow.git
powershell -ExecutionPolicy Bypass -File .\instsci-workflow\scripts\Install-InstSci.ps1
```

The installer places the skill at the standard `$CODEX_HOME/skills/instsci` location
(normally `~/.codex/skills/instsci`). It prefers `uv tool`, then `pipx`, and finally
the current Python environment. Existing CLI or skill installations are not
replaced unless `-Force` is supplied. Preview every action without changing the machine:

```powershell
.\scripts\Install-InstSci.ps1 -DryRun
```

For the CLI and MCP server only, either tool can install directly from GitHub:

```powershell
uv tool install git+https://github.com/deathcats4/instsci-workflow.git
# or
pipx install git+https://github.com/deathcats4/instsci-workflow.git
```

In Codex, the equivalent natural-language request is:

> Install the InstSci skill from `https://github.com/deathcats4/instsci-workflow/tree/main/skills/instsci`.

For editable development instead of a tool installation:

```powershell
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

The first visible publisher-browser run downloads CloakBrowser's Chromium
runtime (currently about 535 MB). InstSci stores this mutable runtime outside
the source tree at `~/.instsci/browsers/cloakbrowser`; set
`INSTSCI_CLOAKBROWSER_CACHE_DIR` when a different cache location is required.

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
title-and-year fallback only when at least one candidate lacks a DOI; conflicting
non-empty DOI values always remain separate. Its JSON output preserves the query,
per-provider request status, contributing sources, source-specific citation counts, result indices,
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

## Chinese Literature

Chinese literature portals are visible-browser workflows, not direct PDF URLs:

```powershell
instsci chinese-literature-sites
instsci cnki-batch .\records_cnki.json --navigation-mode search --output .\runs\cnki
instsci wanfang-batch .\records_wanfang.json --output .\runs\wanfang
```

Batch records may include either an ordered author list or an explicit first
author:

```json
[
  {"record_id": "cnki-1", "title": "示例题名", "authors": ["张三", "李四"]},
  {"record_id": "cnki-2", "title": "Another title", "first_author": "Smith, John"}
]
```

`first_author` takes precedence; otherwise InstSci reads the first non-empty
entry from `authors`. Only the first author is used for searching and
disambiguation. A unique exact-title row can proceed without author metadata.
When more than one row has the exact title, InstSci requires exactly one of
those same result rows to expose an ordered author list whose first entry equals
the requested first author. A later coauthor never counts, and an author field
whose order cannot be extracted reliably is treated as ambiguous. Otherwise it
records `ambiguous_search_result` with `result_evidence=browser_verified` and
does not click or download. For CNKI, record_id never overrides an exact-title mismatch.
If author matching was needed to select the row, the captured PDF must expose
the same first author in its title-adjacent first-page signature; a name found
only in the body, acknowledgements, or references does not pass.

For CNKI search mode, each record needs `record_id` and `title`; `url` is optional and used only as a fallback. Direct mode still requires a validated CNKI URL. Single-record CNKI downloads accept `--title`; InstSci marks `file_status=success` only when extracted PDF text matches the title or record id. A valid PDF that cannot be tied to the requested record is kept as `file_status=unverified` with `standard_status=pdf_candidate_conflict`.

Before evaluating CNKI candidates, search mode requires visible relevance sorting
to be active so older exact-title rows are not hidden by publication-time order.
If relevance sorting is unavailable or does not become active, InstSci fails
closed: it does not select a result, reserve an attempt, or start a download.

For Wanfang, records use `record_id`, `title`, and optional `query`/`url`; the batch route searches `s.wanfangdata.com.cn`, clicks the result-row download control, and captures the browser-generated `Fulltext/Download` PDF popup. CNKI and Wanfang classify visible SSO/CARSI/OpenAthens or configured institution pages as `auth_required`, so the user can complete login in the visible browser and retry the same run.

CNKI and Wanfang share one local attempt ledger for locking and audit, but they
do not have a default hard daily limit. At 100 combined automated attempts,
InstSci prints a conservative reminder; 100 is not a uniform official CNKI or
Wanfang limit. Failures and retries count as attempts. The ledger covers only
InstSci runs on this local installation and local calendar day; it cannot count
manual downloads, other machines, or other users on the same institutional
exit IP. Ledger corruption or an unavailable ledger fails closed as
`quota_state_error` instead of allowing an unaudited download.

Users or institutions can configure optional combined or portal-specific hard
limits. A configured limit stops before capture with `daily_limit_reached`:

```powershell
instsci config-cmd --chinese-warning-threshold 80
instsci config-cmd --chinese-combined-daily-limit 200
instsci config-cmd --cnki-daily-limit 90 --wanfang-daily-limit 120
instsci config-cmd --no-chinese-combined-daily-limit --no-cnki-daily-limit
instsci cnki-batch records.json --daily-limit 30
instsci wanfang-batch records.json --no-daily-limit
```

`--daily-limit` temporarily overrides that portal's configured limit while any
configured combined limit still applies. `--no-daily-limit` disables all hard
daily limits for that command, but keeps the reminder, ledger, delays, visible
verification stops, and audit evidence.

Inspect the local count and lock owner without changing state. A repair removes
only a lock whose recorded PID is no longer running; it refuses active or
unparseable locks:

```powershell
instsci chinese-quota status
instsci chinese-quota repair
```

Zotero sync also supports Chinese records without DOI: when a successful row has `zotero_item_key` and a PDF, `instsci zotero handoff` creates an `attachment_only` action and `instsci zotero sync` links the PDF to the existing Zotero item.
## Zotero

The preferred Zotero flow is boring on purpose: item plus matching PDF attachment.

```powershell
instsci zotero sync .\runs\papers --attachment-mode linked_file
```

By default, Zotero sync only includes successful rows with an existing PDF. Process state stays in InstSci manifests; Zotero stays clean.

This pairs well with local attachment-management tools such as Zotero Attanger, because the InstSci manifest keeps the original PDF path and Zotero becomes the long-term literature interface.

## Public Data and Private Evidence

The repository ships public route knowledge and anonymized planning summaries in
`instsci/data`. Institution-specific browser results, screenshots, local paths,
subscription observations, cookies, and browser profiles do not belong in the
public package.

Register a private run in the external reference-only index when it needs to be
tracked beyond its task archive:

```powershell
instsci evidence policy
instsci evidence register ..\..\runtime\runs\example_run --publisher elsevier
instsci evidence list
```

The default index is `~/.instsci/private-evidence/index.json`. Registration
stores the original run path and manifest SHA-256; it does not copy PDFs,
screenshots, cookies, or browser profiles. `public-audit` rejects private-evidence
directories, `*.private.json`, screenshots, HAR/storage-state files, browser
profiles, key material, cleartext secrets, and local Windows/POSIX user paths.
In a Git checkout it scans tracked plus non-ignored untracked files, so ignored
local prototypes are not mistaken for release contents.

## Review Checks

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
python -B -m py_compile (Get-ChildItem .\instsci -Recurse -Filter *.py | ForEach-Object FullName)
python -B -m unittest discover -s instsci/tests -v
python -B -m instsci.cli public-audit .
python -B -m instsci.cli doctor --full --package-path .
```

Every push and pull request runs the Windows CI release gate: the complete unit
and regression suite, `public-audit`, wheel/sdist build, `twine check`, and a
wheel-content check that rejects bundled `instsci.tests`. Run the commands above
locally before pushing; rely on the CI result instead of a static test count.

## Access and Compliance

InstSci Workflow is intended to make lawful literature acquisition more batchable and diagnosable. Users must rely on Open Access routes, their own library subscriptions, institutional entitlements, interlibrary loan, or other lawful access paths.

The tool does not ask for passwords and should not automate credentials, OTPs, CAPTCHA, or publisher challenges. When those appear, the user completes them manually in the visible browser.

Login persistence is local. InstSci reuses a persistent CloakBrowser profile and long-lived publisher broker, but it does not store your institution password. Exported cookies are not treated as a complete login state, and runtime profiles, cookies, broker queues, and run outputs are ignored by Git.

## Attribution

This repository is a modified public preview build based on the MIT-licensed InstSci project. See `LICENSE` and `NOTICE_MODIFIED.md` for attribution and license details.
