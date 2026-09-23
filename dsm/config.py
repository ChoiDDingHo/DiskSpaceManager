"""설정 모델, JSON 입출력, 그리고 경로/보관기간 안전 검증.

이 모듈의 검증 규칙이 프로그램 전체의 안전선이다.
용량을 못 줄이는 것보다 지우면 안 될 것을 지우는 것이 훨씬 큰 사고이므로,
의심스러운 설정은 저장 단계에서 거부한다.
"""

from __future__ import annotations

import json
import os
import time as time_module
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .paths import app_root, config_path, data_dir

# --- 안전 상수 -------------------------------------------------------------

MIN_RETENTION_DAYS = 3
"""보관기간 하한. 0이나 1을 실수로 입력해 당일 데이터가 날아가는 사고를 막는다."""

MAX_RETENTION_DAYS = 3650

DATE_BASIS_CHOICES = {
    "mtime": "수정 시각",
    "ctime": "생성 시각",
    "foldername": "폴더명 날짜",
}

ACTION_CHOICES = {
    "delete": "영구 삭제",
    "recycle": "휴지통으로 이동",
}

_PROTECTED_TREE_ENV_KEYS = (
    "WINDIR",
    "SYSTEMROOT",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMDATA",
    "APPDATA",
    "LOCALAPPDATA",
)
"""이 폴더들은 하위 전체가 등록 금지 — 시스템/프로그램 상태가 들어 있다."""

_PROTECTED_EXACT_ENV_KEYS = ("USERPROFILE",)
"""이 폴더들은 자기 자신만 금지 — 하위 폴더는 로그 보관에 쓸 수 있다."""


def _resolve_all(paths: list[Path]) -> list[Path]:
    resolved: list[Path] = []
    for path in paths:
        try:
            resolved.append(path.resolve())
        except OSError:
            continue
    return resolved


def _protected_trees() -> list[Path]:
    """자신과 하위 전체가 등록 금지인 폴더들."""
    roots = [Path(os.environ[k]) for k in _PROTECTED_TREE_ENV_KEYS if os.environ.get(k)]
    roots.append(app_root())  # 프로그램 설치 폴더
    roots.append(data_dir())  # 자기 설정/로그 폴더
    return _resolve_all(roots)


def _protected_exact() -> list[Path]:
    """폴더 자체만 등록 금지인 폴더들."""
    roots = [Path(os.environ[k]) for k in _PROTECTED_EXACT_ENV_KEYS if os.environ.get(k)]
    return _resolve_all(roots)


def _is_same_or_inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_folder_path(raw: str) -> tuple[bool, str]:
    """관리 대상 폴더로 적합한지 검사. (가능여부, 사유)"""
    text = (raw or "").strip().strip(chr(34))
    if not text:
        return False, "경로를 입력하세요."

    path = Path(text)
    if not path.is_absolute():
        return False, "절대 경로를 입력하세요. (예: D:\\InspectionLog)"

    try:
        resolved = path.resolve()
    except OSError as exc:
        return False, f"경로를 확인할 수 없습니다: {exc}"

    if not resolved.exists():
        return False, "존재하지 않는 폴더입니다."
    if not resolved.is_dir():
        return False, "폴더가 아닙니다."

    # 드라이브 루트 직접 지정 금지 (D:\ 전체를 대상으로 두는 사고 방지)
    if resolved.parent == resolved:
        return False, "드라이브 루트는 지정할 수 없습니다. 하위 폴더를 선택하세요."

    for protected in _protected_exact():
        if resolved == protected:
            return False, f"보호된 폴더입니다: {protected}"

    for protected in _protected_trees():
        if _is_same_or_inside(resolved, protected):
            return False, f"보호된 시스템/프로그램 폴더입니다: {protected}"
        # 보호 폴더를 품고 있는 상위 경로도 막는다 (하위 전체를 훑게 되므로)
        if _is_same_or_inside(protected, resolved):
            return False, f"보호된 폴더({protected})를 포함하는 상위 경로입니다."

    return True, ""


def parse_hhmm(text: str) -> tuple[int, int] | None:
    """"HH:MM" 을 (시, 분) 으로. 형식이 틀리면 None."""
    try:
        hour_text, minute_text = str(text).strip().split(":")
        hour, minute = int(hour_text), int(minute_text)
    except (ValueError, AttributeError):
        return None
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


def in_time_window(now: time_module.struct_time | None, start: str, end: str) -> bool:
    """지금이 지정한 시간대 안인지. 22:00~05:00 처럼 자정을 넘는 구간도 다룬다."""
    begin = parse_hhmm(start)
    finish = parse_hhmm(end)
    if begin is None or finish is None:
        return True  # 설정이 깨졌으면 막지 않는다

    current = now or time_module.localtime()
    minutes = current.tm_hour * 60 + current.tm_min
    begin_minutes = begin[0] * 60 + begin[1]
    end_minutes = finish[0] * 60 + finish[1]

    if begin_minutes == end_minutes:
        return True  # 시작과 끝이 같으면 제한 없음으로 본다
    if begin_minutes < end_minutes:
        return begin_minutes <= minutes < end_minutes
    return minutes >= begin_minutes or minutes < end_minutes  # 자정을 넘는 구간


def validate_retention(days: Any) -> tuple[bool, str]:
    try:
        value = int(days)
    except (TypeError, ValueError):
        return False, "보관 기간은 숫자여야 합니다."
    if value < MIN_RETENTION_DAYS:
        return False, f"보관 기간은 최소 {MIN_RETENTION_DAYS}일 이상이어야 합니다."
    if value > MAX_RETENTION_DAYS:
        return False, f"보관 기간은 최대 {MAX_RETENTION_DAYS}일입니다."
    return True, ""


# --- 모델 ------------------------------------------------------------------


@dataclass
class FolderProfile:
    """관리 대상 폴더 하나와 그 정리 정책."""

    name: str = "새 프로파일"
    path: str = ""
    enabled: bool = True
    include_subfolders: bool = True
    include_patterns: list[str] = field(default_factory=lambda: ["*"])
    exclude_patterns: list[str] = field(default_factory=list)
    retention_days: int = 30
    emergency_retention_days: int = 7
    """용량이 부족할 때만 적용되는 하한. 평소에는 절대 여기까지 지우지 않는다."""
    date_basis: str = "mtime"
    folder_date_format: str = "%Y%m%d"
    action: str = "delete"
    remove_empty_dirs: bool = True
    dry_run: bool = True  # 새 프로파일은 항상 모의실행으로 시작한다

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.name.strip():
            errors.append("프로파일 이름을 입력하세요.")
        ok, reason = validate_folder_path(self.path)
        if not ok:
            errors.append(reason)
        ok, reason = validate_retention(self.retention_days)
        if not ok:
            errors.append(reason)

        ok, reason = validate_retention(self.emergency_retention_days)
        if not ok:
            errors.append(f"긴급 보관 기간: {reason}")
        elif self.emergency_retention_days > self.retention_days:
            # 긴급 하한이 평소 보관기간보다 길면 긴급 삭제가 아무 일도 하지 않는다
            errors.append("긴급 시 최소 보관일은 평소 보관 기간보다 길 수 없습니다.")

        if self.date_basis not in DATE_BASIS_CHOICES:
            errors.append("날짜 기준 값이 올바르지 않습니다.")
        if self.action not in ACTION_CHOICES:
            errors.append("처리 방식 값이 올바르지 않습니다.")
        if not self.include_patterns:
            errors.append("대상 파일 패턴을 최소 한 개 지정하세요.")
        if self.date_basis == "foldername" and not self.folder_date_format.strip():
            errors.append("폴더명 날짜 형식을 입력하세요. (예: %Y%m%d)")
        return errors

    @property
    def drive(self) -> str:
        try:
            return os.path.splitdrive(self.path)[0].upper()
        except (TypeError, ValueError):
            return ""


@dataclass
class Settings:
    """전역 설정 + 프로파일 목록."""

    version: int = 1
    scan_interval_minutes: int = 60
    global_dry_run: bool = True  # 도입 초기 기본값: 전체 모의실행
    run_on_start: bool = False
    log_retention_days: int = 90
    start_with_windows: bool = False
    warn_free_percent: float = 20.0
    """경고 기준이자 긴급 삭제의 목표치. 여기까지 확보되면 긴급 삭제를 멈춘다."""
    critical_free_percent: float = 10.0
    """이 밑으로 떨어지면 긴급 삭제가 발동한다."""

    capacity_guard: bool = True
    """용량 임계치 안전망 사용 여부. 기간 기준만으로 부족할 때의 최후 수단.

    켜져 있어도 전역 모의실행이 기본이라 곧바로 지우지는 않는다.
    긴급 정리는 프로파일마다 정한 emergency_retention_days 아래로는 내려가지 않는다.
    """

    schedule_enabled: bool = False
    schedule_start: str = "02:00"
    schedule_end: str = "05:00"
    """정리 작업을 허용할 시간대. 용량이 위험 수준이면 이 제한을 무시한다."""

    usage_tracking: bool = False
    """사용량 추이 기록. 켜면 1시간마다 여유 공간을 한 줄씩 남긴다."""
    usage_retention_days: int = 365

    fleet_share_path: str = ""
    """여러 검사기의 상태를 모을 공유 폴더. 비어 있으면 다중 PC 기능을 쓰지 않는다."""

    profiles: list[FolderProfile] = field(default_factory=list)

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not 1 <= int(self.scan_interval_minutes) <= 1440:
            errors.append("검사 주기는 1~1440분 사이여야 합니다.")
        if not 1 <= int(self.log_retention_days) <= 3650:
            errors.append("자체 로그 보관 기간은 1~3650일 사이여야 합니다.")
        if not 0 < float(self.critical_free_percent) < float(self.warn_free_percent) < 100:
            errors.append("여유공간 임계치는 0 < 위험 < 경고 < 100 이어야 합니다.")
        if self.schedule_enabled:
            if parse_hhmm(self.schedule_start) is None:
                errors.append("실행 시작 시각 형식이 올바르지 않습니다. (예: 02:00)")
            if parse_hhmm(self.schedule_end) is None:
                errors.append("실행 종료 시각 형식이 올바르지 않습니다. (예: 05:00)")
            if self.schedule_start.strip() == self.schedule_end.strip():
                errors.append("실행 시작 시각과 종료 시각이 같습니다.")
        if not 1 <= int(self.usage_retention_days) <= 3650:
            errors.append("사용량 기록 보관 기간은 1~3650일 사이여야 합니다.")
        share = (self.fleet_share_path or "").strip()
        if share and not Path(share).is_absolute():
            # 없는 폴더여도 통과시킨다. 네트워크가 잠시 끊겼을 뿐일 수 있다.
            errors.append("공유 폴더는 절대 경로여야 합니다. (예: \\\\서버\\공유\\DSM)")

        names = [p.name.strip() for p in self.profiles]
        if len(names) != len(set(names)):
            errors.append("프로파일 이름이 중복되었습니다.")
        for profile in self.profiles:
            for err in profile.validate():
                errors.append(f"[{profile.name}] {err}")
        return errors

    def enabled_profiles(self) -> list[FolderProfile]:
        return [p for p in self.profiles if p.enabled]

    def is_dry_run(self, profile: FolderProfile) -> bool:
        """전역 스위치가 켜져 있으면 개별 설정과 무관하게 모의실행."""
        return bool(self.global_dry_run or profile.dry_run)

    def drives(self) -> list[str]:
        seen: list[str] = []
        for profile in self.enabled_profiles():
            drive = profile.drive
            if drive and drive not in seen:
                seen.append(drive)
        return seen

    def within_schedule(self, now=None) -> bool:
        """지금 정기 정리를 돌려도 되는 시간인지."""
        if not self.schedule_enabled:
            return True
        return in_time_window(now, self.schedule_start, self.schedule_end)

    def schedule_text(self) -> str:
        if not self.schedule_enabled:
            return "제한 없음"
        return f"{self.schedule_start} ~ {self.schedule_end}"


# --- 직렬화 ----------------------------------------------------------------


def _coerce_profile(raw: dict[str, Any]) -> FolderProfile:
    """외부에서 편집된 JSON도 안전하게 읽도록 필드별로 보정한다."""
    defaults = FolderProfile()
    data = {name: raw.get(name, getattr(defaults, name)) for name in defaults.__dataclass_fields__}
    for key in ("include_patterns", "exclude_patterns"):
        data[key] = [str(p).strip() for p in (data[key] or []) if str(p).strip()]
    for number in ("retention_days", "emergency_retention_days"):
        try:
            data[number] = int(data[number])
        except (TypeError, ValueError):
            data[number] = getattr(defaults, number)
    for flag in ("enabled", "include_subfolders", "remove_empty_dirs", "dry_run"):
        data[flag] = bool(data[flag])
    for text in ("name", "path", "date_basis", "folder_date_format", "action"):
        data[text] = str(data[text])
    return FolderProfile(**data)


def load_settings() -> Settings:
    """설정을 읽는다. 파일이 없거나 손상됐으면 기본값을 돌려준다."""
    path = config_path()
    if not path.exists():
        return Settings()

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # 손상된 설정은 지우지 않고 따로 보관한다 (원인 추적용)
        try:
            path.replace(path.with_suffix(".corrupt.json"))
        except OSError:
            pass
        return Settings()

    if not isinstance(raw, dict):
        return Settings()

    defaults = Settings()
    data: dict[str, Any] = {}
    for key in defaults.__dataclass_fields__:
        if key != "profiles":
            data[key] = raw.get(key, getattr(defaults, key))
    data["profiles"] = [_coerce_profile(p) for p in raw.get("profiles", []) if isinstance(p, dict)]

    try:
        return Settings(**data)
    except TypeError:
        return Settings()


def save_settings(settings: Settings) -> None:
    """원자적 저장. 저장 중 정전이 나도 기존 설정이 남도록 임시파일 후 교체."""
    path = config_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2), encoding="utf-8")
    if path.exists():
        try:
            path.replace(path.with_suffix(".bak"))
        except OSError:
            pass
    tmp.replace(path)
