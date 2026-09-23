"""앱 로그와 감사(삭제 이력) 로그.

감사 로그가 이 프로그램의 신뢰를 지탱한다. 나중에 "그 날짜 이미지 어디 갔냐"는
질문이 반드시 나오고, 그때 실물 파일은 없더라도 경로/크기/날짜는 답할 수 있어야
한다. 그래서 삭제한 파일의 메타데이터를 CSV로 따로 남긴다.

로그를 지우는 프로그램의 로그가 쌓여서 용량을 먹으면 안 되므로,
자체 로그도 보관 기간이 지나면 정리한다.
"""

from __future__ import annotations

import csv
import logging
import logging.handlers
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import TracebackType

from .paths import audit_dir, log_dir
from .scanner import Candidate

_AUDIT_HEADER = (
    "시각",
    "프로파일",
    "모드",
    "결과",
    "경로",
    "크기(bytes)",
    "파일기준시각",
    "경과일",
    "날짜기준",
    "비고",
)

log = logging.getLogger("dsm")


def setup_logging(debug: bool = False) -> None:
    """회전식 앱 로그를 설정한다."""
    log.setLevel(logging.DEBUG if debug else logging.INFO)
    log.handlers.clear()

    handler = logging.handlers.RotatingFileHandler(
        log_dir() / "app.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    log.addHandler(handler)
    log.propagate = False


class AuditWriter:
    """한 번의 정리 작업 동안 열어 두는 감사 로그 기록기."""

    def __init__(self, profile_name: str, mode: str) -> None:
        self.profile_name = profile_name
        self.mode = mode  # DRY-RUN / DELETE / RECYCLE
        self._handle = None
        self._writer: csv.writer | None = None
        self.rows = 0

    def __enter__(self) -> AuditWriter:
        path = audit_dir() / f"audit-{datetime.now():%Y%m%d}.csv"
        is_new = not path.exists()
        # utf-8-sig: 엑셀에서 한글이 깨지지 않게 BOM을 붙인다
        self._handle = path.open("a", encoding="utf-8-sig", newline="")
        self._writer = csv.writer(self._handle)
        if is_new:
            self._writer.writerow(_AUDIT_HEADER)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def write(self, candidate: Candidate, ok: bool, note: str = "") -> None:
        if self._writer is None:
            return
        self._writer.writerow(
            (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                self.profile_name,
                self.mode,
                "OK" if ok else "FAIL",
                candidate.path,
                candidate.size,
                datetime.fromtimestamp(candidate.ref_time).strftime("%Y-%m-%d %H:%M:%S"),
                f"{candidate.age_days:.1f}",
                candidate.basis,
                note,
            )
        )
        self.rows += 1

    def note(self, text: str) -> None:
        """파일 단위가 아닌 요약/특이사항 한 줄."""
        if self._writer is None:
            return
        self._writer.writerow(
            (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                self.profile_name,
                self.mode,
                "INFO",
                "",
                "",
                "",
                "",
                "",
                text,
            )
        )


_ALERT_HEADER = ("시각", "종류", "내용")


def write_alert(kind: str, message: str) -> None:
    """사람이 나중에 찾아볼 수 있게 알림을 남긴다.

    메일이나 메신저로 보내지 않고 기록만 한다. 담당자가 주기적으로 확인하거나,
    용량 문제가 터진 뒤 "경고가 언제부터 나왔는지" 되짚을 때 쓰는 자료다.
    app.log 에도 WARNING 으로 남기지만, 그쪽은 동작 로그라 금방 묻히기 때문에
    알림만 모아 둔 파일을 따로 유지한다.
    """
    log.warning("[알림] %s: %s", kind, message)

    path = audit_dir() / "alerts.csv"
    try:
        is_new = not path.exists()
        with path.open("a", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            if is_new:
                writer.writerow(_ALERT_HEADER)
            writer.writerow((datetime.now().strftime("%Y-%m-%d %H:%M:%S"), kind, message))
    except OSError:
        log.exception("알림 기록 실패")


def read_recent_alerts(limit: int = 20) -> list[tuple[str, str, str]]:
    """최근 알림을 새 것부터. 상태 창에서 보여 주기 위한 용도."""
    path = audit_dir() / "alerts.csv"
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = [row for row in csv.reader(handle) if len(row) == 3]
    except OSError:
        return []
    if rows and rows[0] == list(_ALERT_HEADER):
        rows = rows[1:]
    return rows[-limit:][::-1]


def prune_own_logs(retention_days: int) -> int:
    """보관 기간이 지난 감사 로그와 회전된 앱 로그를 지운다."""
    removed = 0
    cutoff = time.time() - max(1, int(retention_days)) * 86400

    for path in audit_dir().glob("audit-*.csv"):
        try:
            stamp = datetime.strptime(path.stem.removeprefix("audit-"), "%Y%m%d")
        except ValueError:
            continue
        # 날짜 폴더명과 같은 이유로 파일명 날짜를 신뢰한다 (복사해도 안 바뀜)
        if stamp < datetime.now() - timedelta(days=max(1, int(retention_days))):
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue

    for path in log_dir().glob("app.log.*"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue

    return removed


def latest_audit_file() -> Path | None:
    files = sorted(audit_dir().glob("audit-*.csv"))
    return files[-1] if files else None
