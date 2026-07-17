"""Persistent local audit ledger and safety policy for Chinese portal attempts."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path


DEFAULT_DAILY_WARNING_THRESHOLD = 100
QUOTA_LEDGER_SCHEMA = "instsci.chinese_download_quota.v1"
SUPPORTED_PORTALS = {"cnki", "wanfang"}
_LOCK_PID_PATTERN = re.compile(r"^pid=(\d+)\s*$")


class ChineseDownloadQuotaError(RuntimeError):
    """Raised when quota state cannot be trusted or updated safely."""


def _validate_optional_limit(value: int | None, name: str) -> None:
    if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 1):
        raise ValueError(f"{name} must be a positive integer or None")


@dataclass(frozen=True)
class ChineseDownloadPolicy:
    """Resolved local policy for one Chinese literature portal command."""

    warning_threshold: int | None = DEFAULT_DAILY_WARNING_THRESHOLD
    combined_daily_limit: int | None = None
    portal_daily_limit: int | None = None

    def __post_init__(self) -> None:
        _validate_optional_limit(self.warning_threshold, "warning_threshold")
        _validate_optional_limit(self.combined_daily_limit, "combined_daily_limit")
        _validate_optional_limit(self.portal_daily_limit, "portal_daily_limit")


@dataclass(frozen=True)
class QuotaReservation:
    allowed: bool
    date: str
    limit: int | None
    used: int
    remaining: int | None
    portal: str
    record_id: str
    portal_limit: int | None = None
    portal_used: int = 0
    portal_remaining: int | None = None
    warning_threshold: int | None = DEFAULT_DAILY_WARNING_THRESHOLD
    warning_reached: bool = False
    warning_triggered: bool = False
    limit_scope: str = ""
    reason: str = ""

    def to_json(self) -> dict[str, object]:
        return asdict(self)


def _load_ledger(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"schema": QUOTA_LEDGER_SCHEMA, "days": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ChineseDownloadQuotaError(f"invalid quota ledger: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ChineseDownloadQuotaError(f"invalid quota ledger: {path}: root must be an object")
    if payload.get("schema") != QUOTA_LEDGER_SCHEMA:
        raise ChineseDownloadQuotaError(f"unsupported quota ledger schema: {payload.get('schema')!r}")
    days = payload.get("days")
    if not isinstance(days, dict):
        raise ChineseDownloadQuotaError(f"invalid quota ledger: {path}: days must be an object")
    for day, reservations in days.items():
        if not isinstance(day, str) or not isinstance(reservations, list):
            raise ChineseDownloadQuotaError(f"invalid quota ledger: {path}: invalid daily reservations")
        if any(not isinstance(reservation, dict) for reservation in reservations):
            raise ChineseDownloadQuotaError(f"invalid quota ledger: {path}: reservation must be an object")
    return payload


def _write_ledger(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise ChineseDownloadQuotaError(f"could not write quota ledger: {path}: {exc}") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _acquire_lock(lock_path: Path, timeout: float) -> None:
    deadline = time.monotonic() + max(float(timeout), 0.0)
    while True:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise ChineseDownloadQuotaError(f"quota ledger is locked: {lock_path}")
            time.sleep(0.01)
            continue
        except OSError as exc:
            raise ChineseDownloadQuotaError(f"could not lock quota ledger: {lock_path}: {exc}") from exc
        try:
            os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
        finally:
            os.close(descriptor)
        return


def _quota_lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


def _read_lock_pid(lock_path: Path) -> int:
    try:
        content = lock_path.read_text(encoding="ascii")
    except OSError as exc:
        raise ChineseDownloadQuotaError(f"could not read quota lock: {lock_path}: {exc}") from exc
    match = _LOCK_PID_PATTERN.fullmatch(content)
    if not match or int(match.group(1)) < 1:
        raise ChineseDownloadQuotaError(f"invalid quota lock: {lock_path}")
    return int(match.group(1))


def _pid_is_running(pid: int) -> bool:
    if pid < 1:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def inspect_chinese_download_quota(
    ledger_path: str | Path,
    *,
    now: datetime | None = None,
    warning_threshold: int | None = DEFAULT_DAILY_WARNING_THRESHOLD,
    combined_limit: int | None = None,
    cnki_limit: int | None = None,
    wanfang_limit: int | None = None,
) -> dict[str, object]:
    """Return local quota and lock health without changing either file."""
    _validate_optional_limit(warning_threshold, "warning_threshold")
    _validate_optional_limit(combined_limit, "combined_limit")
    _validate_optional_limit(cnki_limit, "cnki_limit")
    _validate_optional_limit(wanfang_limit, "wanfang_limit")
    path = Path(ledger_path).expanduser()
    lock_path = _quota_lock_path(path)
    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.astimezone()
    date_key = current.date().isoformat()
    ledger_valid = True
    ledger_error = ""
    try:
        payload = _load_ledger(path)
        days = payload["days"]
        assert isinstance(days, dict)
        reservations = days.get(date_key, [])
        if not isinstance(reservations, list):
            raise ChineseDownloadQuotaError(f"invalid quota ledger: {path}: daily reservations must be a list")
        combined_used = len(reservations)
        cnki_used = sum(str(item.get("portal") or "").lower() == "cnki" for item in reservations)
        wanfang_used = sum(str(item.get("portal") or "").lower() == "wanfang" for item in reservations)
    except ChineseDownloadQuotaError as exc:
        ledger_valid = False
        ledger_error = str(exc)
        combined_used = 0
        cnki_used = 0
        wanfang_used = 0
    lock_exists = lock_path.exists()
    lock_pid: int | None = None
    lock_pid_running: bool | None = None
    lock_error = ""
    if lock_exists:
        try:
            lock_pid = _read_lock_pid(lock_path)
            lock_pid_running = _pid_is_running(lock_pid)
        except ChineseDownloadQuotaError as exc:
            lock_error = str(exc)
    stale_lock = bool(lock_exists and lock_pid is not None and lock_pid_running is False)
    return {
        "ledger_path": str(path),
        "ledger_exists": path.exists(),
        "ledger_valid": ledger_valid,
        "ledger_error": ledger_error,
        "date": date_key,
        "warning_threshold": warning_threshold,
        "warning_reached": bool(
            ledger_valid and warning_threshold is not None and combined_used >= warning_threshold
        ),
        "limit": combined_limit,
        "used": combined_used,
        "remaining": (
            max(combined_limit - combined_used, 0)
            if ledger_valid and combined_limit is not None
            else None
        ),
        "combined_limit": combined_limit,
        "combined_used": combined_used,
        "combined_remaining": (
            max(combined_limit - combined_used, 0)
            if ledger_valid and combined_limit is not None
            else None
        ),
        "cnki_limit": cnki_limit,
        "cnki_used": cnki_used,
        "cnki_remaining": (
            max(cnki_limit - cnki_used, 0)
            if ledger_valid and cnki_limit is not None
            else None
        ),
        "wanfang_limit": wanfang_limit,
        "wanfang_used": wanfang_used,
        "wanfang_remaining": (
            max(wanfang_limit - wanfang_used, 0)
            if ledger_valid and wanfang_limit is not None
            else None
        ),
        "lock_path": str(lock_path),
        "lock_exists": lock_exists,
        "lock_pid": lock_pid,
        "lock_pid_running": lock_pid_running,
        "lock_error": lock_error,
        "stale_lock": stale_lock,
        "repairable": stale_lock and not lock_error,
    }


def repair_chinese_download_quota_lock(ledger_path: str | Path) -> dict[str, object]:
    """Remove a lock only after its recorded PID is proven inactive."""
    path = Path(ledger_path).expanduser()
    lock_path = _quota_lock_path(path)
    if not lock_path.exists():
        return {"removed": False, "reason": "no_lock", "lock_path": str(lock_path)}
    try:
        before = lock_path.read_bytes()
    except OSError as exc:
        raise ChineseDownloadQuotaError(f"could not read quota lock: {lock_path}: {exc}") from exc
    pid = _read_lock_pid(lock_path)
    if _pid_is_running(pid):
        raise ChineseDownloadQuotaError(f"quota lock belongs to an active process: pid={pid}")
    try:
        if lock_path.read_bytes() != before:
            raise ChineseDownloadQuotaError(f"quota lock changed during repair: {lock_path}")
        if _pid_is_running(pid):
            raise ChineseDownloadQuotaError(f"quota lock belongs to an active process: pid={pid}")
        lock_path.unlink()
    except ChineseDownloadQuotaError:
        raise
    except OSError as exc:
        raise ChineseDownloadQuotaError(f"could not remove stale quota lock: {lock_path}: {exc}") from exc
    return {"removed": True, "reason": "stale_lock_removed", "lock_path": str(lock_path), "pid": pid}


def reserve_chinese_download(
    ledger_path: str | Path,
    *,
    portal: str,
    record_id: str,
    now: datetime | None = None,
    policy: ChineseDownloadPolicy | None = None,
    lock_timeout: float = 5.0,
) -> QuotaReservation:
    """Atomically reserve and audit one CNKI or Wanfang download attempt."""
    normalized_portal = str(portal or "").strip().lower()
    if normalized_portal not in SUPPORTED_PORTALS:
        raise ValueError("portal must be cnki or wanfang")
    effective_policy = policy or ChineseDownloadPolicy()
    if not isinstance(effective_policy, ChineseDownloadPolicy):
        raise TypeError("policy must be a ChineseDownloadPolicy")
    path = Path(ledger_path).expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ChineseDownloadQuotaError(f"could not prepare quota directory: {path.parent}: {exc}") from exc
    lock_path = _quota_lock_path(path)
    _acquire_lock(lock_path, lock_timeout)
    try:
        current = now or datetime.now().astimezone()
        if current.tzinfo is None:
            current = current.astimezone()
        date_key = current.date().isoformat()
        payload = _load_ledger(path)
        days = payload["days"]
        assert isinstance(days, dict)
        reservations = days.setdefault(date_key, [])
        if not isinstance(reservations, list):
            raise ChineseDownloadQuotaError(f"invalid quota ledger: {path}: daily reservations must be a list")
        used = len(reservations)
        portal_used = sum(
            str(item.get("portal") or "").lower() == normalized_portal for item in reservations
        )
        if effective_policy.combined_daily_limit is not None and used >= effective_policy.combined_daily_limit:
            return QuotaReservation(
                allowed=False,
                date=date_key,
                limit=effective_policy.combined_daily_limit,
                used=used,
                remaining=0,
                portal=normalized_portal,
                record_id=str(record_id or ""),
                portal_limit=effective_policy.portal_daily_limit,
                portal_used=portal_used,
                portal_remaining=(
                    max(effective_policy.portal_daily_limit - portal_used, 0)
                    if effective_policy.portal_daily_limit is not None
                    else None
                ),
                warning_threshold=effective_policy.warning_threshold,
                warning_reached=bool(
                    effective_policy.warning_threshold is not None
                    and used >= effective_policy.warning_threshold
                ),
                limit_scope="combined",
                reason="daily_limit_reached",
            )
        if effective_policy.portal_daily_limit is not None and portal_used >= effective_policy.portal_daily_limit:
            return QuotaReservation(
                allowed=False,
                date=date_key,
                limit=effective_policy.combined_daily_limit,
                used=used,
                remaining=(
                    max(effective_policy.combined_daily_limit - used, 0)
                    if effective_policy.combined_daily_limit is not None
                    else None
                ),
                portal=normalized_portal,
                record_id=str(record_id or ""),
                portal_limit=effective_policy.portal_daily_limit,
                portal_used=portal_used,
                portal_remaining=0,
                warning_threshold=effective_policy.warning_threshold,
                warning_reached=bool(
                    effective_policy.warning_threshold is not None
                    and used >= effective_policy.warning_threshold
                ),
                limit_scope="portal",
                reason="daily_limit_reached",
            )
        reservations.append(
            {
                "attempted_at": current.isoformat(timespec="seconds"),
                "portal": normalized_portal,
                "record_id": str(record_id or ""),
            }
        )
        _write_ledger(path, payload)
        used += 1
        portal_used += 1
        warning_reached = bool(
            effective_policy.warning_threshold is not None
            and used >= effective_policy.warning_threshold
        )
        return QuotaReservation(
            allowed=True,
            date=date_key,
            limit=effective_policy.combined_daily_limit,
            used=used,
            remaining=(
                effective_policy.combined_daily_limit - used
                if effective_policy.combined_daily_limit is not None
                else None
            ),
            portal=normalized_portal,
            record_id=str(record_id or ""),
            portal_limit=effective_policy.portal_daily_limit,
            portal_used=portal_used,
            portal_remaining=(
                effective_policy.portal_daily_limit - portal_used
                if effective_policy.portal_daily_limit is not None
                else None
            ),
            warning_threshold=effective_policy.warning_threshold,
            warning_reached=warning_reached,
            warning_triggered=bool(
                warning_reached and used == effective_policy.warning_threshold
            ),
        )
    finally:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass
