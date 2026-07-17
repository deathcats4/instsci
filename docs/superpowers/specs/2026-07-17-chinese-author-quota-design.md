# Chinese Literature Author Disambiguation and Download Policy Design

## Goal

Make CNKI and Wanfang search-first downloads safer by using the first author to
disambiguate duplicate exact-title search results and by enforcing a persistent
local limit of 100 Chinese-literature download attempts per calendar day.

## Scope

This change covers the existing `cnki-batch` and `wanfang-batch` visible-browser
commands. It does not add CQVIP, SinoMed, Duxiu/Chaoxing, HTTP-only download
routes, parallel portal tabs, or automatic CAPTCHA handling.

## Input Contract

Each CNKI or Wanfang batch row continues to require `record_id` and `title`.
The row may additionally contain:

- `first_author`: an explicit first-author name.
- `authors`: an ordered JSON array of author names.

`first_author` takes precedence when both fields are present. Otherwise InstSci
uses only the first non-empty element of `authors`. The loader must not split an
author string on commas because English names may use the `Family, Given`
format. Existing rows without author metadata remain valid.

Normalized batch records and manifests preserve `first_author`. The complete
author list is not required after the first author has been derived.

## Search-Result Selection

Candidate identity is evaluated within a single result row; page-level author
or title text is never accepted as evidence for a different row.

CNKI must visibly activate relevance sorting before collecting candidates.
This makes older exact-title rows reachable instead of leaving the result list
in publication-time order. A missing control, failed activation, or timeout is
a fail-closed result: do not select, reserve an attempt, use a direct fallback, or
capture a PDF.

Selection order is:

1. Collect result rows whose normalized title exactly matches the requested
   title.
2. A `record_id` match may rank or select only within those exact-title rows;
   an ID-like user label never overrides a title mismatch.
3. If there is exactly one exact-title row, select it without requiring author
   metadata. This preserves current compatibility.
4. If there is more than one exact-title row, require `first_author`, extract an
   ordered list from explicit same-row author nodes, and keep only rows whose
   normalized first entry equals the requested first author.
5. Select only when author filtering leaves exactly one row.
6. If the first author is unavailable, no row matches it, or multiple rows still
   match it, do not click a download control. Return an explicit ambiguous
   result for manual review.

Normalization removes whitespace and footnote punctuation and compares
case-insensitively. It does not transliterate Chinese names, guess aliases, or
search the full result-row text. Strong author separators preserve order;
ambiguous author containers fail closed. The browser code reports exact-title
candidate count, extracted first author, author-match count, whether author
disambiguation was used, and the selected row identity.
Portal-specific leaf author nodes take precedence over parent metadata
containers, and issue/date labels are excluded from the ordered author list.

The first version evaluates loaded result rows and does not automatically walk
pagination. If the visible portal state does not provide a unique candidate,
the safe outcome is manual review rather than selecting the first row.

## Download Verification

Existing PDF header, size, and title checks remain mandatory.

When a unique title row was selected without author disambiguation, the current
title-based success rule remains unchanged. When author disambiguation was used,
InstSci locates the requested title on the first page, extracts the immediately
following signature author line, and compares only its first author. Names found
only in the body, acknowledgements, or references do not count. If the PDF is
valid but the required first author does not match, keep the file as
`file_status=unverified` with `standard_status=pdf_candidate_conflict`.

Each manifest row records:

- `first_author`
- `title_candidate_count`
- `author_disambiguation_used`
- `author_match_count`
- `author_match`

Rows blocked before download because the candidate remained ambiguous use
`file_status=missing`, `standard_status=ambiguous_search_result`, and
`result_evidence=browser_verified`, with a next action directing the user to
inspect the visible search results.

## Configurable Download Safety Policy

CNKI and Wanfang share a local attempt ledger, not a default hard quota. The
default combined warning threshold is 100 attempts per local calendar day.
Crossing it prints a reminder without blocking; 100 is an InstSci conservative
default, not a uniform official CNKI or Wanfang limit. Retries, resumed runs,
and failed clicks count as attempts.

Hard limits are unset by default. `Config` may define a combined daily limit,
a CNKI limit, and a Wanfang limit. Batch CLI options may temporarily set the
current portal limit or disable configured hard limits for that command. Only
an explicitly configured hard limit can produce `daily_limit_reached`.

The attempt ledger is stored under `Config.cache_dir`, outside the source tree and
outside run evidence. Before calling the portal-specific capture function, the
batch command atomically reserves one attempt. Reservation happens only after
navigation and candidate selection have reached the point where InstSci is
about to invoke the download control.

