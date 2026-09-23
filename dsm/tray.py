"""시스템 트레이 아이콘과 메뉴.

트레이는 엔진 상태를 비추는 얇은 층이다. 여기서 직접 파일을 건드리는 일은 없다.

아이콘은 assets/tray_icon.png 를 바탕으로 쓰고, 오른쪽 아래에 상태 색 배지를
합성한다. 배지가 있어야 아이콘 하나로 "지금 어떤 상태인지"를 알 수 있다.
합성 결과는 상태별로 캐시해 두므로 상태가 바뀔 때만 이미지를 만든다.
"""

from __future__ import annotations

import pystray
from PIL import Image, ImageDraw
from pystray import Menu, MenuItem

from . import APP_TITLE
from .audit import log
from .engine import Engine
from .paths import audit_dir, open_in_explorer, resource_root
from .settings_ui import UiHost

ICON_SIZE = 64

LEVEL_COLORS = {
    "ok": (46, 158, 79),        # 정상
    "warn": (224, 160, 32),     # 경고 - 여유 공간 부족 또는 삭제 실패 발생
    "critical": (212, 52, 44),  # 위험
    "busy": (45, 123, 212),     # 정리 작업 중
    "paused": (128, 128, 128),  # 일시정지
}

BASE_NAVY = (29, 35, 46, 255)
"""아이콘 배경색. 에셋이 없을 때 그리는 대체 아이콘과 배지 테두리에 쓴다."""

_base_image: Image.Image | None = None
_icon_cache: dict[str, Image.Image] = {}


def _load_base_image() -> Image.Image:
    """아이콘 원본을 한 번만 읽어 둔다."""
    global _base_image
    if _base_image is not None:
        return _base_image

    path = resource_root() / "assets" / "tray_icon.png"
    try:
        with Image.open(path) as source:
            _base_image = source.convert("RGBA").resize((ICON_SIZE, ICON_SIZE), Image.LANCZOS)
    except (OSError, ValueError):
        # 에셋이 없어도 트레이는 떠야 한다
        log.warning("아이콘 파일을 읽지 못해 기본 도형으로 대체합니다: %s", path)
        _base_image = _fallback_image()
    return _base_image


def _fallback_image() -> Image.Image:
    """에셋이 없을 때 쓰는 단순한 원형 아이콘."""
    image = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse([0, 0, ICON_SIZE - 1, ICON_SIZE - 1], fill=BASE_NAVY)
    draw.ellipse([16, 20, 48, 32], fill=(255, 255, 255, 255))
    draw.rectangle([16, 26, 48, 40], fill=(255, 255, 255, 255))
    draw.ellipse([16, 34, 48, 46], fill=(226, 232, 240, 255))
    return image


def make_icon_image(level: str) -> Image.Image:
    """상태 배지를 합성한 트레이 아이콘. 상태별로 캐시한다."""
    cached = _icon_cache.get(level)
    if cached is not None:
        return cached

    image = _load_base_image().copy()
    color = LEVEL_COLORS.get(level, LEVEL_COLORS["ok"])

    # 배지는 4배 크기로 그린 뒤 줄여서 가장자리를 매끄럽게 만든다
    scale = 4
    badge_size = round(ICON_SIZE * 0.34)
    layer = Image.new("RGBA", (badge_size * scale, badge_size * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    edge = badge_size * scale - 1
    draw.ellipse([0, 0, edge, edge], fill=BASE_NAVY)  # 어두운 테두리로 배경과 분리
    inset = round(badge_size * scale * 0.17)
    draw.ellipse([inset, inset, edge - inset, edge - inset], fill=color + (255,))
    layer = layer.resize((badge_size, badge_size), Image.LANCZOS)

    # 원의 테두리(우하단 45도) 위에 얹는다. 로고의 빗자루를 가리지 않으면서
    # 모서리의 빈 공간을 활용하는 위치다.
    offset = ICON_SIZE - badge_size
    image.alpha_composite(layer, (offset, offset))

    _icon_cache[level] = image
    return image


class TrayApp:
    """트레이 아이콘 수명과 메뉴 동작."""

    def __init__(self, engine: Engine, host: UiHost) -> None:
        self.engine = engine
        self.host = host
        self._level = ""
        self._title = ""
        self.icon = pystray.Icon(
            "DiskSpaceManager",
            icon=make_icon_image("ok"),
            title=APP_TITLE,
            menu=self._build_menu(),
        )

    # --- 메뉴 -------------------------------------------------------------

    def _build_menu(self) -> Menu:
        return Menu(
            MenuItem("지금 정리 실행", self._on_run_now),
            MenuItem("정리 미리보기", self._on_preview),
            Menu.SEPARATOR,
            MenuItem("상태 보기", self._on_status, default=True),
            MenuItem("사용량 추이", self._on_trend),
            MenuItem("검사기 현황", self._on_fleet),
            MenuItem("설정...", self._on_settings),
            MenuItem("삭제 이력 폴더 열기", self._on_open_logs),
            Menu.SEPARATOR,
            MenuItem(
                "일시정지",
                Menu(
                    MenuItem("1시간", lambda: self.engine.pause_for(60)),
                    MenuItem("4시간", lambda: self.engine.pause_for(240)),
                    MenuItem("오늘 하루", lambda: self.engine.pause_until_tomorrow()),
                    Menu.SEPARATOR,
                    MenuItem(
                        "일시정지 해제",
                        lambda: self.engine.resume(),
                        enabled=lambda _item: self.engine.is_paused,
                    ),
                ),
            ),
            Menu.SEPARATOR,
            MenuItem("종료", self._on_exit),
        )

    def _on_run_now(self) -> None:
        if self.engine.is_running:
            self.notify(APP_TITLE, "이미 정리 작업이 진행 중입니다.")
            return
        log.info("트레이: 지금 정리 실행")
        self.engine.request_run()

    def _on_preview(self) -> None:
        self.host.open_preview(None)

    def _on_status(self) -> None:
        self.host.open_status()

    def _on_trend(self) -> None:
        self.host.open_trend()

    def _on_fleet(self) -> None:
        self.host.open_fleet()

    def _on_settings(self) -> None:
        self.host.open_settings()

    def _on_open_logs(self) -> None:
        open_in_explorer(audit_dir())

    def _on_exit(self) -> None:
        log.info("트레이: 종료 요청")
        self.icon.stop()

    # --- 상태 반영 --------------------------------------------------------

    def sync(self) -> None:
        """엔진 상태를 아이콘 색과 툴팁에 반영한다.

        실제로 바뀐 것만 건드린다. 트레이 아이콘 갱신은 Shell_NotifyIcon 호출이라
        의미 없이 반복하면 그만큼 낭비다.
        """
        try:
            status = self.engine.status()
            if status.level != self._level:
                self._level = status.level
                self.icon.icon = make_icon_image(status.level)
            title = status.tooltip()
            if title != self._title:
                self._title = title
                self.icon.title = title
        except Exception:
            log.exception("트레이 갱신 실패")

    def notify(self, title: str, message: str) -> None:
        try:
            self.icon.notify(message, title)
        except Exception:
            # 알림은 부가 기능이므로 실패해도 무시한다
            log.debug("트레이 알림 표시 실패", exc_info=True)

    def run(self) -> None:
        """메인 스레드를 점유하는 트레이 루프."""
        self.engine.on_change.append(self.sync)
        self.engine.on_notify.append(self.notify)
        self.sync()
        self.icon.run()
