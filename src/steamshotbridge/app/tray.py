from __future__ import annotations

import os
from pathlib import Path
import subprocess

from PySide6.QtCore import QObject
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from .widgets import create_tray_icon


class TrayManager(QObject):
    def __init__(self, window, parent: QObject | None = None) -> None:
        super().__init__(parent or window)
        self.window = window
        self.tray_icon = QSystemTrayIcon(window)

        self._state = "active"
        self._steam_running = False

        self._build_menu()
        self.tray_icon.activated.connect(self._on_activated)
        self.update_icon()
        self.tray_icon.show()

    def _build_menu(self) -> None:
        self.menu = QMenu(self.window)

        self.action_open = QAction("Abrir janela", self)
        self.action_open.triggered.connect(self._open_window)

        self.action_toggle_pause = QAction("Pausar", self)
        self.action_toggle_pause.triggered.connect(self._toggle_pause)

        self.action_apply_pending = QAction("Aplicar pendências agora", self)
        self.action_apply_pending.triggered.connect(self._apply_pending)

        self.action_open_steam_folder = QAction("Abrir pasta de screenshots da Steam", self)
        self.action_open_steam_folder.triggered.connect(self._open_steam_folder)

        self.action_quit = QAction("Sair", self)
        self.action_quit.triggered.connect(self._quit_app)

        self.menu.addAction(self.action_open)
        self.menu.addSeparator()
        self.menu.addAction(self.action_toggle_pause)
        self.menu.addAction(self.action_apply_pending)
        self.menu.addAction(self.action_open_steam_folder)
        self.menu.addSeparator()
        self.menu.addAction(self.action_quit)

        self.tray_icon.setContextMenu(self.menu)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.DoubleClick,
            QSystemTrayIcon.ActivationReason.Trigger,
        ):
            self._open_window()

    def _open_window(self) -> None:
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()

    def _toggle_pause(self) -> None:
        if hasattr(self.window, "worker") and self.window.worker is not None:
            if self.window.worker.is_paused:
                self.window.worker.resume()
            else:
                self.window.worker.pause()

    def _apply_pending(self) -> None:
        if hasattr(self.window, "_on_apply_pending_clicked"):
            self.window._on_apply_pending_clicked()

    def _open_steam_folder(self) -> None:
        target = None
        if getattr(self.window, "steam_userdata_dir", None):
            remote = Path(self.window.steam_userdata_dir) / "760" / "remote"
            target = remote if remote.is_dir() else Path(self.window.steam_userdata_dir)

        if target is not None and target.is_dir() and os.name == "nt":
            try:
                os.startfile(str(target))
            except Exception:
                subprocess.Popen(["explorer", str(target)])

    def _quit_app(self) -> None:
        self.window.force_quit()

    def set_state(self, state: str) -> None:
        self._state = state
        self.action_toggle_pause.setText("Retomar" if state == "paused" else "Pausar")
        self.update_icon()

    def set_steam_running(self, running: bool) -> None:
        self._steam_running = running
        self.action_apply_pending.setEnabled(not running)

    def update_icon(self) -> None:
        is_dark = True
        if hasattr(self.window, "theme_manager"):
            is_dark = self.window.theme_manager.is_dark
        icon = create_tray_icon(status=self._state, dark_mode=is_dark)
        self.tray_icon.setIcon(icon)
        self.window.setWindowIcon(icon)

    def notify(self, title: str, message: str, is_error: bool = False) -> None:
        if not getattr(self.window, "settings", None) or self.window.settings.notifications:
            icon = (
                QSystemTrayIcon.MessageIcon.Critical
                if is_error
                else QSystemTrayIcon.MessageIcon.Information
            )
            self.tray_icon.showMessage(title, message, icon, 3000)

    def notify_imported(self, game_name: str) -> None:
        self.notify("SteamShotBridge", f"Screenshot importada para {game_name}")

    def notify_pending(self, game_name: str) -> None:
        self.notify("SteamShotBridge", f"Screenshot na fila (Steam aberta): {game_name}")

    def notify_applied(self, count: int) -> None:
        self.notify("SteamShotBridge", f"{count} pendência(s) aplicada(s) na Steam")

    def notify_error(self, message: str) -> None:
        self.notify("SteamShotBridge - Erro", message, is_error=True)
