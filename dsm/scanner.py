"""파일 스캔과 삭제 실행.

설계 원칙
- 후보 파일을 리스트에 모두 담지 않는다. 검사 로그 폴더는 파일이 수십만 개까지
  가므로, 후보는 제너레이터로 흘려보내며 통계만 누적한다.
- 심볼릭 링크와 정션(reparse point)은 따라가지 않는다. 감시 폴더 밖으로
  빠져나가 엉뚱한 곳을 지우는 최악의 사고를 막는 장치다.
- 모든 예외는 개별 파일 단위로 흡수한다. 파일 하나가 잠겨 있다고 전체 정리가
  멈추면 안 된다.
"""

from __future__ import annotations

import os
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import Callable, Iterator

from .config import FolderProfile
from .paths import long_path

FILE_ATTRIBUTE_REPARSE_POINT = 0x400

MAX_DRY_RUN_AUDIT_ROWS = 5000
"""모의실행 감사로그 상세 행 상한. 초과분은 요약만 남긴다."""


# --- 결과 모델 -------------------------------------------------------------


@dataclass
class Candidate:
    """삭제 대상으로 판정된 파일 하나."""

    path: str
    size: int
    ref_time: float
    age_days: float
    basis: str  # 실제 적용된 날짜 기준 (폴더명 파싱 실패 시 mtime으로 대체됨)


@dataclass
class ScanStats:
    scanned_dirs: int = 0
    scanned_files: int = 0
    matched: int = 0
    matched_bytes: int = 0
    excluded: int = 0
    unreadable: int = 0
    foldername_fallback: int = 0
    errors: list[str] = field(default_factory=list)

    def note_error(self, message: str) -> None:
        # 같은 원인이 수천 번 반복될 수 있으므로 앞쪽 일부만 보관한다
        if len(self.errors) < 50:
            self.errors.append(message)


@dataclass
class PreviewResult:
    """모의실행(미리보기) 집계."""

    profile_name: str
    path: str
    stats: ScanStats
    samples: list[Candidate] = field(default_factory=list)
    oldest: Candidate | None = None
    newest: Candidate | None = None
    elapsed: float = 0.0
    stopped: bool = False

    @property
    def count(self) -> int:
        return self.stats.matched

    @property
    def total_bytes(self) -> int:
        return self.stats.matched_bytes


@dataclass
class ExecResult:
    """실제 정리 결과."""

    profile_name: str
    path: str
    dry_run: bool
    deleted: int = 0
    freed_bytes: int = 0
    failed: int = 0
    removed_dirs: int = 0
    stats: ScanStats = field(default_factory=ScanStats)
    elapsed: float = 0.0
    stopped: bool = False
    emergency: bool = False
    """긴급(용량 부족) 정리로 실행됐는지."""
    goal_reached: bool = False
    """목표 여유 공간을 확보해서 중단했는지."""


# --- 내부 헬퍼 -------------------------------------------------------------


def _matches_any(name: str, patterns: list[str]) -> bool:
    lowered = name.lower()
    return any(fnmatch(lowered, p.lower()) for p in patterns)


def _is_reparse_point(entry: os.DirEntry) -> bool:
    """정션/심볼릭 링크 여부. 감시 폴더 밖으로 나가지 않기 위한 핵심 검사."""
    try:
        attrs = entry.stat(follow_symlinks=False).st_file_attributes
    except (OSError, AttributeError):
        try:
            return entry.is_symlink()
        except OSError:
            return True  # 판단 불가하면 건드리지 않는다
    return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)


def _parse_folder_date(name: str, fmt: str) -> float | None:
    """폴더명에서 날짜를 읽는다. 형식이 맞지 않으면 None."""
    try:
        return datetime.strptime(name, fmt).timestamp()
    except (ValueError, OverflowError):
        return None


def _file_ref_time(entry: os.DirEntry, profile: FolderProfile, folder_date: float | None) -> tuple[float, str]:
    """파일의 기준 시각과 실제 사용된 기준을 돌려준다.

    폴더명 기준인데 상위 폴더에서 날짜를 못 찾으면 수정 시각으로 대체한다.
    파일을 복사/이동하면 수정 시각이 바뀌므로, 날짜별 폴더 구조에서는
    폴더명 기준이 가장 안전하다.
    """
    if profile.date_basis == "foldername":
        if folder_date is not None:
            return folder_date, "foldername"
        stats = entry.stat(follow_symlinks=False)
        return stats.st_mtime, "mtime(대체)"

    stats = entry.stat(follow_symlinks=False)
    if profile.date_basis == "ctime":
        return stats.st_ctime, "ctime"
    return stats.st_mtime, "mtime"


# --- 스캔 ------------------------------------------------------------------


def iter_candidates(
    profile: FolderProfile,
    stats: ScanStats,
    now: float | None = None,
    should_stop: Callable[[], bool] | None = None,
    retention_days: int | None = None,
) -> Iterator[Candidate]:
    """보관 기간이 지난 파일을 하나씩 내보낸다.

    retention_days 를 주면 프로파일 설정 대신 그 값을 쓴다.
    용량이 부족할 때 긴급 하한까지 범위를 넓히는 용도다.
    """
    now = now if now is not None else time.time()
    days = profile.retention_days if retention_days is None else retention_days
    cutoff = now - days * 86400.0
    root = Path(profile.path).resolve()
    stop = should_stop or (lambda: False)

    # (디렉터리, 상위에서 물려받은 폴더 날짜)
    pending: list[tuple[str, float | None]] = [(str(root), None)]

    while pending:
        if stop():
            return
        current, inherited_date = pending.pop()
        stats.scanned_dirs += 1

        try:
            with os.scandir(long_path(current)) as entries:
                for entry in entries:
                    if stop():
                        return
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if not profile.include_subfolders:
                                continue
                            if _is_reparse_point(entry):
                                continue  # 링크는 따라가지 않는다
                            if _matches_any(entry.name, profile.exclude_patterns):
                                stats.excluded += 1
                                continue  # 제외 폴더는 하위까지 통째로 건너뛴다
                            folder_date = inherited_date
                            if profile.date_basis == "foldername":
                                parsed = _parse_folder_date(entry.name, profile.folder_date_format)
                                if parsed is not None:
                                    folder_date = parsed
                            pending.append((entry.path, folder_date))
                            continue

                        if not entry.is_file(follow_symlinks=False):
                            continue
                        if _is_reparse_point(entry):
                            continue

                        stats.scanned_files += 1

                        if not _matches_any(entry.name, profile.include_patterns):
                            continue
                        if profile.exclude_patterns and _matches_any(entry.name, profile.exclude_patterns):
                            stats.excluded += 1
                            continue

                        ref_time, basis = _file_ref_time(entry, profile, inherited_date)
                        if ref_time >= cutoff:
                            continue

                        size = entry.stat(follow_symlinks=False).st_size
                        if basis.endswith("(대체)"):
                            stats.foldername_fallback += 1

                        stats.matched += 1
                        stats.matched_bytes += size
                        yield Candidate(
                            path=entry.path,
                            size=size,
                            ref_time=ref_time,
                            age_days=(now - ref_time) / 86400.0,
                            basis=basis,
                        )
                    except OSError as exc:
                        stats.unreadable += 1
                        stats.note_error(f"{entry.path}: {exc}")
        except OSError as exc:
            stats.unreadable += 1
            stats.note_error(f"{current}: {exc}")


def preview(
    profile: FolderProfile,
    now: float | None = None,
    should_stop: Callable[[], bool] | None = None,
    sample_limit: int = 50,
    retention_days: int | None = None,
) -> PreviewResult:
    """삭제하지 않고 대상만 집계한다. 도입 검증의 핵심 기능."""
    started = time.monotonic()
    stats = ScanStats()
    result = PreviewResult(profile_name=profile.name, path=profile.path, stats=stats)

    for candidate in iter_candidates(profile, stats, now=now, should_stop=should_stop,
                                     retention_days=retention_days):
        if len(result.samples) < sample_limit:
            result.samples.append(candidate)
        if result.oldest is None or candidate.ref_time < result.oldest.ref_time:
            result.oldest = candidate
        if result.newest is None or candidate.ref_time > result.newest.ref_time:
            result.newest = candidate

    result.elapsed = time.monotonic() - started
    result.stopped = bool(should_stop and should_stop())
    return result


