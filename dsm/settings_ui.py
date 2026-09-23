"""설정창 / 상태창 / 미리보기 창.

tkinter는 스레드 안전하지 않다. 그래서 전용 UI 스레드가 숨은 루트 창 하나를
계속 소유하고, 트레이 쪽에서는 큐로 작업만 넘긴다. 창을 열고 닫을 때마다
Tk 루트를 새로 만들면 두 번째부터 불안정해지기 때문이다.
"""

from __future__ import annotations

import copy
import queue
import threading
import tkinter as tk
from dataclasses import replace
from datetime import datetime
from tkinter import filedialog, font as tkfont, messagebox, ttk

from . import APP_TITLE
from .audit import log, read_recent_alerts
from .autostart import is_enabled as autostart_is_enabled
from .autostart import set_enabled as autostart_set_enabled
from .config import (
    ACTION_CHOICES,
    DATE_BASIS_CHOICES,
    MIN_RETENTION_DAYS,
    FolderProfile,
    Settings,
    validate_folder_path,
)
from .engine import Engine
from .paths import audit_dir, data_dir, log_dir, open_in_explorer, resource_root
from .scanner import human_bytes, preview

PATTERN_SEPARATOR = "; "


def _split_patterns(text: str) -> list[str]:
    parts = text.replace(",", ";").split(";")
    return [p.strip() for p in parts if p.strip()]


def _join_patterns(patterns: list[str]) -> str:
    return PATTERN_SEPARATOR.join(patterns)


def scale(widget, value: int) -> int:
    """화면 DPI 배율에 맞춰 픽셀 값을 키운다.

    tkinter 는 글꼴은 DPI 에 맞춰 키우면서 코드에 적힌 픽셀 값은 그대로 쓴다.
    200% 배율 화면에서 창 크기와 열 너비를 숫자 그대로 두면 내용이 잘린다.
    """
    try:
        factor = widget.winfo_fpixels("1i") / 96.0
    except tk.TclError:
        factor = 1.0
    return round(value * max(1.0, factor))


def apply_geometry(window, width: int, height: int, min_width: int, min_height: int) -> None:
    """DPI 배율을 반영해 창 크기와 최소 크기를 정한다. 화면은 넘지 않는다."""
    max_width = window.winfo_screenwidth() - 60
    max_height = window.winfo_screenheight() - 120
    width = min(scale(window, width), max_width)
    height = min(scale(window, height), max_height)
    window.geometry(f"{width}x{height}")
    window.minsize(min(scale(window, min_width), max_width),
                   min(scale(window, min_height), max_height))


