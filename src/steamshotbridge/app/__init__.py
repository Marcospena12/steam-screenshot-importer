from __future__ import annotations

import argparse
import sys

from PySide6.QtCore import QPoint, QRectF, QTimer, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QSplashScreen

from .log import setup_logging
from .single_instance import SingleInstance
from .theme import ThemeManager, ThemeMode, ThemeTokens
from .widgets import create_app_logo_pixmap
from .window import MainWindow


def _create_splash_pixmap() -> QPixmap:
    width, height = 360, 240
    pixmap = QPixmap(width, height)
    pixmap.fill(QColor("#0F1115"))

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    # Logo central
    logo = create_app_logo_pixmap(size=80, dark_mode=True)
    painter.drawPixmap((width - 80) // 2, 40, logo)

    # Título
    painter.setPen(QColor("#FFFFFF"))
    font_title = QFont("Segoe UI Variable", 16, QFont.Weight.DemiBold)
    painter.setFont(font_title)
    painter.drawText(
        QRectF(0, 135, width, 30),
        Qt.AlignmentFlag.AlignCenter,
        "SteamShotBridge",
    )

    # Subtítulo
    painter.setPen(QColor("#9CA3AF"))
    font_sub = QFont("Segoe UI Variable", 10)
    painter.setFont(font_sub)
    painter.drawText(
        QRectF(0, 168, width, 24),
        Qt.AlignmentFlag.AlignCenter,
        "Sincronizador de screenshots",
    )

    painter.end()
    return pixmap


def run_app(argv: list[str] | None = None) -> int:
    args_list = argv if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(description="SteamShotBridge GUI")
    parser.add_argument("--minimized", action="store_true", help="Inicia minimizado na bandeja")
    parsed_args, _ = parser.parse_known_args(args_list)

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)

    single = SingleInstance()
    if single.is_already_running():
        return 0

    splash: QSplashScreen | None = None
    should_show = not parsed_args.minimized
    if should_show:
        splash_pixmap = _create_splash_pixmap()
        splash = QSplashScreen(splash_pixmap)
        splash.show()
        app.processEvents()

    window = MainWindow()
    window.single_instance = single
    single.show_requested.connect(window.tray._open_window)

    def on_ui_error(tipo: str, msg: str) -> None:
        window.add_log_entry(tipo, "Sistema", msg)

    setup_logging(ui_error_callback=on_ui_error)

    if should_show and not window.settings.start_minimized:
        if splash is not None:
            QTimer.singleShot(1200, lambda: (splash.finish(window), window.show()))
        else:
            window.show()
    elif splash is not None:
        splash.close()

    return app.exec()


__all__ = [
    "MainWindow",
    "SingleInstance",
    "ThemeManager",
    "ThemeMode",
    "ThemeTokens",
    "run_app",
]
