---
name: instsci
description: Use when working with the InstSci project, publisher PDF retrieval, closed-access article verification, DOI batch downloads, CloakBrowser evidence, CARSI, Shibboleth, OpenAthens, WebVPN, publisher capability matrices, or InstSci CLI workflows.
---

# instsci

## Core Rule

Use this self-contained skill as the project entry point for normal InstSci CLI work. A source checkout is required only for development tasks.

## Startup

1. For normal CLI use, work from the user's research project or chosen output directory; do not require an InstSci source checkout.
2. For code changes, locate the checkout containing `AGENTS.md` and `pyproject.toml`, work from that root, and read `AGENTS.md` first. When a checkout is already present during publisher work, also treat its `AGENTS.md` and `instsci/data/*.json` as authoritative.
3. For continuation, recall, migration, or "previous task" questions, use the `chatmem` skill/MCP first when it is available. Treat indexed history as evidence, not approved startup rules.
4. For publisher PDF, closed-access, institution-login, or capability-matrix tasks, also read `instsci/data/institutional_identity_policy.json` or run:

```powershell
instsci identity-policy
```

## MCP Coordination

When InstSci MCP tools are available, use them as the structured context bridge before reading raw JSON files by hand:

- `get_institutional_identity_policy`: load route-selection policy before closed-access planning.
- `get_publisher_access_catalog`: inspect publisher route templates, login hints, persistence stores, and HTTP preflight limits.
- `get_publisher_capability_summary`: inspect the public route-planning summary; it contains no browser or entitlement verdicts.
- `plan_publisher_pdf_workflow`: build the correct visible CLI command and identify whether a subscription institution is still required.

Use MCP `search_papers`, `get_paper_metadata`, and `fetch_paper` for metadata, Open Access lookup, DOI resolution, or non-final retrieval attempts. For publisher PDF downloads, closed-access verification, capability matrices, or final support verdicts, MCP is planning/context only; the actual evidence must come from the visible CloakBrowser workflow started by `instsci papers`, `instsci publisher-batch`, `PublisherBatchDownloader`, or `ACSCloakBatchDownloader`.

If MCP output and repository files disagree, treat `AGENTS.md` plus `instsci/data/*.json` as the source of truth and mention the mismatch.

## Evidence Standard

Final publisher PDF verdicts require the visible built-in CloakBrowser workflow. `curl`, `requests`, DOI resolution, `publisher-doctor`, route construction, logs, DOM state, URLs, and cookie exports are HTTP preflight only.

Accepted browser-backed routes include:

```powershell
instsci papers dois.txt --publisher auto --institution "Institution Name" --output .\runs\papers
instsci publisher-batch dois.txt --publisher acs --institution "Institution Name" --output .\runs\acs
```

Code-level work may use `PublisherBatchDownloader`, `ACSCloakBatchDownloader`, or the same visible built-in browser context.

## Chinese Literature Portals

Treat Chinese literature databases as visible-browser portal workflows, not as
ordinary direct PDF URLs. Inspect support with:

```powershell
instsci chinese-literature-sites
```

- Production default: treat CNKI and Wanfang as the download-verified Chinese
  literature portals. `download_verified_portals` should be `["cnki", "wanfang"]`.
- CNKI is the primary Chinese full-text route: use the persistent CNKI profile
  and the search-first batch path (`instsci cnki-batch ... --navigation-mode search`).
  Prefer homepage/search-result navigation before saved detail URLs because
  direct article/download URLs are more likely to trigger click-word verification.
- Wanfang is a browser-verified search-download route: start at
  `s.wanfangdata.com.cn`, click the result-row `下载` control, and capture the
  PDF from the `Fulltext/Download` popup. Keep this flow in the same visible
  browser context because the popup URL is generated per session. For batches,
  use `instsci wanfang-batch records.json --output .\runs\wanfang`.
- CQVIP is a manual broker only, not download-verified. The visible route can
  reach a `www.cqvip.com` article page and `PDF下载`; manual `IP登录` can redirect
  to `qikan.cqvip.com`, but qikan rendered blank in CloakBrowser and HTTP
  preflight showed CQVIP cache-server 403/412 challenge responses. The tested
  institution resource portal did not visibly list CQVIP, so entitlement is
  unconfirmed. Do not force CQVIP automation or include it in default Chinese
  literature batch downloads unless qikan page load and institution entitlement
  are freshly browser-verified.
