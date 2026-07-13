"""Public package audit checks for InstSci review/share bundles."""

from __future__ import annotations

import importlib.util
import json
import platform
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SENSITIVE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("windows_user_path", r"(?i)\b[A-Z]:[\\/]Users[\\/]"),
    ("local_drive_path", r"(?i)\b[A-Z]:[\\/]"),
    ("posix_user_path", r"(?<![\w.-])/(?:home|Users)/[^/\s]+(?:/|\b)"),
    ("auth_header_or_cookie", r"(?i)\b(authorization|set-cookie)\s*[:=]"),
    ("cleartext_secret_assignment", r"(?i)(?<![\w-])['\"]?(password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token)['\"]?\s*[:=]\s*['\"][^'\"*][^'\"]{3,}['\"]"),
    ("python_cache_dir", r"__pycache__"),
    ("python_bytecode", r"\.py[co]$"),
    ("legacy_zotero_note_action", r"zotero_create_note|include_notes|evidence_note|--notes\b|--no-notes\b"),
)

PUBLIC_INSTITUTION_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "specific_institution_trace",
        "|".join(
            [
                "(?i:\\btsinghua\\b)",
                "\u6e05\u534e",
                "\u4e2d\u56fd\u6d77\u6d0b\u5927\u5b66",
                "(?i:webvpn\\.tsinghua)",
                "(?i:tlink\\.lib\\.tsinghua)",
            ]
        ),
    ),
)

PUBLIC_BINARY_ALLOWLIST: frozenset[str] = frozenset()

DANGEROUS_BINARY_SUFFIXES: dict[str, str] = {
    ".png": "screenshot_or_image_included",
    ".jpg": "screenshot_or_image_included",
    ".jpeg": "screenshot_or_image_included",
    ".gif": "screenshot_or_image_included",
    ".webp": "screenshot_or_image_included",
    ".bmp": "screenshot_or_image_included",
    ".tif": "screenshot_or_image_included",
    ".tiff": "screenshot_or_image_included",
    ".har": "browser_session_asset_included",
    ".pem": "certificate_or_private_key_included",
    ".key": "certificate_or_private_key_included",
    ".p12": "certificate_or_private_key_included",
    ".pfx": "certificate_or_private_key_included",
    ".crt": "certificate_or_private_key_included",
    ".cer": "certificate_or_private_key_included",
    ".der": "certificate_or_private_key_included",
    ".jks": "certificate_or_private_key_included",
    ".keystore": "certificate_or_private_key_included",
}

REQUIRED_RUNTIME_MODULES = (
    "typer",
    "rich",
    "requests",
    "bs4",
    "lxml",
    "fitz",
    "mcp",
    "Crypto",
)


@dataclass(frozen=True)
class AuditIssue:
    code: str
    path: str
    line: int
    text: str

    def to_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "path": self.path,
            "line": self.line,
            "text": self.text,
        }


def _iter_files(root: Path) -> list[Path]:
    git_paths = _git_public_paths(root)
    if git_paths is not None:
        return [path for path in git_paths if path.is_file()]
    skipped_dirs = {".git", ".hg", ".svn"}
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            parts = set(path.relative_to(root).parts)
        except ValueError:
            parts = set(path.parts)
        if parts & skipped_dirs:
            continue
        files.append(path)
    return files


def _iter_dirs(root: Path) -> list[Path]:
    git_paths = _git_public_paths(root)
    if git_paths is not None:
        directories: set[Path] = set()
        for path in git_paths:
            parent = path.parent
            while parent != root and root in parent.parents:
                directories.add(parent)
                parent = parent.parent
        return sorted(directories)
    skipped_dirs = {".git", ".hg", ".svn"}
    directories: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_dir():
            continue
        try:
            parts = set(path.relative_to(root).parts)
        except ValueError:
            parts = set(path.parts)
        if parts & skipped_dirs:
            continue
        directories.append(path)
    return directories


def _git_public_paths(root: Path) -> list[Path] | None:
    """Return tracked and publishable untracked paths, excluding gitignored files."""
    if not (root / ".git").exists():
        return None
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return [
        root / value.decode("utf-8", errors="surrogateescape")
        for value in completed.stdout.split(b"\0")
        if value
    ]


def _relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _is_text_file(path: Path) -> bool:
    if path.suffix.lower() in {".pyc", ".pyo", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".whl", ".zip"}:
        return False
    return True


