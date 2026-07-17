# Chinese Literature Author Disambiguation and Daily Quota Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add first-author disambiguation for duplicate CNKI/Wanfang title results and enforce a persistent shared limit of 100 local Chinese-literature download attempts per day.

**Architecture:** Shared identity helpers derive and normalize the first author, while each portal keeps DOM-specific candidate extraction and pre-click revalidation. A browser-independent quota ledger uses an exclusive lock plus atomic replacement; the CLI reserves quota immediately before capture and records selection/quota evidence in each manifest row.

**Tech Stack:** Python 3.10+, standard-library `json`, `os`, `datetime`, `pathlib`, existing BeautifulSoup/Playwright-compatible page APIs, `unittest`, Typer CLI.

## Global Constraints

- CNKI and Wanfang share one local-calendar-day limit of exactly 100 download attempts.
- Failed attempts and retries consume quota; ambiguous candidates blocked before capture do not.
- Existing author-less rows remain valid when an exact title identifies one loaded result row.
- Duplicate exact-title rows require one unique first-author match in the same result row unless a stable record ID uniquely matches.
- If author disambiguation was required, downloaded PDF text must match both title and first author.
- Corrupt, locked, or unwritable quota state fails closed and performs no download.
- Automated tests must not call a live portal or start a browser.
- Preserve the visible-browser CAPTCHA/SSO workflow and do not add parallel portal tabs.

---

### Task 1: Shared First-Author Input Contract

**Files:**
- Modify: `instsci/chinese_literature.py`
- Modify: `instsci/cnki_session.py:48-79`
- Modify: `instsci/wanfang_session.py:50-84`
- Test: `instsci/tests/test_chinese_literature.py`
- Test: `instsci/tests/test_cnki_session.py`
- Test: `instsci/tests/test_wanfang_session.py`

**Interfaces:**
- Produces: `first_author_from_record(record: Mapping[str, object]) -> str`
- Produces: `normalize_author_name(value: object) -> str`
- Consumed by: both batch loaders, portal selection logic, and CLI PDF verification.

- [ ] **Step 1: Write failing identity and loader tests**

Add tests proving explicit precedence, ordered-list fallback, comma preservation, missing-author compatibility, and rejection of a non-list `authors` value:

```python
def test_first_author_prefers_explicit_value(self) -> None:
    self.assertEqual(
        first_author_from_record({"first_author": "张三", "authors": ["李四", "王五"]}),
        "张三",
    )

def test_first_author_uses_first_nonempty_ordered_author(self) -> None:
    self.assertEqual(first_author_from_record({"authors": ["", "Smith, John", "李四"]}), "Smith, John")

def test_batch_loader_preserves_first_author(self) -> None:
    source.write_text('[{"record_id":"safe","title":"测试","authors":["张三","李四"]}]', encoding="utf-8")
    self.assertEqual(load_wanfang_batch(source)[0]["first_author"], "张三")
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```powershell
python -m unittest instsci.tests.test_chinese_literature instsci.tests.test_cnki_session instsci.tests.test_wanfang_session -v
```

Expected: failures because the shared helper does not exist and loaders omit `first_author`.

- [ ] **Step 3: Implement the minimal shared helpers and loader fields**

Add to `chinese_literature.py`:

```python
from collections.abc import Mapping

def normalize_author_name(value: object) -> str:
    return "".join(character.casefold() for character in str(value or "") if character.isalpha())

def first_author_from_record(record: Mapping[str, object]) -> str:
    explicit = str(record.get("first_author") or "").strip()
    if explicit:
        return explicit
    authors = record.get("authors")
    if authors is None:
        return ""
    if not isinstance(authors, list):
        raise ValueError("authors must be an ordered JSON array")
    return next((str(author).strip() for author in authors if str(author).strip()), "")