- SinoMed and Duxiu/Chaoxing remain planned portal profiles until
  screenshot-backed CloakBrowser runs verify their route and blocker states.
- Prefer homepage/search-result navigation before saved article detail URLs for
  every Chinese literature portal. Avoid parallel tabs until a portal is
  browser-verified and rate behavior is known.
- If CAPTCHA, SSO, reader checks, delivery flows, or other human verification
  appear, let the user complete them in the visible browser. Do not auto-solve,
  bypass, export cookies, or treat HTTP-only probes as final download evidence.

## Elsevier API Setup

For Elsevier or ScienceDirect DOI retrieval, guide the user to configure a global Elsevier API key once:

```powershell
instsci elsevier-setup --api-key YOUR_ELSEVIER_KEY --validate
```

- The key is global InstSci config, not per article; `--test-doi` is validation only.
- Inst Token is optional. Configure `--inst-token` only when the user's library explicitly provides an Elsevier institutional token.
- The preferred API route is `view=FULL XML -> object/eid -> PDF`.
- Use direct-first routing so `api.elsevier.com` can use campus, school VPN, rule VPN, or library exit before any configured proxy fallback.
- Do not write API keys, Inst Tokens, cookies, or entitlement details into docs, logs, skill files, or commits.
- API success is HTTP preflight/API-route evidence. Final publisher PDF verdicts still require visible CloakBrowser evidence when the task asks for closed-access publisher capability.

## Institution Route

- Do not default to Example University or any other school.
- Resolve subscription institution in this order: explicit `--institution`, `config.carsi_idp_name`, `config.institution_name_en`, `config.institution_name_zh`, `config.school`, then ask the user.
- When the public placeholder directory does not contain the user's institution, use MCP `configure_institution` or the CLI institution-name options instead of requiring a directory entry.
- Prefer publisher broker, Shibboleth, OpenAthens, CARSI, or configured WAYFless institution links before WebVPN.
- Use WebVPN only when the configured institution has a WebVPN gateway and that route is browser-verified for the publisher.
- Do not treat `cookies.json` or `carsi_cookie_dir/*.json` as a full reusable login state; they are preflight/supporting assets, not final evidence.

## Reporting

For publisher PDF work, report each DOI or publisher with `publisher`, `doi`, `route_attempted`, `institution`, `file_status`, `standard_status`, `result_evidence`, `evidence`, and `next_action`.

Use the three-layer status contract:

- `file_status`: file outcome only. Allowed values are `success`, `unverified`, and `missing`.
- `standard_status`: user/workflow meaning. Common values are `success`, `auth_required`, `access_unavailable`, `waf_blocked`, `human_verification_required`, `publisher_error`, `capture_failed`, `unsupported_publisher`, and `pdf_candidate_conflict`.
- `result_evidence`: how the result was established. Allowed values are `oa_direct`, `publisher_open_pdf`, `browser_verified`, `http_preflight`, and `not_verified`.

For final manifests, keep Markdown, CSV, and JSON counts consistent. `file_status=success` means downloaded and verified; `file_status=unverified` means a PDF exists but DOI/text verification is insufficient; `file_status=missing` means no PDF was captured. HTTP-only findings are never final closed-access verdicts; final publisher conclusions require `result_evidence=browser_verified`.

## Zotero Sync

Use the complete discovery-to-library path when the user starts from a topic
rather than an existing DOI list:

```powershell
instsci search "research topic" --limit 50 --year 2020- --output .\runs\search.json
instsci select .\runs\search.json --indices "1,3-8" --output .\runs\selected_dois.txt
instsci papers .\runs\selected_dois.txt --publisher auto --output .\runs\papers
instsci zotero sync .\runs\papers --attachment-mode linked_file
```