def _scan_text_file(root: Path, path: Path, patterns: tuple[tuple[str, str], ...]) -> list[AuditIssue]:
    issues: list[AuditIssue] = []
    if not _is_text_file(path):
        return issues
    if path.name.lower() == ".gitignore":
        return issues
    if _is_audit_rule_or_fixture_file(root, path):
        return issues
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return [AuditIssue("unreadable_text_file", _relative(root, path), 0, str(exc))]
    for line_no, line in enumerate(text.splitlines(), 1):
        for code, pattern in patterns:
            if code in {"windows_user_path", "local_drive_path"} and _looks_like_path_regex_example(path, line):
                continue
            if re.search(pattern, line):
                issues.append(AuditIssue(code, _relative(root, path), line_no, line.strip()[:500]))
    return issues


def _looks_like_path_regex_example(path: Path, line: str) -> bool:
    lower = line.lower()
    if "\\\\" not in line:
        return False
    if path.name.lower() in {"audit_skill.ps1", "public_audit.py"}:
        return True
    markers = ("regex", "pattern", "select-string", "-match", "sensitive_patterns", "path_patterns")
    return any(marker in lower for marker in markers)


def _is_audit_rule_or_fixture_file(root: Path, path: Path) -> bool:
    rel = _relative(root, path).replace("\\", "/")
    return (
        rel.endswith("instsci/public_audit.py")
        or rel.endswith("instsci/tests/test_public_audit.py")
        or rel.endswith("instsci/tests/project_guards.py")
    )


def audit_public_package(path: str | Path, *, include_institution_scan: bool = True) -> dict[str, Any]:
    """Audit a package directory for public-review hygiene problems."""
    root = Path(path).expanduser().resolve()
    issues: list[AuditIssue] = []
    if not root.exists():
        issues.append(AuditIssue("package_path_missing", str(root), 0, "Package path does not exist."))
        return _audit_payload(root, [], issues)
    if not root.is_dir():
        issues.append(AuditIssue("package_path_not_directory", str(root), 0, "Package path must be a directory."))
        return _audit_payload(root, [], issues)

    files = _iter_files(root)
    for file_path in files:
        rel = _relative(root, file_path)
        suffix = file_path.suffix.lower()
        name = file_path.name.lower()
        if suffix in DANGEROUS_BINARY_SUFFIXES and rel not in PUBLIC_BINARY_ALLOWLIST:
            issues.append(
                AuditIssue(
                    DANGEROUS_BINARY_SUFFIXES[suffix],
                    rel,
                    0,
                    "Public package contains a dangerous binary or browser evidence asset.",
                )
            )
        if "__pycache__" in file_path.parts:
            issues.append(AuditIssue("python_cache_dir", rel, 0, "Package contains __pycache__."))
        if suffix in {".pyc", ".pyo"}:
            issues.append(AuditIssue("python_bytecode", rel, 0, "Package contains compiled Python bytecode."))
        if suffix == ".pdf":
            issues.append(AuditIssue("pdf_output_included", rel, 0, "Public package should omit downloaded PDF outputs."))
        if suffix in {".zip", ".whl", ".tar", ".gz"}:
            issues.append(AuditIssue("binary_package_included", rel, 0, "Public repository should not include generated packages."))
        if suffix in {".db", ".sqlite", ".sqlite3"}:
            issues.append(AuditIssue("local_database_included", rel, 0, "Public package should omit local databases."))
        if suffix == ".log":
            issues.append(AuditIssue("local_log_included", rel, 0, "Public package should omit local logs."))
        if (
            name in {".env", "cookies.json", "storage_state.json", "local state"}
            or name.endswith(".env")
            or name.startswith("cookies.")
            or name.startswith("storage_state.")
        ):
            issues.append(AuditIssue("local_secret_or_session_file", rel, 0, "Public package should omit env, cookie, and session files."))

    forbidden_dirs = {
        "source_patched/tests": "root_historical_tests_included",
        "source_patched/build": "build_tree_included",
        "source_patched/runs": "run_outputs_included",
        "runs": "run_outputs_included",
        "downloads": "download_outputs_included",
        "build": "build_tree_included",
        "dist": "build_tree_included",
        "instsci/_browsers": "browser_runtime_included",
        "carsi_cookies": "cookie_store_included",
        "chrome-profile": "browser_profile_included",
        "browser-profile": "browser_profile_included",
        "cnki-profile": "browser_profile_included",
        "worker-profiles": "browser_profile_included",
        ".cache": "local_cache_included",
        "private-evidence": "private_evidence_included",
        "private_evidence": "private_evidence_included",
        "instsci/data/private": "private_evidence_included",
    }
    for rel_dir, code in forbidden_dirs.items():
        if (root / rel_dir).exists():
            issues.append(AuditIssue(code, rel_dir, 0, "Public package should omit local runtime artifacts."))

    forbidden_dir_names = {
        "runs": "run_outputs_included",
        "downloads": "download_outputs_included",
        "carsi_cookies": "cookie_store_included",
        "chrome-profile": "browser_profile_included",
        "browser-profile": "browser_profile_included",
        "cnki-profile": "browser_profile_included",
        "worker-profiles": "browser_profile_included",
        ".cache": "local_cache_included",
        "private-evidence": "private_evidence_included",
        "private_evidence": "private_evidence_included",
    }
    already_reported = {(issue.code, issue.path) for issue in issues}
    for directory in _iter_dirs(root):
        code = forbidden_dir_names.get(directory.name.lower())
        if not code:
            continue
        rel = _relative(root, directory)
        if (code, rel) not in already_reported:
            issues.append(AuditIssue(code, rel, 0, "Public package should omit local runtime artifacts."))
            already_reported.add((code, rel))

    patterns = SENSITIVE_PATTERNS + (PUBLIC_INSTITUTION_PATTERNS if include_institution_scan else ())
    for file_path in files:
        if file_path.name.lower().endswith(".private.json"):
            issues.append(
                AuditIssue(
                    "private_evidence_included",
                    _relative(root, file_path),
                    0,
                    "Private evidence JSON must stay outside the public package.",
                )
            )
        issues.extend(_scan_text_file(root, file_path, patterns))

    return _audit_payload(root, files, issues)


