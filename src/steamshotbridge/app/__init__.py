from __future__ import annotations

import argparse
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from .log import setup_logging
from .single_instance import SingleInstance
from .theme import ThemeManager, ThemeMode, ThemeTokens
from .window import MainWindow


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

    window = MainWindow()
    window.single_instance = single
    single.show_requested.connect(window.tray._open_window)

    def on_ui_error(tipo: str, msg: str) -> None:
        window.add_log_entry(tipo, "Sistema", msg)

    setup_logging(ui_error_callback=on_ui_error)

    if not parsed_args.minimized and not window.settings.start_minimized:
        window.show()

    return app.exec()


__all__ = [
    "MainWindow",
    "SingleInstance",
    "ThemeManager",
    "ThemeMode",
    "ThemeTokens",
    "run_app",
]
