"""드라이브 사용량 기록과 포화 시점 예측.

정리 프로그램이 제 역할을 하는지는 "지금 여유가 얼마인가"가 아니라
"여유가 줄고 있는가"로 판단해야 한다. 한 시간에 한 줄씩 남겨 두면
몇 주 뒤 드라이브가 찰지 미리 알 수 있고, 보관 기간을 조정할 시간이 생긴다.

기록은 한 줄에 100바이트도 안 되므로 1년을 모아도 몇 백 KB 수준이다.
"""

from __future__ import annotations

import csv
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .audit import log
from .paths import data_dir

_HEADER = ("시각", "드라이브", "전체bytes", "사용bytes", "여유bytes", "여유퍼센트")

MIN_SAMPLES = 4
"""추세를 계산하기 위한 최소 표본 수."""

MIN_SPAN_HOURS = 6.0
"""표본이 이 시간 이상 걸쳐 있어야 추세를 신뢰한다."""


def usage_dir() -> Path:
    path = data_dir() / "usage"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class UsageSample:
    when: float
    drive: str
    total: int
    used: int
    free: int

    @property
    def free_percent(self) -> float:
        return (self.free / self.total * 100.0) if self.total else 0.0


@dataclass
class Trend:
    """일정 기간의 여유 공간 변화 추세."""

    window_days: int
    samples: int = 0
    span_days: float = 0.0
    slope_per_day: float = 0.0
    """하루당 여유 공간 변화량(bytes). 줄어들면 음수."""
    days_until_full: float | None = None
    full_at: float | None = None
    reason: str = ""

    @property
    def usable(self) -> bool:
        return not self.reason

    @property
    def shrinking(self) -> bool:
        return self.slope_per_day < 0

    def describe(self) -> str:
        if self.reason:
            return self.reason
        from .scanner import human_bytes

        if not self.shrinking:
            return f"여유 공간이 줄지 않고 있습니다 (하루 +{human_bytes(self.slope_per_day)})"
        text = f"하루 {human_bytes(-self.slope_per_day)}씩 감소"
        if self.days_until_full is not None and self.full_at is not None:
            when = datetime.fromtimestamp(self.full_at)
            text += f" · 약 {self.days_until_full:.0f}일 뒤 가득 참 ({when:%Y-%m-%d})"
        return text


# --- 기록 ------------------------------------------------------------------


def record(samples: list[UsageSample]) -> None:
    """현재 사용량을 한 줄씩 덧붙인다."""
    if not samples:
        return

    path = usage_dir() / f"usage-{datetime.now():%Y%m}.csv"
    try:
        is_new = not path.exists()
        with path.open("a", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            if is_new:
                writer.writerow(_HEADER)
            for sample in samples:
                writer.writerow((
                    datetime.fromtimestamp(sample.when).strftime("%Y-%m-%d %H:%M:%S"),
                    sample.drive,
                    sample.total,
                    sample.used,
                    sample.free,
                    f"{sample.free_percent:.2f}",
                ))
    except OSError:
        log.exception("사용량 기록 실패")


def load(drive: str, days: int, now: float | None = None) -> list[UsageSample]:
    """해당 드라이브의 최근 기록을 시간순으로 읽는다."""
    now = now if now is not None else time.time()
    cutoff = now - days * 86400.0

    # 기간에 걸치는 월별 파일만 읽는다
    wanted: list[Path] = []
    cursor = datetime.fromtimestamp(cutoff).replace(day=1)
    end = datetime.fromtimestamp(now)
    while cursor <= end:
        candidate = usage_dir() / f"usage-{cursor:%Y%m}.csv"
        if candidate.exists():
            wanted.append(candidate)
        # 다음 달 1일로
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)

    target = drive.upper()
    samples: list[UsageSample] = []
    for path in wanted:
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                for row in csv.reader(handle):
                    if len(row) < 6 or row[0] == _HEADER[0]:
                        continue
                    if row[1].upper() != target:
                        continue
                    try:
                        when = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S").timestamp()
                        if when < cutoff:
                            continue
                        samples.append(UsageSample(
                            when=when, drive=row[1],
                            total=int(row[2]), used=int(row[3]), free=int(row[4])))
                    except (ValueError, OverflowError):
                        continue
        except OSError:
            log.warning("사용량 파일을 읽지 못했습니다: %s", path)

    samples.sort(key=lambda s: s.when)
    return samples


def prune(retention_days: int) -> int:
    """보관 기간이 지난 월별 파일을 지운다."""
    removed = 0
    keep_from = datetime.now() - timedelta(days=max(1, int(retention_days)))
    for path in usage_dir().glob("usage-*.csv"):
        try:
            stamp = datetime.strptime(path.stem.removeprefix("usage-"), "%Y%m")
        except ValueError:
            continue
        # 그 달 전체가 보관 기간 밖일 때만 지운다
        month_end = (stamp.replace(day=28) + timedelta(days=4)).replace(day=1)
        if month_end < keep_from:
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
    return removed


# --- 분석 ------------------------------------------------------------------


def analyze(samples: list[UsageSample], window_days: int, now: float | None = None) -> Trend:
    """최소제곱법으로 여유 공간의 하루당 변화량과 포화 시점을 구한다.

    정리 작업 때문에 여유가 계단식으로 튀어 오르지만, 직선을 맞추면
    '정리하고도 남는 순증가'가 기울기로 나온다. 우리가 알고 싶은 게 그것이다.
    """
    now = now if now is not None else time.time()
    trend = Trend(window_days=window_days)

    window = [s for s in samples if s.when >= now - window_days * 86400.0]
    trend.samples = len(window)
    if len(window) < MIN_SAMPLES:
        trend.reason = "기록이 부족합니다"
        return trend

    span_seconds = window[-1].when - window[0].when
    trend.span_days = span_seconds / 86400.0
    if span_seconds < MIN_SPAN_HOURS * 3600:
        trend.reason = "기록 기간이 너무 짧습니다"
        return trend

    # x는 일 단위, y는 여유 bytes
    base = window[0].when
    xs = [(s.when - base) / 86400.0 for s in window]
    ys = [float(s.free) for s in window]
    count = len(xs)
    mean_x = sum(xs) / count
    mean_y = sum(ys) / count
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0:
        trend.reason = "기록이 한 시점에 몰려 있습니다"
        return trend

    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denominator
    trend.slope_per_day = slope

    if slope < 0:
        current_free = window[-1].free
        days_left = current_free / (-slope)
        # 수백 년 뒤 같은 값은 의미가 없다
        if days_left <= 3650:
            trend.days_until_full = days_left
            trend.full_at = now + days_left * 86400.0

    return trend


def latest_free_percent(samples: list[UsageSample]) -> float | None:
    return samples[-1].free_percent if samples else None