def _audit_payload(root: Path, files: list[Path], issues: list[AuditIssue]) -> dict[str, Any]:
    issue_rows = [issue.to_json() for issue in issues]
    return {
        "schema": "instsci.public_audit.v1",
        "schema_version": 1,
        "path": str(root),
        "status": "pass" if not issues else "fail",
        "file_count": len(files),
        "issue_count": len(issues),
        "issues": issue_rows,
        "summary": _issue_counts(issue_rows),
    }


def _issue_counts(issues: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for issue in issues:
        code = str(issue.get("code") or "unknown")
        counts[code] = counts.get(code, 0) + 1
    return dict(sorted(counts.items()))


def runtime_dependency_report() -> dict[str, Any]:
    """Return lightweight runtime dependency availability without importing heavy modules."""
    modules = []
    missing = []
    for name in REQUIRED_RUNTIME_MODULES:
        available = importlib.util.find_spec(name) is not None
        modules.append({"module": name, "available": available})
        if not available:
            missing.append(name)
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "modules": modules,
        "missing": missing,
        "status": "pass" if not missing else "warn",
    }


def matrix_report() -> dict[str, Any]:
    """Validate the publisher matrix loader and return compact status counts."""
    try:
        from .publisher_matrix import load_publisher_matrix

        matrix = load_publisher_matrix()
    except Exception as exc:
        return {"status": "fail", "error": str(exc), "entries": 0, "status_counts": {}}
    counts: dict[str, int] = {}
    for entry in matrix.values():
        counts[entry.status] = counts.get(entry.status, 0) + 1
    return {"status": "pass", "entries": len(matrix), "status_counts": dict(sorted(counts.items()))}


def browser_doctor_support_report() -> dict[str, Any]:
    system = platform.system().lower()
    return {
        "status": "pass" if system == "windows" else "warn",
        "platform": system,
        "supported": system == "windows",
        "note": "browser-doctor screenshot inspection is implemented for Windows visible desktops.",
    }


def zotero_handoff_smoke_report() -> dict[str, Any]:
    try:
        from .zotero_mcp import doi_to_url

        ok = doi_to_url("10.0000/example") == "https://doi.org/10.0000/example"
    except Exception as exc:
        return {"status": "fail", "error": str(exc)}
    return {"status": "pass" if ok else "fail"}


def doctor_report(*, package_path: str | Path | None = None, full: bool = False) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    checks.append({"name": "runtime_dependencies", **runtime_dependency_report()})
    checks.append({"name": "browser_doctor_support", **browser_doctor_support_report()})
    checks.append({"name": "publisher_matrix", **matrix_report()})
    checks.append({"name": "zotero_handoff", **zotero_handoff_smoke_report()})
    if full and package_path:
        audit = audit_public_package(package_path)
        checks.append(
            {
                "name": "public_package_audit",
                "status": audit["status"],
                "issue_count": audit["issue_count"],
                "summary": audit["summary"],
                "path": audit["path"],
            }
        )

    failing = [item for item in checks if item.get("status") == "fail"]
    warnings = [item for item in checks if item.get("status") == "warn"]
    return {
        "schema": "instsci.doctor.v1",
        "schema_version": 1,
        "status": "fail" if failing else ("warn" if warnings else "pass"),
        "checks": checks,
    }


def write_json_report(payload: dict[str, Any], output: str | Path) -> Path:
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path