```

Import `first_author_from_record` in both portal modules, wrap its `ValueError` with the batch row number, and add `"first_author": first_author_from_record(raw)` to normalized records.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run the Step 2 command. Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 1**

```powershell
git add instsci/chinese_literature.py instsci/cnki_session.py instsci/wanfang_session.py instsci/tests/test_chinese_literature.py instsci/tests/test_cnki_session.py instsci/tests/test_wanfang_session.py
git commit -m "feat: preserve first authors in Chinese batches"
```

---

### Task 2: CNKI Stable-ID and First-Author Candidate Selection

**Files:**
- Modify: `instsci/cnki_session.py:204-345`
- Test: `instsci/tests/test_cnki_session.py`

**Interfaces:**
- Consumes: `normalize_author_name()` and normalized `first_author`.
- Produces: `choose_cnki_search_candidate(candidates, *, title, record_id="", first_author="") -> dict[str, object]`
- Changes: `click_cnki_search_result(..., first_author="")` and `navigate_cnki_article_via_search(..., first_author="")`.

- [ ] **Step 1: Write failing pure-selection tests**

Cover these independent cases:

```python
def test_cnki_duplicate_titles_require_unique_first_author(self) -> None:
    result = choose_cnki_search_candidate(
        [
            {"index": 0, "href": "https://kns.cnki.net/detail?a", "title": "同题", "row_text": "同题 张三"},
            {"index": 1, "href": "https://kns.cnki.net/detail?b", "title": "同题", "row_text": "同题 李四"},
        ],
        title="同题",
        first_author="李四",
    )
    self.assertTrue(result["selected"])
    self.assertEqual(result["candidate"]["index"], 1)
    self.assertEqual(result["title_candidate_count"], 2)
    self.assertTrue(result["author_disambiguation_used"])

def test_cnki_duplicate_titles_without_author_are_ambiguous(self) -> None:
    result = choose_cnki_search_candidate(candidates, title="同题")
    self.assertFalse(result["selected"])
    self.assertEqual(result["reason"], "ambiguous_search_result")

def test_cnki_unique_stable_id_wins_before_author(self) -> None:
    result = choose_cnki_search_candidate(candidates, title="同题", record_id="ABC123", first_author="不存在")
    self.assertTrue(result["selected"])
    self.assertEqual(result["selection_method"], "record_id")
