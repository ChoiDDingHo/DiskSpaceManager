"""로그온 시 자동 시작 등록.

Windows 서비스가 아니라 일반 프로세스로 띄운다. 서비스는 Session 0 격리 때문에
트레이 아이콘이 보이지 않기 때문이다.
"""

from __future__ import annotations

import sys
import winreg
from pathlib import Path

from . import APP_NAME
from .audit import log

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _launch_command() -> str:
    """자동 시작에 등록할 명령줄."""
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'

    # 콘솔 창이 뜨지 않도록 pythonw.exe 를 쓴다
    interpreter = Path(sys.executable)
    pythonw = interpreter.with_name("pythonw.exe")
    if not pythonw.exists():
        pythonw = interpreter
    script = Path(__file__).resolve().parent.parent / "DiskSpaceManager.py"
    return f'"{pythonw}" "{script}"'


def is_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
            return bool(value)
    except OSError:
        return False


def set_enabled(enabled: bool) -> tuple[bool, str]:
    """자동 시작을 켜고 끈다. (성공여부, 메시지)"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _launch_command())
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
    except OSError as exc:
        log.warning("자동 시작 설정 실패: %s", exc)
        return False, f"자동 시작 설정에 실패했습니다: {exc}"

    log.info("자동 시작 %s", "등록" if enabled else "해제")
    return True, ""