The ledger contains the local date and an append-only list of reservations with
timestamp, portal, and record ID. Old dates may be pruned when the ledger is
successfully rewritten. A small cross-process lock and atomic file replacement
prevent two local processes from losing reservations or bypassing a configured
limit concurrently. Existing v1 ledgers remain valid because each reservation
already records its portal.

If an applicable configured limit is already exhausted, the current row is written with
`file_status=missing`, `standard_status=daily_limit_reached`, and
`result_evidence=not_verified`; the remaining batch stops without clicking.
The manifest includes combined and portal limits, counts, remaining values,
warning state, limit scope, and ledger date.

If the ledger cannot be parsed, locked, or written safely, InstSci fails closed:
it performs no download and reports a quota-state error. It never silently
resets a corrupt ledger to zero. The ledger applies only to attempts initiated
by this local InstSci installation and cannot account for manual downloads,
other devices, or other users behind the same institutional exit IP.

`instsci chinese-quota status` reports combined and per-portal counts, effective
configured limits, warning state, lock PID, and whether the lock is stale
without changing state. `instsci chinese-quota repair` removes
only a lock whose recorded PID is no longer running and whose contents did not
change during the check. Active, changed, or unparseable locks remain untouched.

## Components

### `instsci/chinese_download_quota.py`

Owns quota state, locking, atomic reservation, date handling, and quota result
objects. It has no browser dependencies and accepts an injectable clock for
deterministic tests.

### `instsci/cnki_session.py`

Preserves the first author from batch input and returns structured candidate
selection evidence. CNKI link selection uses stable record ID first, then exact
title, then first-author disambiguation.

### `instsci/wanfang_session.py`

Preserves the first author from batch input. Wanfang extraction and pre-click
revalidation bind author evidence to the same result-row container as the title
and download control.

### `instsci/cli.py`

Creates the shared attempt ledger from `Config.cache_dir`, resolves the local
safety policy, reserves immediately before capture, stops safely on an explicit
hard-limit or ledger failure, adds identity evidence to each
manifest row, and conditionally requires PDF author verification.

### Documentation and Tests

README examples document `authors` and `first_author`, ambiguity behavior, the
non-blocking reminder, and configurable hard limits. Unit tests cover loaders,
both portal selectors, PDF verification, ledger persistence, cross-portal and
per-portal counts, next-day reset, configured limit exhaustion, default
non-blocking behavior, corrupt-ledger failure, PID-checked stale-lock repair, and
lock-safe reservations. Behavior tests invoke both batch commands with mocked
browser pages and prove that ambiguity, exhaustion, and corrupt state never call
capture; retries reserve twice; and independent commands share the ledger.
Existing rows without authors and unique-title downloads remain covered as
compatibility cases.

## Error Handling

- Missing author with duplicate exact titles: no click; manual review.
- Zero or multiple author matches: no click; manual review.
- Candidate changes between inspection and click: no click; existing drift
  protection remains active.
- Valid PDF with failed required author check: retain as unverified candidate
  conflict.
- Configured daily hard limit exhausted: no click; write checkpoint and stop the batch.
- Quota storage error or corrupt ledger: no click; report the storage failure
  and stop the batch.
- CAPTCHA or institutional authentication: preserve the existing visible-user
  workflow and reserve an attempt only after that workflow reaches download capture.

## Acceptance Criteria

1. Existing author-less unique-title batches remain valid.
2. Multiple exact-title candidates are never auto-selected without a uniquely
   extracted same-row first author; later coauthors never match.
3. Author disambiguation evidence survives into the manifest and, when used,
   becomes part of final PDF identity verification.
4. With default policy, the 100th combined attempt emits a non-blocking reminder
   and the 101st remains allowed. With a configured combined limit of 100, the
   101st attempt is blocked across separate processes or resumed runs.
5. A new local date starts with a fresh allowance without discarding prior-day
   evidence unsafely.
6. Corrupt or unavailable quota state blocks downloads rather than resetting.
7. A `record_id` match never bypasses exact-title verification.
8. Required PDF authors are verified only from the title-adjacent first-page
   signature, not the whole document.
9. A search result with no exact title never reaches quota reservation or PDF
   capture.
10. PDF text extraction may split a Chinese first-author name across adjacent
    lines; only the explicitly requested title-adjacent first author may be
    reassembled.
11. No real portal download is required by the automated test suite.
12. CNKI exact-title selection occurs only after relevance sorting is confirmed
    active; sort failure consumes no quota and performs no capture.
13. CNKI and Wanfang hard limits can be configured independently; one portal's
    limit does not block the other unless a combined hard limit is configured.