```

Also test one exact-title candidate without author, zero author matches, and multiple author matches.

- [ ] **Step 2: Run CNKI tests and verify RED**

```powershell
python -m unittest instsci.tests.test_cnki_session -v
```

Expected: import or attribute failure for `choose_cnki_search_candidate`.

- [ ] **Step 3: Implement pure selection and browser collection/revalidation**

Implement the pure selector with this result contract:

```python
{
    "selected": bool,
    "candidate": dict | None,
    "reason": "" | "no_exact_title_result" | "ambiguous_search_result",
    "selection_method": "record_id" | "exact_title" | "first_author" | "",
    "title_candidate_count": int,
    "author_match_count": int,
    "author_disambiguation_used": bool,
}
```

Refactor the first browser evaluation to collect safe dictionaries containing `candidate_id`, `href`, `title`, and same-row `row_text` without clicking. Use the pure selector, then run a second evaluation that finds the marked candidate, recomputes its href/title/row text, rejects drift as `candidate_changed`, and clicks only after the selected identity still matches. Pass `first_author` through `navigate_cnki_article_via_search` and include the selection evidence under `search_result`.

Do not use the direct URL fallback when `reason == "ambiguous_search_result"`; an ambiguous search must remain blocked.

- [ ] **Step 4: Run CNKI tests and verify GREEN**

Run the Step 2 command. Expected: all CNKI tests pass.

- [ ] **Step 5: Commit Task 2**

```powershell
git add instsci/cnki_session.py instsci/tests/test_cnki_session.py
git commit -m "feat: disambiguate CNKI results by first author"
```

---

### Task 3: Wanfang Same-Row First-Author Selection

**Files:**
- Modify: `instsci/wanfang_session.py:27-590`
- Test: `instsci/tests/test_wanfang_session.py`

**Interfaces:**
- Consumes: `normalize_author_name()`.
- Changes: `extract_wanfang_download_candidates_from_html(..., first_author="")`.
- Changes: `choose_wanfang_download_candidate(..., first_author="")`.
- Produces: `inspect_wanfang_result_download(page, *, title, first_author="") -> dict[str, object]`.
- Changes: `click_wanfang_result_download(..., first_author="", selection=None)` and `capture_wanfang_pdf(..., first_author="", selection=None)`.

- [ ] **Step 1: Write failing Wanfang author-selection tests**

Use HTML with two identical titles and distinct `.author` text. Assert that the requested first author selects the correct row, missing author returns ambiguity, duplicate author remains ambiguous, and a unique title remains compatible without author. Extend the drift-page test so the second evaluation changes the author text and must return `candidate_changed`.

```python
candidates = extract_wanfang_download_candidates_from_html(html, title="同题", first_author="李四")
chosen = choose_wanfang_download_candidate(candidates, title="同题", first_author="李四")
self.assertEqual(chosen["row_index"], 1)
self.assertEqual(chosen["title_candidate_count"], 2)
self.assertEqual(chosen["author_match_count"], 1)
self.assertTrue(chosen["author_disambiguation_used"])
```

- [ ] **Step 2: Run Wanfang tests and verify RED**

```powershell
python -m unittest instsci.tests.test_wanfang_session -v
```

Expected: new keyword arguments or expected author evidence are unsupported.

- [ ] **Step 3: Implement same-row author extraction, inspection, and click**

Add a conservative result-author selector covering common author containers and attributes:

```python
WANFANG_RESULT_AUTHOR_SELECTOR = ".author,.authors,.writer,[class*='author'],[class*='writer']"
```

For fixture extraction and browser evaluation, store `row_author_text` from the same result-row container. Count distinct exact-title rows, not download controls. When more than one exact-title row exists, require exactly one normalized first-author containment match. Add selection evidence to the chosen candidate.

Split inspection from click: `inspect_wanfang_result_download` collects and chooses without mutation; `click_wanfang_result_download` accepts the inspected selection and revalidates title, author, href, label, and candidate ID before clicking. Preserve the old call shape by inspecting internally when `selection` is omitted.

- [ ] **Step 4: Run Wanfang tests and verify GREEN**

Run the Step 2 command. Expected: all Wanfang tests pass.

- [ ] **Step 5: Commit Task 3**

```powershell
git add instsci/wanfang_session.py instsci/tests/test_wanfang_session.py
git commit -m "feat: disambiguate Wanfang results by first author"
```

---

### Task 4: Persistent Shared Daily Quota Ledger

**Files:**
- Create: `instsci/chinese_download_quota.py`
- Create: `instsci/tests/test_chinese_download_quota.py`

**Interfaces:**
- Produces: `DAILY_DOWNLOAD_LIMIT = 100`.
- Produces: `ChineseDownloadQuotaError(RuntimeError)`.
- Produces: immutable `QuotaReservation` with `allowed`, `date`, `limit`, `used`, `remaining`, `portal`, `record_id`, and `reason`.
- Produces: `reserve_chinese_download(ledger_path, *, portal, record_id, now=None, limit=100, lock_timeout=5.0) -> QuotaReservation`.

- [ ] **Step 1: Write failing quota tests**

Add tests for first reservation, CNKI/Wanfang shared counting, persistence across calls, 100 allowed and 101st blocked, next-day reset, corrupt JSON failure, invalid schema failure, and a pre-existing lock timing out without changing the ledger.

```python
def test_cnki_and_wanfang_share_one_daily_limit(self) -> None:
    first = reserve_chinese_download(path, portal="cnki", record_id="a", now=fixed)
    second = reserve_chinese_download(path, portal="wanfang", record_id="b", now=fixed)
    self.assertEqual((first.used, second.used), (1, 2))

def test_combined_101st_attempt_is_blocked(self) -> None:
    for index in range(100):
        self.assertTrue(reserve_chinese_download(path, portal="cnki", record_id=str(index), now=fixed).allowed)
    blocked = reserve_chinese_download(path, portal="wanfang", record_id="101", now=fixed)
    self.assertFalse(blocked.allowed)
    self.assertEqual(blocked.reason, "daily_limit_reached")
