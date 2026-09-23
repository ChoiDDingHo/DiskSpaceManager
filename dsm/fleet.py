"""여러 검사기의 상태를 공유 폴더로 모으는 기능.

서버나 데이터베이스를 두지 않는다. 각 PC가 공유 폴더에 자기 상태를 작은 JSON
한 개로 써 두고, 보고 싶은 사람이 아무 PC에서나 그 폴더를 읽어 모아 보는 방식이다.
설치할 것이 늘지 않고, 공유 폴더가 잠시 끊겨도 각 PC의 정리 작업은 그대로 돈다.

공유 경로가 비어 있으면 아무 동작도 하지 않는다.
"""

from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import APP_VERSION
from .audit import log

STATUS_FOLDER = "dsm-status"
STALE_SECONDS = 3 * 3600
"""이 시간 넘게 보고가 없으면 '연락 끊김'으로 본다."""

_hostname: str | None = None


def hostname() -> str:
    global _hostname
    if _hostname is None:
        try:
            _hostname = socket.gethostname() or "UNKNOWN"
        except OSError:
            _hostname = "UNKNOWN"
    return _hostname


def _safe_name(text: str) -> str:
    """파일 이름으로 쓸 수 있게 다듬는다."""
    keep = [c if c.isalnum() or c in "-_" else "_" for c in text]
    return "".join(keep)[:60] or "UNKNOWN"


def status_dir(share_path: str) -> Path | None:
    if not (share_path or "").strip():
        return None
    return Path(share_path.strip()) / STATUS_FOLDER


@dataclass
class FleetEntry:
    host: str
    reported_at: float = 0.0
    state: str = ""
    level: str = "ok"
    dry_run: bool = True
    schedule: str = ""
    last_cycle: str = ""
    version: str = ""
    drives: list[dict] = field(default_factory=list)
    error: str = ""

    @property
    def stale(self) -> bool:
        return (time.time() - self.reported_at) > STALE_SECONDS

    @property
    def worst_free_percent(self) -> float | None:
        values = [d.get("free_percent") for d in self.drives if d.get("free_percent") is not None]
        return min(values) if values else None


def publish(share_path: str, payload: dict) -> bool:
    """이 PC의 상태를 공유 폴더에 남긴다. 실패해도 조용히 넘어간다."""
    folder = status_dir(share_path)
    if folder is None:
        return False

    try:
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{_safe_name(hostname())}.json"
        # 읽는 쪽이 반쪽짜리 파일을 보지 않도록 임시 파일 후 교체
        tmp = folder / f".{_safe_name(hostname())}.tmp"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, target)
        return True
    except OSError as exc:
        # 공유 폴더가 끊겨도 정리 작업은 계속되어야 한다
        log.debug("공유 폴더에 상태를 남기지 못했습니다: %s", exc)
        return False


def collect(share_path: str) -> tuple[list[FleetEntry], str]:
    """공유 폴더의 모든 PC 상태를 읽는다. (목록, 오류메시지)"""
    folder = status_dir(share_path)
    if folder is None:
        return [], "공유 폴더가 설정되지 않았습니다."

    try:
        files = sorted(folder.glob("*.json"))
    except OSError as exc:
        return [], f"공유 폴더를 읽을 수 없습니다: {exc}"

    if not files:
        return [], f"{folder} 에 보고된 상태가 없습니다."

    entries: list[FleetEntry] = []
    for path in files:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            entries.append(FleetEntry(host=path.stem, error="파일을 읽지 못했습니다"))
            continue
        if not isinstance(raw, dict):
            entries.append(FleetEntry(host=path.stem, error="형식이 올바르지 않습니다"))
            continue

        entries.append(FleetEntry(
            host=str(raw.get("host", path.stem)),
            reported_at=float(raw.get("reported_at", 0.0) or 0.0),
            state=str(raw.get("state", "")),
            level=str(raw.get("level", "ok")),
            dry_run=bool(raw.get("dry_run", True)),
            schedule=str(raw.get("schedule", "")),
            last_cycle=str(raw.get("last_cycle", "")),
            version=str(raw.get("version", "")),
            drives=list(raw.get("drives", []) or []),
        ))

    # 여유가 적은 PC를 위로. 연락이 끊긴 PC는 맨 아래로 내린다.
    def sort_key(entry: FleetEntry):
        worst = entry.worst_free_percent
        return (entry.stale, worst if worst is not None else 999.0)

    entries.sort(key=sort_key)
    return entries, ""


def build_payload(status, settings) -> dict:
    """EngineStatus 를 공유용 JSON 으로 만든다."""
    cycle = status.last_cycle
    return {
        "host": hostname(),
        "reported_at": time.time(),
        "version": APP_VERSION,
        "state": status.state,
        "level": status.level,
        "dry_run": bool(settings.global_dry_run),
        "schedule": settings.schedule_text(),
        "last_cycle": cycle.describe() if cycle is not None else "",
        "last_cycle_at": cycle.finished_at if cycle is not None else 0.0,
        "drives": [
            {
                "drive": disk.drive,
                "total": disk.total,
                "free": disk.free,
                "free_percent": round(disk.free_percent, 2),
                "level": disk.level,
            }
            for disk in status.disks if disk.ok
        ],
    }