`search` queries Semantic Scholar, OpenAlex, and Crossref by default and merges
records by normalized DOI. Title-and-year fallback is allowed only when at least
one record lacks a DOI; conflicting non-empty DOI values remain separate. Treat
citation counts as source-specific metadata; do not describe the maximum merged
value as a single authoritative count. Check `source_status` before interpreting
zero hits because a provider can be rate-limited or unavailable.
`search --output` creates reviewable JSON or CSV. `select` uses one-based result
indices, removes duplicate DOI values, skips rows without a DOI, and writes a
neighboring selection report. Do not silently acquire every search hit when the
user asked to review or choose papers first.

After `papers` or `publisher-batch` writes a run manifest, keep Zotero as the long-term paper entry point:

```powershell
instsci zotero handoff .\runs\papers --tags project/my-topic --collections "Collection Name"
instsci zotero sync .\runs\papers --attachment-mode linked_file
```

`handoff` builds the reviewable action queue. `sync` creates or matches the Zotero item, links the matching local PDF as a `linked_file` attachment, writes `zotero_item_key` and `zotero_attachment_key` back into the InstSci manifest, and writes `zotero_sync_report.json`.

Default sync behavior includes only `standard_status=success` rows and requires an existing PDF. Zotero remains clean: item plus PDF attachment only. Do not create Zotero child notes, evidence notes, or process logs.

Use Zotero Storage uploads only when the user explicitly wants imported attachments and has enough Zotero storage. The stable default is `linked_file`, which also works well with Zotero Attanger-style local attachment management.

## Workflow Plan

After a run with failures or unresolved rows, generate a structured next-step acquisition plan:

```powershell
instsci workflow-plan .\runs\papers
```

`workflow-plan` reads the manifest, excludes success rows by default, and writes `workflow_plan.json` beside the manifest. Each attention item keeps the existing `next_action` and adds machine-readable `suggested_paths`, such as `oa_retry`, `library_resolver`, `ill_request`, `author_email`, `rerun_diagnose`, or `manual_browser_single_doi`.

Use this plan to decide the next research workflow. Keep Zotero focused on items and PDFs; keep acquisition process state in InstSci manifests and reports.

## Publisher Matrix

Before running large closed-access batches, inspect publisher readiness with the matrix panel:

```powershell
instsci publisher-doctor --matrix
```

`publisher-doctor --matrix` is a planning view, not a fresh access verdict. It summarizes the canonical public capability summary with `ready`, `prewarm_required`, `waf_risky`, `route_not_published`, `unclassified`, `batch_recommendation`, `known_blocker`, and machine-readable `suggested_paths`.

Use this panel to decide whether to run a normal batch, run a single-DOI prewarm first, switch to a manual browser check, retry later, or avoid a bulk run. For final publisher PDF verdicts, still use the visible CloakBrowser-backed workflow.

## Public and Private Evidence

Treat `instsci/data/*.json` as distributable route knowledge or anonymized
planning summaries only. Keep institution-specific screenshots, subscription
observations, run paths, cookies, and browser state outside the repository.

Use `instsci evidence policy` to inspect the boundary. When a private run needs
long-term traceability, use `instsci evidence register RUN_DIR`; this creates a
reference-only entry under `~/.instsci/private-evidence` with a manifest hash and
does not copy PDFs, screenshots, cookies, or browser profiles. Never publish the
private index. Any proposed public summary must be separately anonymized and pass
`instsci public-audit`.

## Detailed Reference

For recent gotchas, publisher-specific notes, visible-browser UI fallback steps, report-count rules, and verification commands, read `references/publisher-pdf-workflow.md` when the task touches publisher PDFs or DOI batches.

## Safety

- Keep CloakBrowser visible for SSO, CAPTCHA, WAF, Cloudflare, and publisher verification.
- After clicking PDF, institutional access, OpenAthens/Shibboleth/CARSI, cookie prompts, or verification prompts, inspect a screenshot before concluding success or failure.
- Visible UI fallback may click public publisher controls such as `Access through your organization`, institution search results, or PDF viewer `Download`, but never fill passwords, OTPs, or account credentials.
- Do not manually call private or local notification scripts.
- Never write notification endpoints, tokens, institution credentials, cookies, or other secrets into docs, code, logs, skills, or commits.