```

- [ ] **Step 2: Run quota tests and verify RED**

```powershell
python -m unittest instsci.tests.test_chinese_download_quota -v
```

Expected: module import failure.

- [ ] **Step 3: Implement lock, validation, and atomic reservation**

Use the ledger schema:

```json
{
  "schema": "instsci.chinese_download_quota.v1",
  "days": {
    "2026-07-17": [
      {"attempted_at": "2026-07-17T12:00:00+08:00", "portal": "cnki", "record_id": "ABC"}
    ]
  }
}
```

Acquire `ledger_path.with_suffix(ledger_path.suffix + ".lock")` using `os.open(..., os.O_CREAT | os.O_EXCL | os.O_WRONLY)`, polling until `lock_timeout`. Within the lock, parse and validate the schema and `days` lists, refuse corrupt state, append only when below the limit, write a same-directory temporary JSON file, flush and `os.fsync`, then `os.replace`. Remove the lock in `finally`. Use `datetime.now().astimezone()` when `now` is omitted and normalize a naive injected datetime with the local timezone.

- [ ] **Step 4: Run quota tests and verify GREEN**

Run the Step 2 command. Expected: all quota tests pass.

- [ ] **Step 5: Commit Task 4**

```powershell
git add instsci/chinese_download_quota.py instsci/tests/test_chinese_download_quota.py
git commit -m "feat: enforce shared Chinese download quota"
```

---

### Task 5: CLI Safety Integration and Final PDF Author Verification

**Files:**
- Modify: `instsci/cli.py:57-75,1662-2176`
- Modify: `instsci/wanfang_session.py:229-263`
- Modify: `instsci/publisher_matrix.py:20-40,276-331`
- Modify: `instsci/tests/test_wanfang_session.py`
- Modify: `instsci/tests/test_status_contract.py`
- Create: `instsci/tests/test_chinese_batch_safety.py`

**Interfaces:**
- Adds standard statuses: `ambiguous_search_result`, `daily_limit_reached`, `quota_state_error`.
- Produces: `_chinese_quota_ledger_path(config: Config) -> Path`.
- Produces: `_verify_chinese_pdf_identity(title, first_author, text, *, author_required) -> dict[str, bool]`.
- Consumes: portal selection evidence and `reserve_chinese_download()`.

- [ ] **Step 1: Write failing PDF/status/quota-integration tests**

Test the pure PDF identity helper with author required and optional. Extend Wanfang summary tests with `first_author` and `author_required`. Extend status-contract expectations:

```python
self.assertEqual(manifest_next_action("ambiguous_search_result"), "inspect_visible_search_results_and_select_manually")
self.assertEqual(manifest_next_action("daily_limit_reached"), "stop_batch_and_resume_next_local_day")
self.assertEqual(manifest_next_action("quota_state_error"), "inspect_or_repair_local_quota_state_before_retry")
```

Add source-level integration assertions in `test_chinese_batch_safety.py` that both batch loops call `reserve_chinese_download` before their portal capture call, pass `first_author`, and handle all three statuses. These tests complement pure unit coverage without launching a browser.

- [ ] **Step 2: Run focused tests and verify RED**

```powershell
python -m unittest instsci.tests.test_wanfang_session instsci.tests.test_status_contract instsci.tests.test_chinese_batch_safety -v
```

Expected: missing helpers/status mappings and absent CLI quota integration.

- [ ] **Step 3: Implement status mappings and identity helper**

Add the three statuses to `STANDARD_STATUSES`. Add suggested paths:

```python
"ambiguous_search_result": ["manual_browser_single_doi", "rerun_diagnose"],
"daily_limit_reached": ["stop_batch", "retry_next_day"],
"quota_state_error": ["inspect_local_state", "stop_batch"],
```

Implement corresponding `manifest_next_action` branches. Implement PDF identity output with `title_match`, `author_match`, and `verified`; `verified` requires author only when `author_required` is true.

- [ ] **Step 4: Integrate CNKI batch safety**

Pass `first_author` to every initial and resumed `navigate_cnki_article_via_search` call. Copy search selection evidence to the manifest. If navigation reports `ambiguous_search_result`, checkpoint the row as missing/browser-verified and continue without capture.

Immediately before each `capture_cnki_pdf` call, reserve from `Path(cfg.cache_dir) / "chinese_download_quota.json"`. On exhaustion, checkpoint `daily_limit_reached` with the quota snapshot and break. On `ChineseDownloadQuotaError`, checkpoint `quota_state_error` and break. Apply conditional PDF author verification when `author_disambiguation_used` is true.

- [ ] **Step 5: Integrate Wanfang batch safety**

Inspect the result candidate with title and first author before reservation. Handle ambiguity without quota consumption. Reserve quota, then call `capture_wanfang_pdf` with the inspected selection so the click revalidates the same row. Apply conditional PDF author verification through `summarize_wanfang_capture_result` and record selection/quota evidence in the manifest. Stop on exhausted or unsafe quota state.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run the Step 2 command plus:

```powershell
python -m unittest instsci.tests.test_cnki_session instsci.tests.test_chinese_download_quota -v
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit Task 5**

