"""Persistent local quota for CNKI and Wanfang download attempts."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path


DAILY_DOWNLOAD_LIMIT = 100
QUOTA_LEDGER_SCHEMA = "instsci.chinese_download_quota.v1"
SUPPORTED_PORTALS = {"cnki", "wanfang"}


class ChineseDownloadQuotaError(RuntimeError):
    """Raised when quota state cannot be trusted or updated safely."""


@dataclass(frozen=True)
class QuotaReservation:
    allowed: bool
    date: str
    limit: int
    used: int
    remaining: int
    portal: str
    record_id: str
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


def reserve_chinese_download(
    ledger_path: str | Path,
    *,
    portal: str,
    record_id: str,
    now: datetime | None = None,
    limit: int = DAILY_DOWNLOAD_LIMIT,
    lock_timeout: float = 5.0,
) -> QuotaReservation:
    """Atomically reserve one shared CNKI/Wanfang download attempt."""
    normalized_portal = str(portal or "").strip().lower()
    if normalized_portal not in SUPPORTED_PORTALS:
        raise ValueError("portal must be cnki or wanfang")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("limit must be a positive integer")
    path = Path(ledger_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
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
        if used >= limit:
            return QuotaReservation(
                allowed=False,
                date=date_key,
                limit=limit,
                used=used,
                remaining=0,
                portal=normalized_portal,
                record_id=str(record_id or ""),
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
        return QuotaReservation(
            allowed=True,
            date=date_key,
            limit=limit,
            used=used,
            remaining=limit - used,
            portal=normalized_portal,
            record_id=str(record_id or ""),
        )
    finally:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass
