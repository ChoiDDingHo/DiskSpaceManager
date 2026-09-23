"""DiskSpaceManager 진입점.

검사기 PC의 검사 결과 로그(이미지/영상)를 보관 기간에 따라 자동 정리한다.
백그라운드 상주 프로그램이며 시스템 트레이에서 상태 확인과 설정을 한다.

Phase 1 (MVP) 범위
  - 트레이 상주, 폴더 프로파일, 기간 기준 삭제, 모의실행, 감사 로그, 설정창
"""

from __future__ import annotations

import ctypes
import sys
import traceback

from dsm import APP_TITLE
from dsm.audit import log, setup_logging
from dsm.config import load_settings
from dsm.engine import Engine
from dsm.paths import acquire_single_instance, data_dir
from dsm.perf import lower_process_priority, memory_usage_mb, trim_working_set
from dsm.settings_ui import UiHost
from dsm.tray import TrayApp


def _enable_dpi_awareness() -> None:
    """고해상도 화면에서 설정창이 흐리게 보이지 않도록."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def _set_app_id() -> None:
    """작업표시줄에서 Python 이 아니라 이 앱으로 묶이고 아이콘도 앱 것으로 나오게 한다."""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DiskSpaceManager.App")
    except (AttributeError, OSError):
        pass


def _message_box(title: str, text: str) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x40)
    except (AttributeError, OSError):
        print(f"{title}: {text}")


def main() -> int:
    _enable_dpi_awareness()
    _set_app_id()

    if not acquire_single_instance():
        _message_box(APP_TITLE, "이미 실행 중입니다. 시스템 트레이를 확인하세요.")
        return 0

    setup_logging()
    log.info("=" * 60)
    log.info("%s 시작 (데이터 폴더: %s)", APP_TITLE, data_dir())

    # 검사 설비가 도는 PC에 상주하므로 스스로를 낮은 우선순위에 둔다
    if lower_process_priority():
        log.info("프로세스 우선순위를 '보통보다 낮음'으로 설정")

    settings = load_settings()
    issues = settings.validate()
    if issues:
        # 설정이 어긋나도 기동은 시킨다. 트레이에서 고칠 수 있어야 하기 때문이다.
        log.warning("설정에 문제가 있습니다: %s", " / ".join(issues))

    engine = Engine(settings)
    host = UiHost(engine)
    tray = TrayApp(engine, host)

    host.start()
    engine.start()

    if not settings.profiles:
        # 첫 실행이면 무엇을 해야 하는지 바로 보여준다
        log.info("등록된 프로파일이 없어 설정창을 엽니다")
        host.open_settings()

    # 기동하며 올라온 페이지를 반납하고 상주 상태로 들어간다
    trim_working_set()
    log.info("상주 시작 (메모리 %.1fMB)", memory_usage_mb())

    try:
        tray.run()  # 트레이 루프가 끝나면 종료 절차로 넘어간다
    finally:
        log.info("종료 처리 중...")
        engine.stop()
        host.stop()
        log.info("%s 종료", APP_TITLE)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        setup_logging()
        log.critical("처리되지 않은 예외:\n%s", traceback.format_exc())
        _message_box(APP_TITLE, f"예기치 않은 오류로 종료되었습니다.\n\n{traceback.format_exc()}")
        sys.exit(1)
