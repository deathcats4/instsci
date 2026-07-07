# Attribution and Modified Build Notice

This repository is a modified public preview build based on the MIT-licensed InstSci project.

Original copyright remains with the InstSci contributors. The original MIT license is preserved in `LICENSE` and applies to the included source unless a file states otherwise.

## Why This Build Exists

This build packages an agent-friendly research acquisition workflow around InstSci. The goal is not to rebrand the upstream project as a new original work, but to make a reviewable preview that is easier to test, explain, and connect with Zotero.

## Main Changes in This Build

- Clean public packaging without local profiles, cookies, PDFs, run outputs, or browser binaries.
- Clearer OA-first workflow boundaries.
- Visible-browser evidence rules for closed-access publisher conclusions.
- Three-layer manifest contract: `file_status`, `standard_status`, and `result_evidence`.
- Profile-aware browser diagnostics and publisher workflow guidance.
- Zotero handoff/sync guidance focused on item plus matching PDF attachment.
- Public preview documentation, review checklist, and workflow guidance.

## Compliance Position

InstSci Workflow does not bypass publisher or institutional access controls. Closed-access retrieval requires the user's own legal institutional access. SSO, CAPTCHA, 2FA, WAF, and password prompts must be completed by the user in a visible browser.

If you redistribute this build or derivative work, keep the MIT license notice and preserve attribution to the InstSci contributors.