class ScrollArea(ttk.Frame):
    """내용이 창보다 길 때만 세로 스크롤바가 나타나는 영역.

    검사기 PC는 해상도와 DPI 배율이 제각각이다. 200% 배율에서는 같은 창이
    두 배 가까이 커지기 때문에, 고정 크기로 두면 아래쪽 버튼이 화면 밖으로
    밀려난다. 내용은 여기에 담고 버튼은 창 하단에 고정해 항상 보이게 한다.
    """

    def __init__(self, master) -> None:
        super().__init__(master)
        background = ttk.Style().lookup("TFrame", "background") or "SystemButtonFace"

        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0, background=background)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)

        self.body = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")

        self.body.bind("<Configure>", self._on_body_resize)
        self.canvas.bind("<Configure>", self._on_canvas_resize)

    def _on_body_resize(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._sync_scrollbar()

    def _on_canvas_resize(self, event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)
        self._sync_scrollbar()

    def _sync_scrollbar(self) -> None:
        needed = self.body.winfo_reqheight() > self.canvas.winfo_height()
        mapped = bool(self.scrollbar.winfo_ismapped())
        if needed and not mapped:
            self.scrollbar.pack(side="right", fill="y")
        elif not needed and mapped:
            self.scrollbar.pack_forget()

    def on_mousewheel(self, event) -> None:
        if self.scrollbar.winfo_ismapped():
            self.canvas.yview_scroll(-int(event.delta / 120), "units")


class UiHost:
    """UI 스레드와 창 수명을 관리한다."""

    POLL_ACTIVE_MS = 100
    """창이 열려 있을 때의 작업 큐 확인 간격."""

    POLL_IDLE_MS = 600
    """트레이에만 떠 있을 때의 간격."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._queue: queue.Queue = queue.Queue()
        self._root: tk.Tk | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._settings_window: SettingsWindow | None = None
        self._status_window: StatusWindow | None = None
        self._trend_window = None
        self._fleet_window = None
        self._icon_image = None

    # --- 수명주기 ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="dsm-ui", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)

    def _run(self) -> None:
        root = tk.Tk()
        root.withdraw()
        root.title(APP_TITLE)

        self._apply_window_icon(root)

        style = ttk.Style(root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass

        # ttk 의 표 행 높이는 글꼴과 무관하게 20px 로 고정돼 있다.
        # DPI 배율이 높은 PC에서는 글자가 위아래로 잘리므로 직접 맞춰 준다.
        try:
            line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
            style.configure("Treeview", rowheight=line + scale(root, 8))
        except tk.TclError:
            pass

        self._root = root
        self._ready.set()
        self._pump()
        root.mainloop()

    def _apply_window_icon(self, root) -> None:
        """모든 창의 제목 표시줄 아이콘을 DSM 아이콘으로 바꾼다.

        루트에 default 로 걸어 두면 이후 만들어지는 Toplevel 이 모두 물려받는다.
        실패해도 Tk 기본 아이콘으로 뜰 뿐이므로 조용히 넘어간다.
        """
        try:
            root.iconbitmap(default=str(resource_root() / "assets" / "app_icon.ico"))
            return
        except tk.TclError:
            pass

        try:
            # PhotoImage 는 참조가 사라지면 아이콘도 사라지므로 붙잡아 둔다
            self._icon_image = tk.PhotoImage(
                file=str(resource_root() / "assets" / "tray_icon.png"), master=root)
            root.iconphoto(True, self._icon_image)
        except tk.TclError:
            log.debug("창 아이콘을 적용하지 못했습니다", exc_info=True)

    def _pump(self) -> None:
        """다른 스레드가 넘긴 작업을 UI 스레드에서 실행한다.

        창이 하나도 없을 때는 느리게 돈다. 트레이에만 떠 있는 대부분의 시간
        동안 쓸데없이 깨어나지 않기 위해서다. 창을 여는 요청이 들어오면
        최대 0.6초 안에 처리되므로 체감 차이는 없다.
        """
        while True:
            try:
                task = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                task()
            except Exception:
                log.exception("UI 작업 실패")

        if self._root is None:
            return
        interval = self.POLL_ACTIVE_MS if self._has_open_window() else self.POLL_IDLE_MS
        self._root.after(interval, self._pump)

    def _has_open_window(self) -> bool:
        try:
            return any(child.winfo_exists() for child in self._root.winfo_children())
        except tk.TclError:
            return False

    def call(self, func) -> None:
        self._queue.put(func)

    def stop(self) -> None:
        if self._root is not None:
            self.call(self._root.quit)
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    # --- 창 열기 ----------------------------------------------------------

    def open_settings(self) -> None:
        self.call(self._open_settings)

    def _open_settings(self) -> None:
        if self._settings_window is not None and self._settings_window.winfo_exists():
            self._settings_window.lift()
            self._settings_window.focus_force()
            return
        self._settings_window = SettingsWindow(self._root, self)

    def open_status(self) -> None:
        self.call(self._open_status)

    def _open_status(self) -> None:
        if self._status_window is not None and self._status_window.winfo_exists():
            self._status_window.lift()
            self._status_window.focus_force()
            return
        self._status_window = StatusWindow(self._root, self)

    def open_preview(
        self,
        profiles: list[FolderProfile] | None = None,
        settings: Settings | None = None,
    ) -> None:
        """미리보기 창을 연다.

        설정창에서 열 때는 아직 저장하지 않은 편집본(settings)을 함께 넘긴다.
        그래야 "지금 화면의 설정대로라면 실제로 지워지는가"를 정확히 알려줄 수 있다.
        """
        self.call(lambda: PreviewWindow(self._root, self, profiles, settings))

    def open_trend(self) -> None:
        self.call(self._open_trend)

    def _open_trend(self) -> None:
        from .trend_ui import TrendWindow

        if self._trend_window is not None and self._trend_window.winfo_exists():
            self._trend_window.lift()
            self._trend_window.focus_force()
            return
        self._trend_window = TrendWindow(self._root, self)

    def open_fleet(self) -> None:
        self.call(self._open_fleet)

    def _open_fleet(self) -> None:
        from .trend_ui import FleetWindow

        if not (self.engine.settings.fleet_share_path or "").strip():
            messagebox.showinfo(
                APP_TITLE,
                "공유 폴더가 설정되지 않았습니다.\n\n"
                "설정 > 일반에서 여러 검사기가 함께 쓰는 폴더 경로를 넣으면,\n"
                "각 PC가 자기 상태를 그 폴더에 남기고 여기서 모아 볼 수 있습니다.",
            )
            return
        if self._fleet_window is not None and self._fleet_window.winfo_exists():
            self._fleet_window.lift()
            self._fleet_window.focus_force()
            return
        self._fleet_window = FleetWindow(self._root, self)

    def notify_error(self, title: str, message: str) -> None:
        self.call(lambda: messagebox.showerror(title, message))


class StatusWindow(tk.Toplevel):
    """트레이 더블클릭으로 열리는 상태 대시보드."""

    def __init__(self, master, host: UiHost) -> None:
        super().__init__(master)
        self.host = host
        self.engine = host.engine
        self.title(f"{APP_TITLE} - 상태")
        apply_geometry(self, 620, 470, 560, 420)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        outer = ttk.Frame(self, padding=scale(self, 12))
        outer.pack(fill="both", expand=True)

        # 아래쪽 고정 영역부터 배치해 창이 작아져도 버튼이 남도록 한다
        buttons = ttk.Frame(outer)
        buttons.pack(side="bottom", fill="x", pady=(12, 0))
        ttk.Button(buttons, text="지금 정리 실행", command=self._run_now).pack(side="left")
        ttk.Button(buttons, text="정리 미리보기", command=self._preview).pack(side="left", padx=6)
        ttk.Button(buttons, text="사용량 추이", command=self.host.open_trend).pack(side="left")
        ttk.Button(buttons, text="설정...", command=self.host.open_settings).pack(side="left", padx=6)
        ttk.Button(buttons, text="삭제 이력 보기",
                   command=lambda: open_in_explorer(audit_dir())).pack(side="left", padx=6)
        ttk.Button(buttons, text="닫기", command=self.destroy).pack(side="right")

        self._last_var = tk.StringVar()
        ttk.Label(outer, textvariable=self._last_var).pack(side="bottom", anchor="w", pady=(10, 0))

        self._alert_var = tk.StringVar()
        self._alert_label = ttk.Label(outer, textvariable=self._alert_var,
                                      foreground="#b06000", wraplength=scale(self, 560))
        self._alert_label.pack(side="bottom", anchor="w", pady=(8, 0))

        self._state_var = tk.StringVar()
        ttk.Label(outer, textvariable=self._state_var, font=("Segoe UI", 11, "bold")).pack(anchor="w")

        self._dry_var = tk.StringVar()
        self._dry_label = ttk.Label(outer, textvariable=self._dry_var, foreground="#b06000")
        self._dry_label.pack(anchor="w", pady=(2, 8))

        disk_frame = ttk.LabelFrame(outer, text="드라이브 여유 공간", padding=10)
        disk_frame.pack(fill="x")
        self._disk_frame = disk_frame
        self._disk_widgets: list[tuple[ttk.Label, ttk.Progressbar]] = []

        profile_frame = ttk.LabelFrame(outer, text="관리 대상 폴더", padding=10)
        profile_frame.pack(fill="both", expand=True, pady=(10, 0))
        columns = ("path", "retention", "mode")
        self._tree = ttk.Treeview(profile_frame, columns=columns, show="tree headings", height=6)
        self._tree.heading("#0", text="프로파일")
        self._tree.heading("path", text="경로")
        self._tree.heading("retention", text="보관")
        self._tree.heading("mode", text="처리")
        self._tree.column("#0", width=scale(self, 130), anchor="w")
        self._tree.column("path", width=scale(self, 270), anchor="w")
        self._tree.column("retention", width=scale(self, 60), anchor="center")
        self._tree.column("mode", width=scale(self, 110), anchor="center")
        self._tree.pack(fill="both", expand=True)

        self._refresh()

    def _run_now(self) -> None:
        self.engine.request_run()

    def _preview(self) -> None:
        self.host.open_preview(None)

    def _refresh(self) -> None:
        if not self.winfo_exists():
            return
        # 워커는 유휴 시 2분에 한 번만 깨어난다. 이 창이 열려 있는 동안에는
        # 여기서 직접 갱신해 최신 여유 공간을 보여준다.
        self.engine.refresh_disks()
        status = self.engine.status()

        labels = {"idle": "대기 중", "running": "정리 작업 진행 중...", "paused": "일시정지"}
        text = labels.get(status.state, status.state)
        if status.state == "paused" and status.paused_until:
            text += f" ({datetime.fromtimestamp(status.paused_until):%m-%d %H:%M} 까지)"
        elif status.state == "idle" and status.schedule_enabled and not status.within_schedule:
            text += f" · 실행 시간대 {status.schedule_text} 밖이라 대기"
        elif status.state == "idle" and status.next_run_at:
            text += f" · 다음 검사 {datetime.fromtimestamp(status.next_run_at):%m-%d %H:%M}"
        if status.schedule_enabled and status.within_schedule:
            text += f" · 실행 시간대 {status.schedule_text}"
        self._state_var.set(text)

        recent = read_recent_alerts(limit=1)
        if recent:
            when, kind, message = recent[0]
            self._alert_var.set(f"최근 알림 {when} [{kind}] {message}")
        else:
            self._alert_var.set("")

        if status.dry_run:
            self._dry_var.set("전역 모의실행이 켜져 있습니다. 실제 파일은 삭제되지 않습니다.")
        else:
            self._dry_var.set("")

        # 드라이브 게이지는 개수가 바뀔 때만 다시 만든다
        if len(self._disk_widgets) != len(status.disks):
            for child in self._disk_frame.winfo_children():
                child.destroy()
            self._disk_widgets = []
            for _ in status.disks:
                label = ttk.Label(self._disk_frame, text="")
                label.pack(anchor="w")
                bar = ttk.Progressbar(self._disk_frame, maximum=100, length=scale(self, 560))
                bar.pack(fill="x", pady=(0, 8))
                self._disk_widgets.append((label, bar))
            if not status.disks:
                ttk.Label(self._disk_frame, text="관리 대상 폴더가 없습니다. 설정에서 추가하세요.").pack(anchor="w")

        for (label, bar), disk in zip(self._disk_widgets, status.disks):
            if disk.ok:
                label.config(
                    text=f"{disk.drive}  사용 {human_bytes(disk.used)} / 전체 {human_bytes(disk.total)}"
                         f"   ·   여유 {human_bytes(disk.free)} ({disk.free_percent:.1f}%)"
                )
                bar.config(value=100 - disk.free_percent)
            else:
                label.config(text=f"{disk.drive} 확인 불가")
                bar.config(value=0)

        self._tree.delete(*self._tree.get_children())
        for profile in self.engine.settings.profiles:
            dry = self.engine.settings.is_dry_run(profile)
            mode = "모의실행" if dry else ACTION_CHOICES.get(profile.action, profile.action)
            if not profile.enabled:
                mode = "사용 안 함"
            self._tree.insert(
                "", "end", text=profile.name,
                values=(profile.path, f"{profile.retention_days}일", mode),
            )

        cycle = status.last_cycle
        if cycle is None:
            self._last_var.set("아직 실행된 정리 작업이 없습니다.")
        else:
            when = datetime.fromtimestamp(cycle.finished_at)
            extra = f", 빈 폴더 {cycle.removed_dirs}개 정리" if cycle.removed_dirs else ""
            self._last_var.set(f"최근 작업 {when:%Y-%m-%d %H:%M} · {cycle.describe()}{extra}")

        self.after(3000, self._refresh)


class SettingsWindow(tk.Toplevel):
    """프로파일과 전역 설정 편집."""

    def __init__(self, master, host: UiHost) -> None:
        super().__init__(master)
        self.host = host
        self.engine = host.engine
        # 취소 시 되돌릴 수 있도록 사본을 편집한다
        self.draft: Settings = copy.deepcopy(self.engine.settings)

        self.title(f"{APP_TITLE} - 설정")
        apply_geometry(self, 800, 560, 700, 480)
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        # 버튼 바를 먼저 아래에 고정해야 창이 작아져도 저장/취소가 남는다
        buttons = ttk.Frame(self, padding=scale(self, 10))
        buttons.pack(side="bottom", fill="x")
        ttk.Button(buttons, text="이 설정으로 미리보기", command=self._preview).pack(side="left")
        ttk.Button(buttons, text="취소", command=self._cancel).pack(side="right")
        ttk.Button(buttons, text="저장", command=self._save).pack(side="right", padx=6)

        notebook = ttk.Notebook(self)
        notebook.pack(side="top", fill="both", expand=True, padx=10, pady=(10, 0))
        notebook.add(self._build_profiles_tab(notebook), text="  관리 폴더  ")
        notebook.add(self._build_general_tab(notebook), text="  일반  ")

        self._reload_tree()

    # --- 탭 구성 ----------------------------------------------------------

    def _build_profiles_tab(self, master) -> ttk.Frame:
        frame = ttk.Frame(master, padding=scale(self, 10))

        columns = ("path", "patterns", "retention", "emergency", "basis", "action", "dry")
        tree = ttk.Treeview(frame, columns=columns, show="tree headings", selectmode="browse")
        tree.heading("#0", text="사용 / 이름")
        tree.heading("path", text="경로")
        tree.heading("patterns", text="대상 패턴")
        tree.heading("retention", text="보관")
        tree.heading("emergency", text="긴급")
        tree.heading("basis", text="날짜 기준")
        tree.heading("action", text="처리")
        tree.heading("dry", text="모의")
        tree.column("#0", width=scale(self, 140), anchor="w")
        tree.column("path", width=scale(self, 170), anchor="w")
        tree.column("patterns", width=scale(self, 100), anchor="w")
        tree.column("retention", width=scale(self, 50), anchor="center")
        tree.column("emergency", width=scale(self, 50), anchor="center")
        tree.column("basis", width=scale(self, 80), anchor="center")
        tree.column("action", width=scale(self, 85), anchor="center")
        tree.column("dry", width=scale(self, 45), anchor="center")
        tree.pack(fill="both", expand=True)
        tree.bind("<Double-1>", lambda _event: self._edit_profile())
        self._tree = tree

        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=(8, 0))
        ttk.Button(bar, text="추가", command=self._add_profile).pack(side="left")
        ttk.Button(bar, text="편집", command=self._edit_profile).pack(side="left", padx=6)
        ttk.Button(bar, text="복제", command=self._duplicate_profile).pack(side="left")
        ttk.Button(bar, text="삭제", command=self._delete_profile).pack(side="left", padx=6)
        ttk.Button(bar, text="사용/해제", command=self._toggle_profile).pack(side="left")
        # 긴급 정리는 이 목록 순서대로 처리하므로 순서를 바꿀 수 있어야 한다
        ttk.Button(bar, text="▲ 위로", command=lambda: self._move_profile(-1)).pack(side="right")
        ttk.Button(bar, text="▼ 아래로", command=lambda: self._move_profile(1)).pack(side="right", padx=6)

        hint = ("같은 폴더라도 파일 종류별로 프로파일을 나누면 보관 기간을 다르게 줄 수 있습니다. "
                "예: 영상 7일 / 양품 이미지 30일 / 불량 이미지 180일\n"
                "용량이 부족해 긴급 정리가 발동하면 이 목록의 위에서부터 차례로 지웁니다. "
                "먼저 지워도 되는 것을 위로 올려 두세요.")
        ttk.Label(frame, text=hint, foreground="#555555",
                  wraplength=scale(self, 700)).pack(anchor="w", pady=(8, 0))
        return frame

    def _build_general_tab(self, master) -> ttk.Frame:
        frame = ttk.Frame(master, padding=scale(self, 14))
        row = 0

        self._interval_var = tk.IntVar(value=self.draft.scan_interval_minutes)
        ttk.Label(frame, text="검사 주기(분)").grid(row=row, column=0, sticky="w", pady=6)
        ttk.Spinbox(frame, from_=1, to=1440, textvariable=self._interval_var, width=10).grid(
            row=row, column=1, sticky="w")
        row += 1

        self._dry_var = tk.BooleanVar(value=self.draft.global_dry_run)
        ttk.Checkbutton(
            frame,
            text="전역 모의실행 (실제로 지우지 않고 대상만 기록) — 도입 검증 기간에는 켜 두세요",
            variable=self._dry_var,
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=6)
        row += 1

        self._run_on_start_var = tk.BooleanVar(value=self.draft.run_on_start)
        ttk.Checkbutton(frame, text="프로그램 시작 직후 한 번 정리 실행",
                        variable=self._run_on_start_var).grid(
            row=row, column=0, columnspan=3, sticky="w", pady=6)
        row += 1

        self._autostart_var = tk.BooleanVar(value=autostart_is_enabled())
        ttk.Checkbutton(frame, text="Windows 로그온 시 자동 시작",
                        variable=self._autostart_var).grid(
            row=row, column=0, columnspan=3, sticky="w", pady=6)
        row += 1

        ttk.Separator(frame, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="ew", pady=12)
        row += 1

        # --- 실행 시간대 -------------------------------------------------
        self._schedule_var = tk.BooleanVar(value=self.draft.schedule_enabled)
        ttk.Checkbutton(
            frame, text="정해진 시간대에만 정리 실행", variable=self._schedule_var,
            command=self._sync_schedule_state,
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=6)
        row += 1

        times = ttk.Frame(frame)
        times.grid(row=row, column=0, columnspan=3, sticky="w", pady=2)
        self._schedule_start_var = tk.StringVar(value=self.draft.schedule_start)
        self._schedule_end_var = tk.StringVar(value=self.draft.schedule_end)
        ttk.Label(times, text="        시작").pack(side="left")
        self._start_entry = ttk.Entry(times, textvariable=self._schedule_start_var, width=8)
        self._start_entry.pack(side="left", padx=(8, 0))
        ttk.Label(times, text="종료").pack(side="left", padx=(14, 0))
        self._end_entry = ttk.Entry(times, textvariable=self._schedule_end_var, width=8)
        self._end_entry.pack(side="left", padx=(8, 0))
        ttk.Label(times, text="(24시간 형식, 예: 02:00 ~ 05:00)",
                  foreground="#555555").pack(side="left", padx=(12, 0))
        row += 1

        ttk.Label(
            frame,
            text="검사가 도는 시간에 대량 삭제가 겹치지 않게 합니다. "
                 "22:00 ~ 05:00 처럼 자정을 넘겨도 됩니다. "
                 "단, 여유 공간이 위험 기준 밑으로 떨어지면 시간대와 상관없이 즉시 정리합니다.",
            foreground="#555555", wraplength=scale(self, 680),
        ).grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1

        ttk.Separator(frame, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="ew", pady=12)
        row += 1

        # --- 용량 안전망 -------------------------------------------------
        self._guard_var = tk.BooleanVar(value=self.draft.capacity_guard)
        ttk.Checkbutton(
            frame, text="용량이 부족하면 보관 기간보다 먼저 정리 (긴급 정리)",
            variable=self._guard_var,
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=6)
        row += 1

        self._warn_var = tk.DoubleVar(value=self.draft.warn_free_percent)
        ttk.Label(frame, text="여유 공간 경고 기준(%)").grid(row=row, column=0, sticky="w", pady=6)
        ttk.Spinbox(frame, from_=1, to=99, increment=1, textvariable=self._warn_var, width=10).grid(
            row=row, column=1, sticky="w")
        row += 1

        self._critical_var = tk.DoubleVar(value=self.draft.critical_free_percent)
        ttk.Label(frame, text="여유 공간 위험 기준(%)").grid(row=row, column=0, sticky="w", pady=6)
        ttk.Spinbox(frame, from_=1, to=99, increment=1, textvariable=self._critical_var, width=10).grid(
            row=row, column=1, sticky="w")
        row += 1

        ttk.Label(
            frame,
            text="여유 공간이 위험 기준 밑으로 떨어지면, 관리 폴더 목록 순서대로 "
                 "각 프로파일의 '긴급 시 최소 보관일'까지 지웁니다. "
                 "경고 기준만큼 확보되면 즉시 멈추고, 그래도 모자라면 더 지우지 않고 "
                 "알림만 기록합니다. 임계치는 트레이 아이콘 색상에도 함께 쓰입니다.",
            foreground="#555555", wraplength=scale(self, 680),
        ).grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1

        ttk.Separator(frame, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="ew", pady=12)
        row += 1

        # --- 사용량 추이 / 다중 PC ---------------------------------------
        self._usage_var = tk.BooleanVar(value=self.draft.usage_tracking)
        ttk.Checkbutton(
            frame, text="사용량 추이 기록 (1시간마다 여유 공간을 한 줄씩 저장)",
            variable=self._usage_var,
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=6)
        row += 1

        ttk.Label(
            frame,
            text="며칠 쌓이면 '언제쯤 드라이브가 찬다'를 예측할 수 있습니다. "
                 "하루 24줄, 1년 모아도 수백 KB 수준입니다. "
                 "그래프는 트레이 메뉴의 '사용량 추이'에서 봅니다.",
            foreground="#555555", wraplength=scale(self, 680),
        ).grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1

        self._usage_retention_var = tk.IntVar(value=self.draft.usage_retention_days)
        ttk.Label(frame, text="사용량 기록 보관(일)").grid(row=row, column=0, sticky="w", pady=6)
        ttk.Spinbox(frame, from_=1, to=3650, textvariable=self._usage_retention_var,
                    width=10).grid(row=row, column=1, sticky="w")
        row += 1

        self._share_var = tk.StringVar(value=self.draft.fleet_share_path)
        ttk.Label(frame, text="검사기 공유 폴더").grid(row=row, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=self._share_var).grid(
            row=row, column=1, columnspan=2, sticky="ew")
        row += 1

        ttk.Label(
            frame,
            text="비워 두면 이 기능을 쓰지 않습니다. 여러 검사기가 함께 접근하는 폴더를 "
                 "넣으면 각 PC가 자기 상태를 그 폴더에 남기고, 트레이 메뉴의 "
                 "'검사기 현황'에서 전체를 모아 볼 수 있습니다. "
                 "예: \\\\서버\\공유\\DSM",
            foreground="#555555", wraplength=scale(self, 680),
        ).grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1

        ttk.Separator(frame, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="ew", pady=12)
        row += 1

        self._log_retention_var = tk.IntVar(value=self.draft.log_retention_days)
        ttk.Label(frame, text="자체 로그 보관(일)").grid(row=row, column=0, sticky="w", pady=6)
        ttk.Spinbox(frame, from_=1, to=3650, textvariable=self._log_retention_var, width=10).grid(
            row=row, column=1, sticky="w")
        row += 1

        ttk.Label(frame, text=f"설정/로그 위치: {data_dir()}", foreground="#555555").grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(10, 4))
        row += 1

        ttk.Label(
            frame,
            text="아래 버튼은 해당 폴더를 탐색기로 엽니다. "
                 "삭제 이력은 무엇이 언제 어떤 기준으로 지워졌는지 남는 기록이고, "
                 "동작 로그는 프로그램이 제대로 돌았는지 확인할 때 봅니다.",
            foreground="#555555", wraplength=scale(self, 680),
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 6))
        row += 1

        links = ttk.Frame(frame)
        links.grid(row=row, column=0, columnspan=3, sticky="w")
        ttk.Button(links, text="설정 폴더 (config.json)",
                   command=lambda: open_in_explorer(data_dir())).pack(side="left")
        ttk.Button(links, text="삭제 이력 (CSV)",
                   command=lambda: open_in_explorer(audit_dir())).pack(side="left", padx=6)
        ttk.Button(links, text="동작 로그 (app.log)",
                   command=lambda: open_in_explorer(log_dir())).pack(side="left")

        self._sync_schedule_state()
        return frame

    def _sync_schedule_state(self) -> None:
        """시간대 제한을 쓰지 않으면 시각 입력칸을 비활성화한다."""
        state = "normal" if self._schedule_var.get() else "disabled"
        self._start_entry.config(state=state)
        self._end_entry.config(state=state)

    # --- 프로파일 목록 ----------------------------------------------------

    def _reload_tree(self) -> None:
        self._tree.delete(*self._tree.get_children())
        for index, profile in enumerate(self.draft.profiles):
            mark = "O" if profile.enabled else "-"
            self._tree.insert(
                "", "end", iid=str(index),
                text=f"{mark}  {profile.name}",
                values=(
                    profile.path,
                    _join_patterns(profile.include_patterns),
                    f"{profile.retention_days}일",
                    # 평소 보관과 같으면 긴급 정리 대상이 아니다
                    f"{profile.emergency_retention_days}일"
                    if profile.emergency_retention_days < profile.retention_days else "-",
                    DATE_BASIS_CHOICES.get(profile.date_basis, profile.date_basis),
                    ACTION_CHOICES.get(profile.action, profile.action),
                    "예" if profile.dry_run else "",
                ),
            )

    def _selected_index(self) -> int | None:
        selection = self._tree.selection()
        return int(selection[0]) if selection else None

    def _add_profile(self) -> None:
        dialog = ProfileDialog(self, FolderProfile())
        self.wait_window(dialog)
        if dialog.result is not None:
            self.draft.profiles.append(dialog.result)
            self._reload_tree()

    def _edit_profile(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        dialog = ProfileDialog(self, copy.deepcopy(self.draft.profiles[index]))
        self.wait_window(dialog)
        if dialog.result is not None:
            self.draft.profiles[index] = dialog.result
            self._reload_tree()

    def _duplicate_profile(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        source = self.draft.profiles[index]
        names = {p.name for p in self.draft.profiles}
        name = f"{source.name} 복사본"
        suffix = 2
        while name in names:
            name = f"{source.name} 복사본 {suffix}"
            suffix += 1
        self.draft.profiles.append(replace(copy.deepcopy(source), name=name))
        self._reload_tree()

    def _delete_profile(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        profile = self.draft.profiles[index]
        if messagebox.askyesno("프로파일 삭제", f"'{profile.name}' 프로파일을 목록에서 지울까요?\n"
                                           "(폴더의 파일은 삭제되지 않습니다)", parent=self):
            del self.draft.profiles[index]
            self._reload_tree()

    def _toggle_profile(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        self.draft.profiles[index].enabled = not self.draft.profiles[index].enabled
        self._reload_tree()

    def _move_profile(self, offset: int) -> None:
        """긴급 정리 처리 순서를 바꾼다."""
        index = self._selected_index()
        if index is None:
            return
        target = index + offset
        if not 0 <= target < len(self.draft.profiles):
            return
        profiles = self.draft.profiles
        profiles[index], profiles[target] = profiles[target], profiles[index]
        self._reload_tree()
        self._tree.selection_set(str(target))
        self._tree.focus(str(target))

    # --- 저장 / 취소 ------------------------------------------------------

    def _collect(self) -> Settings | None:
        try:
            self.draft.scan_interval_minutes = int(self._interval_var.get())
            self.draft.log_retention_days = int(self._log_retention_var.get())
            self.draft.usage_retention_days = int(self._usage_retention_var.get())
            self.draft.warn_free_percent = float(self._warn_var.get())
            self.draft.critical_free_percent = float(self._critical_var.get())
        except (tk.TclError, ValueError):
            messagebox.showerror("설정 오류", "숫자 항목에 잘못된 값이 있습니다.", parent=self)
            return None
        self.draft.global_dry_run = bool(self._dry_var.get())
        self.draft.run_on_start = bool(self._run_on_start_var.get())
        self.draft.start_with_windows = bool(self._autostart_var.get())
        self.draft.capacity_guard = bool(self._guard_var.get())
        self.draft.schedule_enabled = bool(self._schedule_var.get())
        self.draft.schedule_start = self._schedule_start_var.get().strip()
        self.draft.schedule_end = self._schedule_end_var.get().strip()
        self.draft.usage_tracking = bool(self._usage_var.get())
        self.draft.fleet_share_path = self._share_var.get().strip().strip(chr(34))

        errors = self.draft.validate()
        if errors:
            messagebox.showerror("설정 오류", "\n".join(errors[:10]), parent=self)
            return None
        return self.draft

    def _preview(self) -> None:
        settings = self._collect()
        if settings is None:
            return
        profiles = settings.enabled_profiles()
        if not profiles:
            messagebox.showinfo("미리보기", "사용 중인 프로파일이 없습니다.", parent=self)
            return
        self.host.open_preview(copy.deepcopy(profiles), copy.deepcopy(settings))

    def _save(self) -> None:
        settings = self._collect()
        if settings is None:
            return

        if not settings.global_dry_run:
            real = [p for p in settings.enabled_profiles() if not p.dry_run]
            if real:
                names = ", ".join(p.name for p in real)
                if not messagebox.askyesno(
                    "실제 삭제 확인",
                    f"다음 프로파일이 실제로 파일을 삭제합니다.\n\n{names}\n\n"
                    "먼저 미리보기로 대상을 확인하셨나요? 계속할까요?",
                    parent=self, icon="warning",
                ):
                    return

        ok, message = autostart_set_enabled(settings.start_with_windows)
        if not ok:
            messagebox.showwarning("자동 시작", message, parent=self)

        self.engine.apply_settings(settings)
        log.info("설정창에서 설정 저장")
        self.host._settings_window = None
        self.destroy()

    def _cancel(self) -> None:
        self.host._settings_window = None
        self.destroy()


class ProfileDialog(tk.Toplevel):
    """폴더 프로파일 하나를 편집하는 모달 창."""

    def __init__(self, master, profile: FolderProfile) -> None:
        super().__init__(master)
        self.profile = profile
        self.result: FolderProfile | None = None

        self.title("폴더 프로파일")
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        # 버튼 바를 먼저 아래쪽에 고정한다. 내용이 길어져도 확인/취소가
        # 화면 밖으로 밀려나지 않게 하기 위한 순서다.
        pad = scale(self, 14)
        buttons = ttk.Frame(self, padding=(pad, 0, pad, scale(self, 12)))
        buttons.pack(side="bottom", fill="x")

        area = ScrollArea(self)
        area.pack(side="top", fill="both", expand=True)

        frame = ttk.Frame(area.body, padding=(pad, pad, pad, scale(self, 6)))
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, pad=scale(self, 14))  # 긴 라벨이 입력칸에 붙지 않게
        frame.columnconfigure(1, weight=1)

        self._area = area
        self._content = frame
        self._buttons = buttons
        row = 0

        self._name_var = tk.StringVar(value=profile.name)
        ttk.Label(frame, text="이름").grid(row=row, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=self._name_var).grid(row=row, column=1, columnspan=2, sticky="ew")
        row += 1

        self._path_var = tk.StringVar(value=profile.path)
        ttk.Label(frame, text="대상 폴더").grid(row=row, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=self._path_var).grid(row=row, column=1, sticky="ew")
        ttk.Button(frame, text="찾아보기", command=self._browse).grid(row=row, column=2, padx=(6, 0))
        row += 1

        self._subfolders_var = tk.BooleanVar(value=profile.include_subfolders)
        ttk.Checkbutton(frame, text="하위 폴더 포함", variable=self._subfolders_var).grid(
            row=row, column=1, sticky="w", pady=4)
        row += 1

        self._include_var = tk.StringVar(value=_join_patterns(profile.include_patterns))
        ttk.Label(frame, text="대상 패턴").grid(row=row, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=self._include_var).grid(row=row, column=1, columnspan=2, sticky="ew")
        row += 1
        ttk.Label(frame, text="예: *.jpg; *.png; *.mp4   (세미콜론으로 구분)",
                  foreground="#555555").grid(row=row, column=1, columnspan=2, sticky="w")
        row += 1

        self._exclude_var = tk.StringVar(value=_join_patterns(profile.exclude_patterns))
        ttk.Label(frame, text="제외 패턴").grid(row=row, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=self._exclude_var).grid(row=row, column=1, columnspan=2, sticky="ew")
        row += 1
        ttk.Label(frame, text="파일명과 폴더명 모두에 적용됩니다. 예: NG  (NG 폴더 전체를 보존)",
                  foreground="#555555").grid(row=row, column=1, columnspan=2, sticky="w")
        row += 1

        ttk.Separator(frame, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="ew", pady=10)
        row += 1

        self._retention_var = tk.IntVar(value=profile.retention_days)
        ttk.Label(frame, text="보관 기간(일)").grid(row=row, column=0, sticky="w", pady=5)
        ttk.Spinbox(frame, from_=MIN_RETENTION_DAYS, to=3650,
                    textvariable=self._retention_var, width=10).grid(row=row, column=1, sticky="w")
        row += 1

        self._emergency_var = tk.IntVar(value=profile.emergency_retention_days)
        ttk.Label(frame, text="긴급 시 최소 보관(일)").grid(row=row, column=0, sticky="w", pady=5)
        ttk.Spinbox(frame, from_=MIN_RETENTION_DAYS, to=3650,
                    textvariable=self._emergency_var, width=10).grid(row=row, column=1, sticky="w")
        row += 1
        ttk.Label(
            frame,
            text="드라이브 여유 공간이 위험 수준일 때만 여기까지 지웁니다. "
                 "평소에는 절대 이 기간까지 내려가지 않습니다. "
                 "보관 기간과 같게 두면 긴급 정리를 하지 않습니다.",
            foreground="#555555", wraplength=scale(self, 420),
        ).grid(row=row, column=1, columnspan=2, sticky="w")
        row += 1

        self._basis_var = tk.StringVar(value=DATE_BASIS_CHOICES.get(profile.date_basis, "수정 시각"))
        ttk.Label(frame, text="날짜 기준").grid(row=row, column=0, sticky="w", pady=5)
        basis_box = ttk.Combobox(frame, textvariable=self._basis_var, state="readonly",
                                 values=list(DATE_BASIS_CHOICES.values()), width=18)
        basis_box.grid(row=row, column=1, sticky="w")
        basis_box.bind("<<ComboboxSelected>>", lambda _e: self._sync_format_state())
        row += 1

        self._format_var = tk.StringVar(value=profile.folder_date_format)
        ttk.Label(frame, text="폴더명 날짜 형식").grid(row=row, column=0, sticky="w", pady=5)
        self._format_entry = ttk.Entry(frame, textvariable=self._format_var, width=18)
        self._format_entry.grid(row=row, column=1, sticky="w")
        row += 1
        ttk.Label(frame,
                  text="파일을 복사/이동하면 수정 시각이 바뀝니다. 날짜별 폴더 구조라면 폴더명 기준이 가장 정확합니다.",
                  foreground="#555555", wraplength=scale(self, 420)).grid(
            row=row, column=1, columnspan=2, sticky="w")
        row += 1

        self._action_var = tk.StringVar(value=ACTION_CHOICES.get(profile.action, "영구 삭제"))
        ttk.Label(frame, text="처리 방식").grid(row=row, column=0, sticky="w", pady=5)
        ttk.Combobox(frame, textvariable=self._action_var, state="readonly",
                     values=list(ACTION_CHOICES.values()), width=18).grid(row=row, column=1, sticky="w")
        row += 1

        self._empty_var = tk.BooleanVar(value=profile.remove_empty_dirs)
        ttk.Checkbutton(frame, text="파일을 지운 뒤 빈 폴더도 정리",
                        variable=self._empty_var).grid(row=row, column=1, sticky="w", pady=4)
        row += 1

        self._dry_var = tk.BooleanVar(value=profile.dry_run)
        ttk.Checkbutton(frame, text="이 프로파일은 모의실행 (대상만 기록하고 삭제하지 않음)",
                        variable=self._dry_var).grid(row=row, column=1, columnspan=2, sticky="w", pady=4)
        row += 1

        ttk.Separator(buttons, orient="horizontal").pack(fill="x", pady=(0, 10))
        ttk.Button(buttons, text="취소", command=self._cancel).pack(side="right")
        ok_button = ttk.Button(buttons, text="확인", command=self._ok, default="active")
        ok_button.pack(side="right", padx=6)

        self._sync_format_state()
        self.bind("<Return>", lambda _event: self._ok())
        self.bind("<Escape>", lambda _event: self._cancel())
        self.bind("<MouseWheel>", area.on_mousewheel)
        self._fit_to_content(master)
        self.grab_set()
        ok_button.focus_set()

    def _fit_to_content(self, master) -> None:
        """내용에 맞춰 창 크기를 정하고 부모 가운데에 놓는다.

        고정 크기로 두면 DPI 배율이 높은 PC에서 아래쪽 버튼이 잘린다.
        창 자체의 요청 크기는 스크롤 영역 때문에 의미가 없으므로,
        내용 프레임과 버튼 바의 요청 크기를 직접 더해서 계산한다.
        """
        self.update_idletasks()

        scrollbar_allowance = self._area.scrollbar.winfo_reqwidth() + 4
        width = self._content.winfo_reqwidth() + scrollbar_allowance
        height = self._content.winfo_reqheight() + self._buttons.winfo_reqheight()

        # 화면을 벗어나지 않게 제한한다 (해상도가 낮은 검사기 PC 대비).
        # 넘치는 만큼은 스크롤로 볼 수 있고 버튼은 항상 하단에 남는다.
        max_width = self.winfo_screenwidth() - 60
        max_height = self.winfo_screenheight() - 120
        width = min(width, max_width)
        height = min(height, max_height)

        self.minsize(min(width, max_width), min(360, height))
        self.resizable(False, True)

        try:
            x = master.winfo_rootx() + (master.winfo_width() - width) // 2
            y = master.winfo_rooty() + (master.winfo_height() - height) // 3
        except tk.TclError:
            x = y = 100
        x = max(0, min(x, self.winfo_screenwidth() - width))
        y = max(0, min(y, self.winfo_screenheight() - height))
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _sync_format_state(self) -> None:
        is_foldername = self._basis_var.get() == DATE_BASIS_CHOICES["foldername"]
        self._format_entry.config(state="normal" if is_foldername else "disabled")

    def _browse(self) -> None:
        chosen = filedialog.askdirectory(parent=self, title="관리할 폴더 선택",
                                         initialdir=self._path_var.get() or "D:\\")
        if not chosen:
            return
        normalized = chosen.replace("/", "\\")
        ok, reason = validate_folder_path(normalized)
        if not ok:
            messagebox.showerror("사용할 수 없는 폴더", reason, parent=self)
            return
        self._path_var.set(normalized)

    def _reverse_lookup(self, table: dict[str, str], label: str, fallback: str) -> str:
        for key, value in table.items():
            if value == label:
                return key
        return fallback

    def _ok(self) -> None:
        profile = replace(
            self.profile,
            name=self._name_var.get().strip(),
            path=self._path_var.get().strip(),
            include_subfolders=bool(self._subfolders_var.get()),
            include_patterns=_split_patterns(self._include_var.get()),
            exclude_patterns=_split_patterns(self._exclude_var.get()),
            date_basis=self._reverse_lookup(DATE_BASIS_CHOICES, self._basis_var.get(), "mtime"),
            folder_date_format=self._format_var.get().strip() or "%Y%m%d",
            action=self._reverse_lookup(ACTION_CHOICES, self._action_var.get(), "delete"),
            remove_empty_dirs=bool(self._empty_var.get()),
            dry_run=bool(self._dry_var.get()),
        )
        try:
            profile.retention_days = int(self._retention_var.get())
            profile.emergency_retention_days = int(self._emergency_var.get())
        except (tk.TclError, ValueError):
            messagebox.showerror("입력 오류", "보관 기간은 숫자여야 합니다.", parent=self)
            return

        errors = profile.validate()
        if errors:
            messagebox.showerror("입력 오류", "\n".join(errors), parent=self)
            return

        self.result = profile
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


class PreviewWindow(tk.Toplevel):
    """보관 기간이 지난 파일을 집계해 보여준다. 이 창은 아무것도 삭제하지 않는다.

    중요한 건 단순히 "이 창이 안 지운다"가 아니라, **지금 설정으로 실제 정리를
    실행하면 이 파일들이 지워지는지 아닌지**다. 모의실행이 켜져 있으면 실제
    실행 때도 지워지지 않는데, 그 사실을 여기서 알려주지 않으면 사용자는
    곧 지워질 것으로 오해한다.
    """

    def __init__(
        self,
        master,
        host: UiHost,
        profiles: list[FolderProfile] | None,
        settings: Settings | None = None,
    ) -> None:
        super().__init__(master)
        self.host = host
        self.engine = host.engine
        self._settings = settings if settings is not None else self.engine.settings
        self._profiles = (
            profiles if profiles is not None else list(self._settings.enabled_profiles())
        )
        self._cancelled = False

        self.title(f"{APP_TITLE} - 정리 미리보기")
        apply_geometry(self, 820, 560, 700, 460)
        self.protocol("WM_DELETE_WINDOW", self._close)

        pad = scale(self, 12)
        buttons = ttk.Frame(self, padding=(pad, 0, pad, pad))
        buttons.pack(side="bottom", fill="x")
        ttk.Button(buttons, text="닫기", command=self._close).pack(side="right")

        frame = ttk.Frame(self, padding=pad)
        frame.pack(side="top", fill="both", expand=True)

        self._headline = tk.StringVar(value="집계 중입니다...")
        ttk.Label(frame, textvariable=self._headline, font=("Segoe UI", 11, "bold")).pack(anchor="w")

        self._verdict = tk.StringVar(value="")
        self._verdict_label = ttk.Label(frame, textvariable=self._verdict,
                                        wraplength=scale(self, 760))
        self._verdict_label.pack(anchor="w", pady=(4, 2))

        ttk.Label(frame, text="집계만 하는 창입니다. 이 창을 닫아도 파일은 그대로입니다.",
                  foreground="#666666").pack(anchor="w", pady=(0, 8))

        self._progress = ttk.Progressbar(frame, mode="indeterminate")
        self._progress.pack(fill="x")
        self._progress.start(12)

        self._text = tk.Text(frame, wrap="none", height=20, font=("Consolas", 9))
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self._text.yview)
        self._text.configure(yscrollcommand=scroll.set)
        self._text.pack(side="left", fill="both", expand=True, pady=(10, 0))
        scroll.pack(side="right", fill="y", pady=(10, 0))

        threading.Thread(target=self._work, name="dsm-preview", daemon=True).start()

    def _work(self) -> None:
        # 프로파일과 결과를 짝지어 둔다. 결과만으로는 실제 삭제 여부를 알 수 없다.
        pairs = []
        for profile in self._profiles:
            if self._cancelled:
                return
            try:
                pairs.append((profile, preview(profile, should_stop=lambda: self._cancelled)))
            except OSError as exc:
                log.warning("미리보기 실패 [%s]: %s", profile.name, exc)
        if not self._cancelled:
            self.host.call(lambda: self._render(pairs))

    def _mode_line(self, profile: FolderProfile) -> str:
        """이 프로파일이 실제 정리 때 무엇을 하는지 한 줄로."""
        if self._settings.is_dry_run(profile):
            reason = "전역 모의실행 켜짐" if self._settings.global_dry_run else "이 프로파일 모의실행 켜짐"
            return f"모의실행 ({reason}) — 실제 정리를 실행해도 삭제되지 않습니다"
        action = ACTION_CHOICES.get(profile.action, profile.action)
        return f"{action} — 실제 정리를 실행하면 삭제됩니다"

    def _render(self, pairs) -> None:
        if not self.winfo_exists():
            return
        self._progress.stop()
        self._progress.pack_forget()

        total_count = sum(result.count for _profile, result in pairs)
        total_bytes = sum(result.total_bytes for _profile, result in pairs)
        self._headline.set(f"보관 기간이 지난 파일 {total_count:,}개 · {human_bytes(total_bytes)}")
        self._set_verdict(pairs, total_count)

        lines: list[str] = []
        for profile, result in pairs:
            lines.append(f"[{result.profile_name}]  {result.path}")
            lines.append(f"   처리 방식  {self._mode_line(profile)}")
            lines.append(
                f"   대상 {result.count:,}개 / {human_bytes(result.total_bytes)}"
                f"   (보관 {profile.retention_days}일 기준,"
                f" 스캔 {result.stats.scanned_files:,}개, 제외 {result.stats.excluded:,}개,"
                f" 읽기 실패 {result.stats.unreadable:,}개, {result.elapsed:.1f}초)"
            )
            if result.stats.foldername_fallback:
                lines.append(
                    f"   주의: 폴더명에서 날짜를 못 읽어 수정 시각으로 판정한 파일 "
                    f"{result.stats.foldername_fallback:,}개"
                )
            if result.oldest is not None and result.newest is not None:
                lines.append(
                    f"   대상 파일 날짜  {datetime.fromtimestamp(result.oldest.ref_time):%Y-%m-%d}"
                    f" ~ {datetime.fromtimestamp(result.newest.ref_time):%Y-%m-%d}"
                    f"  ({DATE_BASIS_CHOICES.get(profile.date_basis, profile.date_basis)} 기준)"
                )
            if result.samples:
                lines.append("   예시    경과일       크기  경로")
                for candidate in result.samples[:10]:
                    lines.append(
                        f"        {candidate.age_days:9.1f}일 {human_bytes(candidate.size):>9}  {candidate.path}"
                    )
                if result.count > len(result.samples):
                    lines.append(f"        ... 외 {result.count - len(result.samples):,}개")
            for message in result.stats.errors[:5]:
                lines.append(f"   오류: {message}")
            lines.append("")

        if not pairs:
            lines.append("사용 중인 프로파일이 없습니다.")

        self._text.config(state="normal")
        self._text.delete("1.0", "end")
        self._text.insert("1.0", "\n".join(lines))
        self._text.config(state="disabled")

    def _set_verdict(self, pairs, total_count: int) -> None:
        """실제 정리를 돌리면 어떻게 되는지를 맨 위에 못 박아 준다."""
        if not pairs or total_count == 0:
            self._verdict.set("지금 기준으로 보관 기간이 지난 파일이 없습니다.")
            self._verdict_label.config(foreground="#2e7d32")
            return

        deleting = [(p, r) for p, r in pairs if not self._settings.is_dry_run(p) and r.count]
        simulated = [(p, r) for p, r in pairs if self._settings.is_dry_run(p) and r.count]

        if not deleting:
            self._verdict.set(
                "모의실행 상태입니다. 실제 정리를 실행해도 이 파일들은 삭제되지 않고 "
                "기록만 남습니다. 실제로 지우려면 설정에서 모의실행을 끄세요."
            )
            self._verdict_label.config(foreground="#b06000")
            return

        count = sum(r.count for _p, r in deleting)
        size = human_bytes(sum(r.total_bytes for _p, r in deleting))
        text = f"실제 정리를 실행하면 {count:,}개 / {size} 가 삭제됩니다."
        if simulated:
            names = ", ".join(p.name for p, _r in simulated)
            text += f"  (모의실행이라 삭제되지 않는 프로파일: {names})"
        self._verdict.set(text)
        self._verdict_label.config(foreground="#c62828")

    def _close(self) -> None:
        self._cancelled = True
        self.destroy()
