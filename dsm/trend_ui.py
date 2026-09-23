"""사용량 추이 창과 다중 PC 현황 창.

그래프는 tkinter Canvas 에 직접 그린다. matplotlib 을 쓰면 의존성이 수십 MB
늘어나는데, 선 하나와 기준선 두 개를 그리는 데 그만한 비용을 치를 이유가 없다.
검사기에 상주하는 프로그램이라 가벼운 쪽이 맞다.
"""

from __future__ import annotations

import threading
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

from . import APP_TITLE
from . import usage
from .audit import log
from .fleet import collect
from .scanner import human_bytes

PLOT_BG = "#fbfbfd"
AXIS_COLOR = "#9aa0a6"
LINE_COLOR = "#2d7bd4"
WARN_COLOR = "#e0a020"
CRITICAL_COLOR = "#d4342c"
FORECAST_COLOR = "#b0b6be"


class TrendWindow(tk.Toplevel):
    """드라이브 여유 공간 추이와 포화 시점 예측."""

    def __init__(self, master, host) -> None:
        from .settings_ui import apply_geometry, scale

        super().__init__(master)
        self.host = host
        self.engine = host.engine
        self._scale = scale
        self._samples: list[usage.UsageSample] = []
        self._cancelled = False

        self.title(f"{APP_TITLE} - 사용량 추이")
        apply_geometry(self, 860, 620, 720, 520)
        self.protocol("WM_DELETE_WINDOW", self._close)

        pad = scale(self, 12)
        top = ttk.Frame(self, padding=(pad, pad, pad, 0))
        top.pack(side="top", fill="x")

        ttk.Label(top, text="드라이브").pack(side="left")
        self._drive_var = tk.StringVar()
        drives = self.engine.settings.drives()
        self._drive_box = ttk.Combobox(top, textvariable=self._drive_var, state="readonly",
                                       values=drives, width=8)
        self._drive_box.pack(side="left", padx=(8, 0))
        self._drive_box.bind("<<ComboboxSelected>>", lambda _e: self._reload())
        if drives:
            self._drive_var.set(drives[0])

        ttk.Label(top, text="표시 기간").pack(side="left", padx=(20, 0))
        self._range_var = tk.StringVar(value="30일")
        range_box = ttk.Combobox(top, textvariable=self._range_var, state="readonly",
                                 values=["7일", "30일", "90일", "1년"], width=8)
        range_box.pack(side="left", padx=(8, 0))
        range_box.bind("<<ComboboxSelected>>", lambda _e: self._reload())

        ttk.Button(top, text="새로 고침", command=self._reload).pack(side="right")

        self._notice = tk.StringVar()
        self._notice_label = ttk.Label(self, textvariable=self._notice,
                                       foreground="#b06000", wraplength=scale(self, 800))
        self._notice_label.pack(side="top", anchor="w", padx=pad, pady=(6, 0))

        summary = ttk.LabelFrame(self, text="추세와 예측", padding=pad)
        summary.pack(side="bottom", fill="x", padx=pad, pady=(0, pad))
        self._summary_vars: dict[int, tk.StringVar] = {}
        for window_days in (7, 30):
            var = tk.StringVar(value="—")
            row = ttk.Frame(summary)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=f"최근 {window_days}일", width=10).pack(side="left")
            ttk.Label(row, textvariable=var).pack(side="left")
            self._summary_vars[window_days] = var

        self._canvas = tk.Canvas(self, background=PLOT_BG, highlightthickness=0)
        self._canvas.pack(side="top", fill="both", expand=True, padx=pad, pady=pad)
        self._canvas.bind("<Configure>", lambda _e: self._draw())

        self._reload()

    # --- 데이터 ------------------------------------------------------------

    def _range_days(self) -> int:
        return {"7일": 7, "30일": 30, "90일": 90, "1년": 365}.get(self._range_var.get(), 30)

    def _reload(self) -> None:
        drive = self._drive_var.get()
        if not drive:
            self._notice.set("관리 대상 폴더가 없어 표시할 드라이브가 없습니다.")
            self._samples = []
            self._draw()
            return

        days = self._range_days()
        settings = self.engine.settings

        def work():
            try:
                samples = usage.load(drive, days)
            except OSError:
                log.exception("사용량 기록을 읽지 못했습니다")
                samples = []
            if not self._cancelled:
                self.host.call(lambda: self._apply(samples, settings))

        threading.Thread(target=work, name="dsm-trend", daemon=True).start()

    def _apply(self, samples, settings) -> None:
        if not self.winfo_exists():
            return
        self._samples = samples

        if not settings.usage_tracking:
            self._notice.set(
                "사용량 기록이 꺼져 있습니다. 설정 > 일반에서 켜면 1시간마다 기록이 쌓이고, "
                "며칠 뒤부터 추세와 예측을 볼 수 있습니다."
            )
        elif not samples:
            self._notice.set("아직 쌓인 기록이 없습니다. 기록을 켠 뒤 몇 시간 지나면 표시됩니다.")
        else:
            self._notice.set("")

        for window_days, var in self._summary_vars.items():
            trend = usage.analyze(samples, window_days)
            var.set(trend.describe())

        self._draw()

    # --- 그리기 ------------------------------------------------------------

    def _draw(self) -> None:
        if not self.winfo_exists():
            return
        canvas = self._canvas
        canvas.delete("all")
        width = canvas.winfo_width()
        height = canvas.winfo_height()
        if width < 50 or height < 50:
            return

        s = self._scale
        left, right = s(self, 58), width - s(self, 16)
        top, bottom = s(self, 14), height - s(self, 30)
        if right <= left or bottom <= top:
            return

        settings = self.engine.settings
        warn = settings.warn_free_percent
        critical = settings.critical_free_percent

        def y_for(percent: float) -> float:
            percent = max(0.0, min(100.0, percent))
            return bottom - (percent / 100.0) * (bottom - top)

        # 축과 눈금
        canvas.create_line(left, top, left, bottom, fill=AXIS_COLOR)
        canvas.create_line(left, bottom, right, bottom, fill=AXIS_COLOR)
        for percent in (0, 25, 50, 75, 100):
            y = y_for(percent)
            canvas.create_line(left, y, right, y, fill="#ececf0")
            canvas.create_text(left - s(self, 6), y, text=f"{percent}%", anchor="e",
                               fill="#666666", font=("Segoe UI", 8))

        # 경고/위험 기준선
        for value, color, label in ((warn, WARN_COLOR, "경고"), (critical, CRITICAL_COLOR, "위험")):
            y = y_for(value)
            canvas.create_line(left, y, right, y, fill=color, dash=(4, 3))
            canvas.create_text(right, y - s(self, 7), text=f"{label} {value:.0f}%",
                               anchor="e", fill=color, font=("Segoe UI", 8))

        if len(self._samples) < 2:
            canvas.create_text((left + right) / 2, (top + bottom) / 2,
                               text="표시할 기록이 없습니다", fill="#888888",
                               font=("Segoe UI", 10))
            return

        first = self._samples[0].when
        last = self._samples[-1].when
        span = max(last - first, 1.0)

        def x_for(when: float) -> float:
            return left + (when - first) / span * (right - left)

        points = []
        for sample in self._samples:
            points.extend((x_for(sample.when), y_for(sample.free_percent)))
        canvas.create_line(*points, fill=LINE_COLOR, width=2, smooth=False)

        # 예측선: 30일 추세로 0%에 닿는 지점까지 점선
        trend = usage.analyze(self._samples, 30)
        if trend.usable and trend.days_until_full is not None:
            end_when = last + trend.days_until_full * 86400.0
            # 그래프 오른쪽 끝을 넘어가면 잘라서 그린다
            visible_end = min(end_when, last + span * 0.35)
            ratio = (visible_end - last) / max(end_when - last, 1.0)
            end_percent = self._samples[-1].free_percent * (1 - ratio)
            canvas.create_line(x_for(last), y_for(self._samples[-1].free_percent),
                               x_for(visible_end), y_for(end_percent),
                               fill=FORECAST_COLOR, width=2, dash=(5, 4))

        # X축 날짜 라벨
        for ratio in (0.0, 0.5, 1.0):
            when = first + span * ratio
            anchor = "w" if ratio == 0 else ("e" if ratio == 1 else "center")
            canvas.create_text(x_for(when), bottom + s(self, 14),
                               text=f"{datetime.fromtimestamp(when):%m-%d %H:%M}",
                               anchor=anchor, fill="#666666", font=("Segoe UI", 8))

        newest = self._samples[-1]
        canvas.create_text(left + s(self, 6), top + s(self, 6), anchor="nw",
                           text=f"현재 여유 {human_bytes(newest.free)} ({newest.free_percent:.1f}%)"
                                f"  ·  기록 {len(self._samples):,}개",
                           fill="#333333", font=("Segoe UI", 9, "bold"))

    def _close(self) -> None:
        self._cancelled = True
        self.host._trend_window = None
        self.destroy()