```powershell
git add instsci/cli.py instsci/wanfang_session.py instsci/publisher_matrix.py instsci/tests/test_wanfang_session.py instsci/tests/test_status_contract.py instsci/tests/test_chinese_batch_safety.py
git commit -m "feat: guard Chinese batch identity and daily volume"
```

---

### Task 6: Public Documentation and Full Verification

**Files:**
- Modify: `README.md:168-175`
- Modify: `skills/instsci/SKILL.md`
- Modify: `instsci/tests/test_public_language.py` only if existing public-language assertions require the new wording to be represented explicitly.

**Interfaces:**
- Documents the accepted batch JSON fields, exact ambiguity behavior, local shared limit, and local-only accounting boundary.

- [ ] **Step 1: Write a failing documentation assertion**

Add an assertion that README contains `first_author`, `authors`, `100`, `CNKI`, and `Wanfang` in the Chinese literature section, and that the distributed skill states duplicate-title author disambiguation and the shared daily cap.

- [ ] **Step 2: Run the documentation test and verify RED**

```powershell
python -m unittest instsci.tests.test_public_language -v
```

Expected: the new public contract wording is absent.

- [ ] **Step 3: Update README and the distributed InstSci skill**

Document a JSON example with ordered `authors`, explicit `first_author` precedence, manual review on unresolved duplicate titles, author-required PDF verification after disambiguation, and the combined local 100-attempt cap. State that failed attempts count and other devices/manual downloads are not visible to the ledger.

- [ ] **Step 4: Run focused and full verification**

```powershell
python -m unittest instsci.tests.test_chinese_literature instsci.tests.test_cnki_session instsci.tests.test_wanfang_session instsci.tests.test_chinese_download_quota instsci.tests.test_chinese_batch_safety instsci.tests.test_status_contract instsci.tests.test_public_language -v
python -m unittest discover -s instsci/tests -p 'test_*.py'
& '..\..\scripts\Check.ps1'
git diff --check
git status --short --branch
```

Expected: all tests pass with the existing single environment-dependent skip, `Check.ps1` and public audit pass, `git diff --check` is silent, and status lists only intended feature files before commit.

- [ ] **Step 5: Commit Task 6**

```powershell
git add README.md skills/instsci/SKILL.md instsci/tests/test_public_language.py
git commit -m "docs: explain Chinese download safeguards"
```

- [ ] **Step 6: Record final branch evidence**

```powershell
git log --oneline origin/main..HEAD
git status --short --branch
```

Expected: the design, plan, author contract, both portal selectors, quota ledger, CLI integration, and documentation commits are present; the worktree is clean and ahead of `origin/main` only by this feature series.
