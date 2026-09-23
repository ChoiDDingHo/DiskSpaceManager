"""백그라운드 정리 워커와 상태 관리.

트레이 UI는 이 엔진의 상태를 비추기만 하고, 실제 판단과 작업은 전부 여기서 한다.
UI가 멈추거나 닫혀도 정리는 계속되어야 하기 때문이다.
"""

from __future__ import annotations

import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import usage
from .audit import AuditWriter, log, prune_own_logs, write_alert
from .config import Settings, load_settings, save_settings
from .fleet import build_payload, publish
from .perf import begin_background_io, end_background_io, memory_usage_mb, trim_working_set
from .scanner import Candidate, ExecResult, PreviewResult, execute, human_bytes, preview

USAGE_INTERVAL_SECONDS = 3600.0
"""사용량 추이를 기록하는 간격. 1시간이면 추세를 보기에 충분하고 파일도 작다."""

FLEET_PUBLISH_SECONDS = 600.0
"""공유 폴더에 상태를 올리는 간격. 네트워크 접근이라 자주 할 이유가 없다."""

STATUS_REFRESH_SECONDS = 120.0
"""작업이 없을 때 깨어나는 주기.

트레이 툴팁의 여유 공간 표시를 갱신하는 용도라 자주 돌 이유가 없다.
상태 창이 열려 있는 동안에는 창이 직접 refresh_disks() 를 호출해 최신값을 본다.
"""


@dataclass
class DiskInfo:
    drive: str
    total: int = 0
    used: int = 0
    free: int = 0
    ok: bool = True
    level: str = "ok"  # ok / warn / critical

    @property
    def free_percent(self) -> float:
        return (self.free / self.total * 100.0) if self.total else 0.0

    def describe(self) -> str:
        if not self.ok:
            return f"{self.drive} 확인 불가"
        return f"{self.drive} 여유 {human_bytes(self.free)} ({self.free_percent:.0f}%)"


@dataclass
class CycleSummary:
    """한 번의 정리 사이클 결과."""

    started_at: float
    finished_at: float = 0.0
    results: list[ExecResult] = field(default_factory=list)
    aborted: bool = False

    @property
    def deleted(self) -> int:
        return sum(r.deleted for r in self.results)

    @property
    def freed_bytes(self) -> int:
        return sum(r.freed_bytes for r in self.results)

    @property
    def failed(self) -> int:
        return sum(r.failed for r in self.results)

    @property
    def removed_dirs(self) -> int:
        return sum(r.removed_dirs for r in self.results)

    @property
    def all_dry_run(self) -> bool:
        return bool(self.results) and all(r.dry_run for r in self.results)

    @property
    def had_emergency(self) -> bool:
        return any(r.emergency for r in self.results)

    def describe(self) -> str:
        if not self.results:
            return "정리 대상 프로파일이 없습니다"
        verb = "대상" if self.all_dry_run else "삭제"
        text = f"{verb} {self.deleted}개 / {human_bytes(self.freed_bytes)}"
        if self.failed:
            text += f", 실패 {self.failed}건"
        if self.all_dry_run:
            text += " (모의실행)"
        if self.had_emergency:
            text += " · 긴급 정리 포함"
        return text


@dataclass
class EngineStatus:
    """트레이/설정창이 읽어 가는 상태 스냅샷."""

    state: str = "idle"  # idle / running / paused
    next_run_at: float = 0.0
    paused_until: float = 0.0
    last_cycle: CycleSummary | None = None
    disks: list[DiskInfo] = field(default_factory=list)
    dry_run: bool = True
    profile_count: int = 0
    schedule_text: str = "제한 없음"
    schedule_enabled: bool = False
    within_schedule: bool = True

    @property
    def level(self) -> str:
        """트레이 아이콘 색을 결정하는 종합 상태."""
        if self.state == "running":
            return "busy"
        if self.state == "paused":
            return "paused"
        if self.last_cycle is not None and self.last_cycle.failed:
            return "warn"
        for disk in self.disks:
            if disk.level == "critical":
                return "critical"
        for disk in self.disks:
            if disk.level == "warn":
                return "warn"
        return "ok"

    def tooltip(self) -> str:
        lines = ["Disk Space Manager"]
        if self.disks:
            lines.extend(disk.describe() for disk in self.disks)
        else:
            lines.append("관리 대상 폴더가 없습니다")

        if self.state == "running":
            lines.append("정리 작업 진행 중...")
        elif self.state == "paused" and self.paused_until:
            lines.append(f"일시정지 ({datetime.fromtimestamp(self.paused_until):%H:%M} 까지)")
        elif self.schedule_enabled and not self.within_schedule:
            lines.append(f"대기 중 · 실행 시간대 {self.schedule_text}")
        elif self.next_run_at:
            lines.append(f"다음 검사 {datetime.fromtimestamp(self.next_run_at):%m-%d %H:%M}")

        if self.last_cycle is not None:
            when = datetime.fromtimestamp(self.last_cycle.finished_at)
            lines.append(f"최근 {when:%m-%d %H:%M} · {self.last_cycle.describe()}")
        if self.dry_run:
            lines.append("전역 모의실행 켜짐 — 실제 삭제 안 함")
        # 윈도우 툴팁은 128자 제한이 있어 넘치면 잘린다
        return "\n".join(lines)[:127]


class Engine:
    """정리 작업 스케줄러."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._lock = threading.RLock()
        self._wake = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._settings = settings or load_settings()
        self._run_requested = False
        self._running = False
        self._paused_until = 0.0
        self._next_run_at = 0.0
        self._last_cycle: CycleSummary | None = None
        self._disks: list[DiskInfo] = []
        self._last_signature: tuple | None = None
        self._logged_schedule_skip = False
        self._next_usage_at = 0.0
        self._next_publish_at = 0.0

        self.on_change: list = []
        self.on_notify: list = []

    # --- 설정 -------------------------------------------------------------

    @property
    def settings(self) -> Settings:
        with self._lock:
            return self._settings

    def apply_settings(self, settings: Settings, persist: bool = True) -> None:
        with self._lock:
            self._settings = settings
            if persist:
                save_settings(settings)
            self._next_run_at = time.time() + settings.scan_interval_minutes * 60
            self._wake.notify_all()
        log.info("설정 적용: 프로파일 %d개, 주기 %d분, 전역 모의실행 %s",
                 len(settings.profiles), settings.scan_interval_minutes, settings.global_dry_run)
        self.refresh_disks()
        self._emit_change()

    # --- 수명주기 ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self.refresh_disks()
        with self._lock:
            interval = self._settings.scan_interval_minutes * 60
            self._next_run_at = time.time() + interval
            if self._settings.run_on_start:
                self._run_requested = True
        self._thread = threading.Thread(target=self._loop, name="dsm-worker", daemon=True)
        self._thread.start()
        log.info("워커 시작")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        with self._lock:
            self._wake.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        log.info("워커 종료")

    # --- 외부 조작 --------------------------------------------------------

    def request_run(self) -> None:
        """지금 정리 실행."""
        with self._lock:
            self._run_requested = True
            self._paused_until = 0.0
            self._wake.notify_all()

    def pause_for(self, minutes: int) -> None:
        with self._lock:
            self._paused_until = time.time() + minutes * 60
            self._wake.notify_all()
        log.info("일시정지 %d분", minutes)
        self._emit_change()

    def pause_until_tomorrow(self) -> None:
        tomorrow = (datetime.now() + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        with self._lock:
            self._paused_until = tomorrow.timestamp()
            self._wake.notify_all()
        log.info("일시정지 (오늘 하루)")
        self._emit_change()

    def resume(self) -> None:
        with self._lock:
            self._paused_until = 0.0
            self._wake.notify_all()
        log.info("일시정지 해제")
        self._emit_change()

    @property
    def is_paused(self) -> bool:
        with self._lock:
            return self._paused_until > time.time()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    # --- 상태 -------------------------------------------------------------

    def status(self) -> EngineStatus:
        with self._lock:
            settings = self._settings
            if self._running:
                state = "running"
            elif self._paused_until > time.time():
                state = "paused"
            else:
                state = "idle"
            return EngineStatus(
                state=state,
                next_run_at=self._next_run_at,
                paused_until=self._paused_until,
                last_cycle=self._last_cycle,
                disks=list(self._disks),
                dry_run=settings.global_dry_run,
                profile_count=len(settings.enabled_profiles()),
                schedule_text=settings.schedule_text(),
                schedule_enabled=settings.schedule_enabled,
                within_schedule=settings.within_schedule(),
            )

    def refresh_disks(self) -> None:
        """드라이브 여유 공간을 다시 읽는다. 상태 창이 열려 있는 동안에도 호출한다."""
        settings = self.settings
        disks: list[DiskInfo] = []
        for drive in settings.drives():
            info = DiskInfo(drive=drive)
            try:
                usage = shutil.disk_usage(drive + "\\")
                info.total, info.used, info.free = usage.total, usage.used, usage.free
                if info.free_percent < settings.critical_free_percent:
                    info.level = "critical"
                elif info.free_percent < settings.warn_free_percent:
                    info.level = "warn"
            except OSError:
                info.ok = False
                info.level = "warn"
            disks.append(info)
        with self._lock:
            self._disks = disks

    # --- 콜백 -------------------------------------------------------------

    def _emit_change(self) -> None:
        for callback in list(self.on_change):
            try:
                callback()
            except Exception:  # UI 콜백이 엔진을 멈추게 두지 않는다
                log.exception("상태 콜백 실패")

    def _emit_change_if_changed(self) -> None:
        """보이는 내용이 실제로 달라졌을 때만 콜백을 부른다.

        주기적으로 깨어날 때마다 트레이를 갱신하면 Shell_NotifyIcon 호출만
        늘어난다. 유휴 상태에서는 대부분 아무것도 바뀌지 않는다.
        """
        status = self.status()
        signature = (status.level, status.tooltip())
        if signature == self._last_signature:
            return
        self._last_signature = signature
        self._emit_change()

    def _emit_notify(self, title: str, message: str) -> None:
        for callback in list(self.on_notify):
            try:
                callback(title, message)
            except Exception:
                log.exception("알림 콜백 실패")

    # --- 미리보기 ---------------------------------------------------------

    def preview_all(self, should_stop=None) -> list[PreviewResult]:
        """설정을 바꾸지 않고 대상만 집계한다."""
        results = []
        for profile in self.settings.enabled_profiles():
            results.append(preview(profile, should_stop=should_stop))
        return results

    # --- 실행 -------------------------------------------------------------

    def _loop(self) -> None:
        # 스캔과 삭제는 전부 이 스레드에서 일어난다. 백그라운드 모드로 두면
        # CPU 뿐 아니라 디스크 I/O 우선순위까지 낮아져, 검사 프로그램이
        # 이미지를 쓰는 동안 정리 작업이 디스크를 선점하지 않는다.
        if begin_background_io():
            log.info("워커 스레드를 백그라운드 I/O 모드로 전환")

        try:
            self._loop_body()
        finally:
            end_background_io()

    def _loop_body(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                now = time.time()
                paused = self._paused_until > now
                due = (not paused) and now >= self._next_run_at
                trigger = self._run_requested

                if due and not trigger and not self._schedule_allows_now():
                    # 지정 시간대 밖이다. 다음 주기로 미루되, 용량이 위험하면
                    # 시간대를 무시하고 그대로 진행한다.
                    self._next_run_at = now + min(
                        self._settings.scan_interval_minutes * 60, 600)
                    due = False
                    if not self._logged_schedule_skip:
                        log.info("실행 시간대(%s) 밖이라 정리를 미룹니다",
                                 self._settings.schedule_text())
                        self._logged_schedule_skip = True

                if not (due or trigger):
                    # 다음에 깨어날 시각: 예정 실행 / 일시정지 해제 / 상태 갱신
                    targets = [now + STATUS_REFRESH_SECONDS]
                    if not paused:
                        targets.append(self._next_run_at)
                    if paused:
                        targets.append(self._paused_until)
                    timeout = max(0.5, min(targets) - now)
                    self._wake.wait(timeout)
                    self.refresh_disks()
                    self.record_usage_if_due()
                    self.publish_fleet_if_due()
                    self._emit_change_if_changed()
                    continue

                self._run_requested = False
                self._running = True

            self._emit_change()
            try:
                self._run_cycle()
            except Exception:
                log.exception("정리 사이클 실패")
            finally:
                with self._lock:
                    self._running = False
                    self._next_run_at = time.time() + self._settings.scan_interval_minutes * 60
                self.refresh_disks()
                self._emit_change()
                # 큰 폴더를 훑고 나면 잡아 둔 페이지가 그대로 남는다.
                # 다시 유휴로 돌아가는 지금이 반납하기 좋은 시점이다.
                before = memory_usage_mb()
                if trim_working_set():
                    log.debug("작업 세트 정리: %.1fMB -> %.1fMB", before, memory_usage_mb())

    def _should_stop(self) -> bool:
        return self._stop.is_set()

    def record_usage_if_due(self, force: bool = False) -> None:
        """사용량 추이를 한 줄 남긴다. 설정이 꺼져 있으면 아무것도 하지 않는다."""
        settings = self.settings
        if not settings.usage_tracking:
            return
        now = time.time()
        if not force and now < self._next_usage_at:
            return
        self._next_usage_at = now + USAGE_INTERVAL_SECONDS

        samples = [
            usage.UsageSample(when=now, drive=disk.drive, total=disk.total,
                              used=disk.used, free=disk.free)
            for disk in self._disks if disk.ok and disk.total
        ]
        usage.record(samples)

    def publish_fleet_if_due(self, force: bool = False) -> None:
        """공유 폴더에 이 PC 상태를 올린다. 경로가 없으면 아무것도 하지 않는다."""
        settings = self.settings
        if not (settings.fleet_share_path or "").strip():
            return
        now = time.time()
        if not force and now < self._next_publish_at:
            return
        self._next_publish_at = now + FLEET_PUBLISH_SECONDS
        publish(settings.fleet_share_path, build_payload(self.status(), settings))

    def _schedule_allows_now(self) -> bool:
        """지금 정기 정리를 돌려도 되는지.

        시간대 제한이 걸려 있어도, 여유 공간이 위험 수준이면 기다리지 않는다.
        용량이 꽉 차서 설비가 느려지는 것을 막는 게 이 프로그램의 목적이기 때문이다.
        """
        settings = self._settings
        if settings.within_schedule():
            self._logged_schedule_skip = False
            return True

        if settings.capacity_guard:
            for disk in self._disks:
                if disk.ok and disk.free_percent < settings.critical_free_percent:
                    log.warning("실행 시간대 밖이지만 %s 여유 %.1f%% 라 즉시 정리합니다",
                                disk.drive, disk.free_percent)
                    write_alert("시간대 예외",
                                f"{disk.drive} 여유 {disk.free_percent:.1f}% 로 위험 수준이라 "
                                f"지정 시간대({settings.schedule_text()}) 밖에 정리를 실행합니다.")
                    return True
        return False

    def _run_cycle(self) -> None:
        settings = self.settings
        summary = CycleSummary(started_at=time.time())
        profiles = settings.enabled_profiles()

        log.info("정리 시작: 프로파일 %d개", len(profiles))
        for profile in profiles:
            if self._stop.is_set():
                summary.aborted = True
                break

            dry_run = settings.is_dry_run(profile)
            mode = "DRY-RUN" if dry_run else profile.action.upper()
            try:
                with AuditWriter(profile.name, mode) as audit:

                    def record(candidate: Candidate, ok: bool, note: str) -> None:
                        audit.write(candidate, ok, note)

                    result = execute(
                        profile,
                        dry_run=dry_run,
                        on_file=record,
                        should_stop=self._should_stop,
                    )
                    audit.note(
                        f"요약: 대상 {result.deleted}개 / {human_bytes(result.freed_bytes)}, "
                        f"실패 {result.failed}, 빈폴더 {result.removed_dirs}, "
                        f"스캔 {result.stats.scanned_files}개"
                    )
            except OSError:
                log.exception("[%s] 감사 로그 기록 실패", profile.name)
                continue

            summary.results.append(result)
            log.info(
                "[%s] %s 대상 %d개 / %s, 실패 %d, 빈폴더 %d, %.1f초",
                profile.name, mode, result.deleted, human_bytes(result.freed_bytes),
                result.failed, result.removed_dirs, result.elapsed,
            )
            for message in result.stats.errors[:5]:
                log.warning("[%s] %s", profile.name, message)

        summary.finished_at = time.time()
        with self._lock:
            self._last_cycle = summary

        try:
            pruned = prune_own_logs(settings.log_retention_days)
            if settings.usage_tracking:
                pruned += usage.prune(settings.usage_retention_days)
            if pruned:
                log.info("오래된 자체 로그 %d개 정리", pruned)
        except OSError:
            log.exception("자체 로그 정리 실패")

        # 정리 직후의 여유 공간은 추세에서 의미 있는 지점이라 바로 남긴다
        self.refresh_disks()
        self.record_usage_if_due(force=True)
        self.publish_fleet_if_due(force=True)

        if settings.capacity_guard:
            try:
                self._run_capacity_guard(settings, summary)
            except Exception:
                log.exception("용량 안전망 실행 실패")

        if summary.failed:
            write_alert("삭제 실패", f"이번 정리에서 {summary.failed}건을 지우지 못했습니다. "
                                 f"감사 로그의 FAIL 행을 확인하세요.")

        log.info("정리 완료: %s", summary.describe())
        if summary.deleted or summary.failed:
            self._emit_notify("정리 완료", summary.describe())

    # --- 용량 안전망 ------------------------------------------------------

    def _free_percent(self, drive: str) -> float | None:
        try:
            usage = shutil.disk_usage(drive + "\\")
        except OSError:
            return None
        return (usage.free / usage.total * 100.0) if usage.total else None

    def _run_capacity_guard(self, settings: Settings, summary: CycleSummary) -> None:
        """기간 기준으로 정리했는데도 여유 공간이 위험 수준이면 긴급 정리를 한다.

        평소 보관 기간을 무시하되, 프로파일마다 정해 둔 '긴급 시 최소 보관일'
        아래로는 절대 내려가지 않는다. 목표(경고 기준)를 확보하면 즉시 멈춘다.
        """
        for drive in settings.drives():
            if self._stop.is_set():
                return

            free = self._free_percent(drive)
            if free is None or free >= settings.critical_free_percent:
                continue

            write_alert(
                "용량 위험",
                f"{drive} 여유 {free:.1f}% — 위험 기준 {settings.critical_free_percent:.0f}% 미만. "
                f"긴급 정리를 시작합니다 (목표 {settings.warn_free_percent:.0f}%).",
            )
            self._emergency_pass(settings, summary, drive)

            after = self._free_percent(drive)
            if after is None:
                continue
            if after >= settings.warn_free_percent:
                write_alert("긴급 정리 완료", f"{drive} 여유 {after:.1f}% 확보")
            else:
                # 여기서 멈추는 게 맞다. 더 지우려면 보존 규정을 건드려야 한다.
                write_alert(
                    "긴급 정리 부족",
                    f"{drive} 여유 {after:.1f}% — 긴급 하한까지 지워도 목표 "
                    f"{settings.warn_free_percent:.0f}% 에 도달하지 못했습니다. "
                    "보관 기간 조정이나 저장소 증설이 필요합니다.",
                )

    def _emergency_pass(self, settings: Settings, summary: CycleSummary, drive: str) -> None:
        """해당 드라이브의 프로파일을 목록 순서대로 긴급 하한까지 정리한다."""
        target = settings.warn_free_percent

        def goal_reached() -> bool:
            free = self._free_percent(drive)
            return free is not None and free >= target

        for profile in settings.enabled_profiles():
            if self._stop.is_set() or profile.drive != drive:
                continue
            if goal_reached():
                return
            # 평소 보관 기간과 같으면 이미 정규 정리에서 다 지웠다
            if profile.emergency_retention_days >= profile.retention_days:
                continue

            dry_run = settings.is_dry_run(profile)
            mode = "DRY-RUN" if dry_run else profile.action.upper()
            try:
                with AuditWriter(profile.name, f"{mode}/긴급") as audit:
                    result = execute(
                        profile,
                        dry_run=dry_run,
                        on_file=audit.write,
                        should_stop=self._should_stop,
                        retention_days=profile.emergency_retention_days,
                        goal_reached=goal_reached,
                    )
                    audit.note(
                        f"긴급 정리: 보관 {profile.retention_days}일 -> "
                        f"{profile.emergency_retention_days}일 적용, "
                        f"대상 {result.deleted}개 / {human_bytes(result.freed_bytes)}"
                    )
            except OSError:
                log.exception("[%s] 긴급 정리 감사 로그 기록 실패", profile.name)
                continue

            result.emergency = True
            summary.results.append(result)
            log.warning(
                "[%s] 긴급 정리 %s: %d개 / %s (보관 %d일 적용)",
                profile.name, mode, result.deleted, human_bytes(result.freed_bytes),
                profile.emergency_retention_days,
            )