class FleetWindow(tk.Toplevel):
    """공유 폴더에 모인 여러 검사기의 현황."""

    def __init__(self, master, host) -> None:
        from .settings_ui import apply_geometry, scale

        super().__init__(master)
        self.host = host
        self.engine = host.engine
        self._cancelled = False

        self.title(f"{APP_TITLE} - 검사기 현황")
        apply_geometry(self, 860, 520, 720, 420)
        self.protocol("WM_DELETE_WINDOW", self._close)

        pad = scale(self, 12)
        bar = ttk.Frame(self, padding=(pad, pad, pad, 0))
        bar.pack(side="top", fill="x")
        self._notice = tk.StringVar(value="읽는 중입니다...")
        ttk.Label(bar, textvariable=self._notice, wraplength=scale(self, 640)).pack(side="left")
        ttk.Button(bar, text="새로 고침", command=self._reload).pack(side="right")

        columns = ("state", "free", "drives", "cycle", "reported")
        tree = ttk.Treeview(self, columns=columns, show="tree headings", selectmode="browse")
        tree.heading("#0", text="검사기")
        tree.heading("state", text="상태")
        tree.heading("free", text="최저 여유")
        tree.heading("drives", text="드라이브")
        tree.heading("cycle", text="최근 정리")
        tree.heading("reported", text="마지막 보고")
        tree.column("#0", width=scale(self, 150), anchor="w")
        tree.column("state", width=scale(self, 90), anchor="center")
        tree.column("free", width=scale(self, 80), anchor="center")
        tree.column("drives", width=scale(self, 200), anchor="w")
        tree.column("cycle", width=scale(self, 180), anchor="w")
        tree.column("reported", width=scale(self, 110), anchor="center")
        tree.pack(side="top", fill="both", expand=True, padx=pad, pady=pad)
        tree.tag_configure("stale", foreground="#999999")
        tree.tag_configure("critical", foreground="#c62828")
        tree.tag_configure("warn", foreground="#b06000")
        self._tree = tree

        self._reload()

    def _reload(self) -> None:
        share = self.engine.settings.fleet_share_path

        def work():
            entries, error = collect(share)
            if not self._cancelled:
                self.host.call(lambda: self._apply(entries, error))

        threading.Thread(target=work, name="dsm-fleet", daemon=True).start()

    def _apply(self, entries, error) -> None:
        if not self.winfo_exists():
            return
        self._tree.delete(*self._tree.get_children())

        if error:
            self._notice.set(error)
            return

        stale_count = sum(1 for e in entries if e.stale)
        text = f"검사기 {len(entries)}대"
        if stale_count:
            text += f" · 연락 끊김 {stale_count}대"
        self._notice.set(text)

        for entry in entries:
            worst = entry.worst_free_percent
            drives = "  ".join(
                f"{d.get('drive', '?')} {d.get('free_percent', 0):.0f}%" for d in entry.drives
            )
            reported = (datetime.fromtimestamp(entry.reported_at).strftime("%m-%d %H:%M")
                        if entry.reported_at else "—")
            if entry.stale:
                state = "연락 끊김"
                tag = "stale"
            else:
                state = {"idle": "대기", "running": "정리 중", "paused": "일시정지"}.get(
                    entry.state, entry.state or "—")
                if entry.dry_run:
                    state += " (모의)"
                tag = entry.level if entry.level in ("critical", "warn") else ""

            self._tree.insert(
                "", "end", text=entry.host,
                values=(state,
                        f"{worst:.0f}%" if worst is not None else "—",
                        drives or entry.error,
                        entry.last_cycle or "—",
                        reported),
                tags=(tag,) if tag else (),
            )

    def _close(self) -> None:
        self._cancelled = True
        self.host._fleet_window = None
        self.destroy()
