"""자원 사용을 낮추는 Windows 전용 장치들.

이 프로그램은 검사 설비가 돌아가는 PC에 상주한다. 정리 작업이 설비의 CPU와
디스크를 빼앗으면, 용량을 확보하려다 오히려 설비를 느리게 만드는 셈이 된다.
그래서 기본적으로 자신을 낮은 우선순위에 두고, 스캔/삭제 스레드는 Windows의
백그라운드 모드로 돌린다.

64비트에서 핸들이 잘리지 않도록 모든 API 의 argtypes/restype 을 명시한다.
모든 함수는 실패해도 조용히 넘어간다. 성능 조정이 안 된다고 정리 기능 자체가
멈출 이유는 없다.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from .audit import log

BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
THREAD_MODE_BACKGROUND_BEGIN = 0x00010000
THREAD_MODE_BACKGROUND_END = 0x00020000

SIZE_T = ctypes.c_size_t

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_psapi = ctypes.WinDLL("psapi", use_last_error=True)

_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
_kernel32.GetCurrentProcess.argtypes = []
_kernel32.GetCurrentThread.restype = wintypes.HANDLE
_kernel32.GetCurrentThread.argtypes = []

_kernel32.SetPriorityClass.restype = wintypes.BOOL
_kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]

_kernel32.SetThreadPriority.restype = wintypes.BOOL
_kernel32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]

_kernel32.SetProcessWorkingSetSize.restype = wintypes.BOOL
_kernel32.SetProcessWorkingSetSize.argtypes = [wintypes.HANDLE, SIZE_T, SIZE_T]


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", SIZE_T),
        ("WorkingSetSize", SIZE_T),
        ("QuotaPeakPagedPoolUsage", SIZE_T),
        ("QuotaPagedPoolUsage", SIZE_T),
        ("QuotaPeakNonPagedPoolUsage", SIZE_T),
        ("QuotaNonPagedPoolUsage", SIZE_T),
        ("PagefileUsage", SIZE_T),
        ("PeakPagefileUsage", SIZE_T),
    ]


_psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
_psapi.GetProcessMemoryInfo.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(_ProcessMemoryCounters),
    wintypes.DWORD,
]


def lower_process_priority() -> bool:
    """프로세스 우선순위를 '보통보다 낮음'으로 내린다.

    IDLE 까지 내리면 바쁜 PC에서 정리가 끝나지 않을 수 있어 한 단계만 내린다.
    """
    try:
        ok = bool(_kernel32.SetPriorityClass(
            _kernel32.GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS))
    except OSError:
        return False
    if not ok:
        log.debug("프로세스 우선순위 조정 실패 (err=%d)", ctypes.get_last_error())
    return ok


def begin_background_io() -> bool:
    """현재 스레드를 백그라운드 모드로 전환한다.

    CPU 우선순위뿐 아니라 **디스크 I/O 우선순위까지** 낮아진다. 검사 프로그램이
    이미지를 쓰고 있을 때 정리 작업이 디스크를 선점하지 않게 하는 핵심이다.
    """
    return _set_thread_mode(THREAD_MODE_BACKGROUND_BEGIN)


def end_background_io() -> bool:
    """백그라운드 모드 해제. 반드시 시작한 그 스레드에서 호출해야 한다."""
    return _set_thread_mode(THREAD_MODE_BACKGROUND_END)


def _set_thread_mode(mode: int) -> bool:
    try:
        return bool(_kernel32.SetThreadPriority(_kernel32.GetCurrentThread(), mode))
    except OSError:
        return False


def trim_working_set() -> bool:
    """작업 세트를 줄여 유휴 시 실제 메모리 사용량을 낮춘다.

    한 번 크게 스캔하고 나면 파이썬이 잡아 둔 페이지가 그대로 남는다.
    필요해지면 다시 올라오므로 유휴 상태에서만 호출한다.
    """
    try:
        return bool(_kernel32.SetProcessWorkingSetSize(
            _kernel32.GetCurrentProcess(), SIZE_T(-1), SIZE_T(-1)))
    except OSError:
        return False


def memory_usage_mb() -> float:
    """현재 프로세스의 작업 세트 크기(MB). 측정과 로그용."""
    try:
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        ok = _psapi.GetProcessMemoryInfo(
            _kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
        return counters.WorkingSetSize / (1024 * 1024) if ok else 0.0
    except OSError:
        return 0.0