# --- 삭제 ------------------------------------------------------------------


def _delete_file(path: str, action: str) -> None:
    target = long_path(path)
    if action == "recycle":
        from send2trash import send2trash

        send2trash(path)  # 휴지통 API는 확장 경로를 받지 않는다
        return
    try:
        os.remove(target)
    except PermissionError:
        # 읽기 전용 속성 때문이면 한 번만 풀고 재시도한다
        os.chmod(target, stat.S_IWRITE)
        os.remove(target)


def _remove_empty_dirs(root: Path, should_stop: Callable[[], bool]) -> int:
    """하위부터 올라오며 빈 폴더를 정리한다. 루트 자체는 건드리지 않는다.

    os.walk가 넘겨주는 dirnames는 순회 시작 시점의 목록이라 방금 지운 하위
    폴더가 그대로 남아 있다. 그래서 목록을 보고 판단하지 않고 rmdir을 그냥
    시도한다. rmdir은 비어 있지 않은 폴더에서는 반드시 실패하므로,
    이 방식이 오히려 안전하면서 날짜 폴더까지 한 번에 정리된다.
    """
    removed = 0
    for current, _dirnames, _filenames in os.walk(str(root), topdown=False):
        if should_stop():
            break
        if Path(current) == root:
            continue
        try:
            os.rmdir(long_path(current))
            removed += 1
        except OSError:
            continue  # 내용이 남아 있으면 그대로 둔다
    return removed


GOAL_CHECK_INTERVAL = 200
"""긴급 삭제에서 목표 달성 여부를 몇 개마다 확인할지. 매번 디스크를 조회하면 느리다."""


def execute(
    profile: FolderProfile,
    dry_run: bool,
    on_file: Callable[[Candidate, bool, str], None] | None = None,
    now: float | None = None,
    should_stop: Callable[[], bool] | None = None,
    retention_days: int | None = None,
    goal_reached: Callable[[], bool] | None = None,
) -> ExecResult:
    """정리를 실행한다.

    on_file(candidate, ok, note) 콜백으로 감사로그를 남긴다.
    dry_run이면 파일을 건드리지 않고 판정 결과만 통보한다.
    retention_days 로 긴급 하한을 적용할 수 있고, goal_reached 가 True 를
    돌려주면 더 지우지 않고 중단한다 (목표 여유 공간을 확보한 경우).
    """
    started = time.monotonic()
    stop = should_stop or (lambda: False)
    stats = ScanStats()
    result = ExecResult(
        profile_name=profile.name,
        path=profile.path,
        dry_run=dry_run,
        stats=stats,
    )

    processed = 0
    for candidate in iter_candidates(profile, stats, now=now, should_stop=stop,
                                     retention_days=retention_days):
        # 목표를 이미 채웠으면 더 지우지 않는다. 꼭 필요한 만큼만 지우기 위한 장치.
        if goal_reached is not None and processed % GOAL_CHECK_INTERVAL == 0 and goal_reached():
            result.goal_reached = True
            break
        processed += 1

        if dry_run:
            result.deleted += 1
            result.freed_bytes += candidate.size
            if on_file is not None and result.deleted <= MAX_DRY_RUN_AUDIT_ROWS:
                on_file(candidate, True, "")
            continue

        try:
            _delete_file(candidate.path, profile.action)
        except OSError as exc:
            result.failed += 1
            stats.note_error(f"{candidate.path}: {exc}")
            if on_file is not None:
                on_file(candidate, False, str(exc))
            continue

        result.deleted += 1
        result.freed_bytes += candidate.size
        if on_file is not None:
            on_file(candidate, True, "")

    if goal_reached is not None and not result.goal_reached and goal_reached():
        result.goal_reached = True

    if profile.remove_empty_dirs and not dry_run and not stop():
        try:
            result.removed_dirs = _remove_empty_dirs(Path(profile.path).resolve(), stop)
        except OSError as exc:
            stats.note_error(f"빈 폴더 정리 실패: {exc}")

    result.elapsed = time.monotonic() - started
    result.stopped = stop()
    return result


def human_bytes(size: float) -> str:
    """사람이 읽는 용량 표기."""
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    value = float(size)
    for unit in units:
        if abs(value) < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} PB"
