"""데이터 폴더 위치 결정과 단일 인스턴스 보장."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path

from . import APP_NAME

_ERROR_ALREADY_EXISTS = 183
_mutex_handle = None


def app_root() -> Path:
    """실행 파일(또는 소스)이 놓인 폴더."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_root() -> Path:
    """아이콘 같은 동봉 자원이 놓인 폴더.

    PyInstaller 로 onefile 빌드하면 자원은 exe 옆이 아니라 임시 폴더
    (sys._MEIPASS)에 풀린다. 그래서 설치 폴더를 가리키는 app_root() 와 구분한다.
    """
    bundled = getattr(sys, "_MEIPASS", None)
    return Path(bundled) if bundled else app_root()


def data_dir() -> Path:
    """설정과 로그가 저장되는 폴더.

    우선순위는 DSM_DATA_DIR 환경변수, 그다음 LOCALAPPDATA 아래의 앱 폴더다.
    검사기 여러 대에 같은 설정을 배포할 때는 환경변수로 공용 경로를 지정한다.
    """
    override = os.environ.get("DSM_DATA_DIR")
    if override:
        base = Path(override).expanduser()
    else:
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        base = Path(local) / APP_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def config_path() -> Path:
    return data_dir() / "config.json"


def log_dir() -> Path:
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def audit_dir() -> Path:
    path = data_dir() / "audit"
    path.mkdir(parents=True, exist_ok=True)
    return path


def acquire_single_instance() -> bool:
    """이미 실행 중이면 False.

    검사기에서 중복 기동으로 같은 폴더를 동시에 지우는 사고를 막는다.
    """
    global _mutex_handle

    name = "Global\\" + APP_NAME + "_SingleInstance"
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, name)
    if not handle:
        return True  # 뮤텍스 생성 자체가 실패하면 기동을 막지는 않는다
    if ctypes.windll.kernel32.GetLastError() == _ERROR_ALREADY_EXISTS:
        ctypes.windll.kernel32.CloseHandle(handle)
        return False
    _mutex_handle = handle
    return True


def open_in_explorer(path: Path | str) -> None:
    """탐색기로 폴더 열기."""
    target = Path(path)
    if not target.exists():
        return
    try:
        os.startfile(str(target))
    except OSError:
        subprocess.Popen(["explorer", str(target)])


def long_path(path: str) -> str:
    """260자 제한을 우회하는 확장 경로 접두사를 붙인다.

    검사 로그는 날짜/모델/카메라별로 폴더가 깊어져 260자를 넘기는 경우가 있고,
    그 파일들이 조용히 삭제 실패로 남으면 용량이 줄지 않는다.
    """
    if len(path) < 250 or path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path
