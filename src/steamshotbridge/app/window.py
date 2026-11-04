from __future__ import annotations

from datetime import datetime
import json
import logging
import os
from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..exe_map import (
    default_config_path,
    is_manual_game_name,
    is_shortcut_applied,
    list_exe_map,
    load_exe_map,
    rename_game,
    resolve_exe,
    update_exe_mapping,
)
from ..games import Game, GameValidationError, list_games
from ..removal import (
    can_remove_game,
    inspect_removal,
    is_app_managed_game,
    remove_game,
    restore_game,
)
from ..name_resolver import resolve_game_name
from ..pending import (
    apply_pending,
    default_pending_path,
    load_pending,
)
from ..steam import (
    find_steam_installation,
    is_steam_running,
    resolve_userdata_id,
    screenshot_files,
    userdata_path,
)
from ..steam_names import SteamNameManager
from ..watcher import Event
from . import autostart
from .covers import CoverManager
from .settings import Settings, load_settings, save_settings
from .theme import ThemeManager, ThemeMode, ThemeTokens
from .tray import TrayManager
from .widgets import (
    EmptyState,
    GameCard,
    GameDetailsDialog,
    MetricCard,
    Sidebar,
    ToggleSwitch,
    create_tray_icon,
)
from .worker import WatcherWorker


LOGGER = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(
        self,
        config_path: Path | str | None = None,
        steam_userdata_dir: Path | str | None = None,
        pending_path: Path | str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("SteamShotBridge")
        self.setMinimumSize(960, 640)
        self.resize(1020, 680)

        self.config_path = (
            Path(config_path) if config_path is not None else default_config_path()
        )
        self.pending_path = (
            Path(pending_path) if pending_path is not None else default_pending_path()
        )

        self.settings = load_settings(self.config_path)

        if steam_userdata_dir is not None:
            self.steam_userdata_dir = Path(steam_userdata_dir)
        elif self.settings.steam_userdata_id:
            try:
                inst = find_steam_installation()
                self.steam_userdata_dir = userdata_path(inst.path, self.settings.steam_userdata_id)
            except Exception:
                self.steam_userdata_dir = None
        else:
            try:
                inst = find_steam_installation()
                uid, _ = resolve_userdata_id(inst.path)
                self.steam_userdata_dir = userdata_path(inst.path, uid)
            except Exception:
                self.steam_userdata_dir = None

        self.theme_manager = ThemeManager(self)
        self.theme_manager.theme_changed.connect(self._on_theme_changed)

        self.cover_manager = CoverManager(
            steam_userdata_dir=self.steam_userdata_dir,
            parent=self,
        )
        self.cover_manager.cover_loaded.connect(self._on_cover_loaded)
        self._game_cards: dict[int, GameCard] = {}
        self._table_cover_labels: dict[int, QLabel] = {}
        self._table_name_items: dict[int, QTableWidgetItem] = {}
        self._current_details_dialog: GameDetailsDialog | None = None

        steam_path = None
        if self.steam_userdata_dir is not None:
            try:
                steam_path = self.steam_userdata_dir.parents[1]
            except Exception:
                steam_path = None
        self.steam_name_manager = SteamNameManager(steam_path=steam_path, parent=self)
        self.steam_name_manager.name_resolved.connect(self._on_steam_name_resolved)

        # Dados da aplicação
        self.logs: list[dict[str, str]] = []
        self._games_cache: list[tuple[Game, int, bool]] = []
        self._view_mode = "grid"  # "grid" ou "list"
        self._is_quitting = False

        self._init_ui()
        self._init_tray()
        self._init_worker()

        self._on_theme_changed(self.theme_manager.tokens, self.theme_manager.is_dark)
        self.refresh_all()

        # Se primeira execução sem pasta configurada, abre em Configurações
        if not self.settings.watch_folder or not Path(self.settings.watch_folder).is_dir():
            self.sidebar.set_active("settings")
            self.pages.setCurrentIndex(3)
            self.lbl_welcome_notice.setVisible(True)

    def _init_ui(self) -> None:
        central = QWidget(self)
        central.setObjectName("centralWidget")
        self.setCentralWidget(central)

        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Sidebar fixa à esquerda (~210 px)
        self.sidebar = Sidebar(self.theme_manager.tokens, self)
        self.sidebar.page_changed.connect(self._on_sidebar_page_changed)
        main_layout.addWidget(self.sidebar)

        # Stack de páginas à direita
        self.pages = QStackedWidget(self)
        main_layout.addWidget(self.pages)

        # 1. Página de Atividade
        self.page_activity = self._create_activity_page()
        self.pages.addWidget(self.page_activity)

        # 2. Página de Jogos
        self.page_games = self._create_games_page()
        self.pages.addWidget(self.page_games)

        # 3. Página de Pendências
        self.page_pending = self._create_pending_page()
        self.pages.addWidget(self.page_pending)

        # 4. Página de Configurações
        self.page_settings = self._create_settings_page()
        self.pages.addWidget(self.page_settings)

    def _init_tray(self) -> None:
        self.tray = TrayManager(self)
        self.tray_icon = self.tray.tray_icon

    def _init_worker(self) -> None:
        self.worker = WatcherWorker(
            watch_folder=self.settings.watch_folder,
            steam_userdata_dir=self.steam_userdata_dir,
            config_path=self.config_path,
            pending_path=self.pending_path,
            fallback_game=self.settings.fallback_game,
            create_shortcut=self.settings.create_shortcut,
            hidden_shortcut=self.settings.hidden_shortcut,
            parent=self,
        )
        self.worker.evento_log.connect(self._on_worker_log_event)
        self.worker.importado.connect(self._on_worker_imported)
        self.worker.pendente.connect(self._on_worker_pending)
        self.worker.aplicado.connect(self._on_worker_applied)
        self.worker.erro.connect(self._on_worker_error)
        self.worker.estado.connect(self._on_worker_state_changed)
        self.worker.steam.connect(self._on_worker_steam_changed)

        if self.settings.watch_folder and Path(self.settings.watch_folder).is_dir():
            self.worker.start()

    def _on_worker_log_event(self, event: Event) -> None:
        pass

    def _on_worker_imported(self, game_name: str, image_path: str) -> None:
        self.add_log_entry("importado", game_name, f"Salvo em {Path(image_path).name}")
        self.tray.notify_imported(game_name)
        self.refresh_activity()
        self.refresh_games()

    def _on_worker_pending(self, game_name: str) -> None:
        self.add_log_entry("pendente", game_name, "Aguardando fechamento da Steam")
        self.tray.notify_pending(game_name)
        self.refresh_activity()
        self.refresh_pending()

    def _on_worker_applied(self, count: int) -> None:
        self.add_log_entry("aplicado", "Steam", f"{count} pendência(s) sincronizada(s)")
        self.tray.notify_applied(count)
        self.refresh_activity()
        self.refresh_pending()
        self.refresh_games()

    def _on_worker_error(self, message: str) -> None:
        self.add_log_entry("erro", "Sistema", message)
        self.tray.notify_error(message)

    def _on_worker_state_changed(self, state: str) -> None:
        if state == "rodando":
            self.sidebar.set_status("active", "Monitorando")
            self.tray.set_state("active")
        elif state == "pausado":
            self.sidebar.set_status("paused", "Pausado")
            self.tray.set_state("paused")
        else:
            self.sidebar.set_status("paused", "Parado")
            self.tray.set_state("paused")

    def _on_worker_steam_changed(self, is_open: bool) -> None:
        self.tray.set_steam_running(is_open)
        self.banner_steam_open.setVisible(is_open)
        self.btn_apply_now.setEnabled(not is_open)
        if is_open:
            self.sidebar.set_status("warning", "Steam aberta")
            self.tray.set_state("warning")
        else:
            if self.worker.is_running:
                self.sidebar.set_status("active", "Monitorando")
                self.tray.set_state("active")
            elif self.worker.is_paused:
                self.sidebar.set_status("paused", "Pausado")
                self.tray.set_state("paused")

    def closeEvent(self, event) -> None:
        if not self._is_quitting:
            event.ignore()
            self.hide()
        else:
            if hasattr(self, "worker") and self.worker is not None:
                self.worker.stop()
            event.accept()

    def force_quit(self) -> None:
        self._is_quitting = True
        if hasattr(self, "worker") and self.worker is not None:
            self.worker.stop()
        self.close()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _on_theme_changed(self, tokens: ThemeTokens, is_dark: bool) -> None:
        self.setStyleSheet(self.theme_manager.tokens.colors.bg)
        self.theme_manager.apply_to_app()
        self.sidebar.set_tokens(tokens)
        if hasattr(self, "tray"):
            self.tray.update_icon()
        self.refresh_games_view()

    def _on_sidebar_page_changed(self, page_id: str) -> None:
        page_map = {
            "activity": 0,
            "games": 1,
            "pending": 2,
            "settings": 3,
        }
        idx = page_map.get(page_id, 0)
        self.pages.setCurrentIndex(idx)
        if page_id == "activity":
            self.refresh_activity()
        elif page_id == "games":
            self.refresh_games()
        elif page_id == "pending":
            self.refresh_pending()

    # -------------------------------------------------------------------------
    # 1. TELA DE ATIVIDADE
    # -------------------------------------------------------------------------
    def _create_activity_page(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(32, 28, 32, 28)
        layout.setSpacing(24)

        lbl_title = QLabel("Atividade")
        lbl_title.setProperty("heading", "true")
        layout.addWidget(lbl_title)

        metrics_layout = QHBoxLayout()
        metrics_layout.setSpacing(16)

        self.metric_today = MetricCard("Importadas hoje", "0")
        self.metric_pending = MetricCard("Pendências", "0")
        self.metric_games = MetricCard("Jogos", "0")

        metrics_layout.addWidget(self.metric_today)
        metrics_layout.addWidget(self.metric_pending)
        metrics_layout.addWidget(self.metric_games)
        layout.addLayout(metrics_layout)

        self.log_card = QFrame()
        self.log_card.setProperty("card", "true")
        log_card_layout = QVBoxLayout(self.log_card)
        log_card_layout.setContentsMargins(20, 20, 20, 20)
        log_card_layout.setSpacing(12)

        lbl_log_header = QLabel("Registro recente")
        lbl_log_header.setProperty("subheading", "true")
        log_card_layout.addWidget(lbl_log_header)

        self.log_scroll = QScrollArea()
        self.log_scroll.setWidgetResizable(True)
        self.log_scroll.setFrameShape(QFrame.Shape.NoFrame)

        self.log_container = QWidget()
        self.log_layout = QVBoxLayout(self.log_container)
        self.log_layout.setContentsMargins(0, 4, 0, 4)
        self.log_layout.setSpacing(8)
        self.log_layout.addStretch()

        self.log_scroll.setWidget(self.log_container)
        log_card_layout.addWidget(self.log_scroll)

        self.activity_empty = EmptyState(
            "Nenhuma captura ainda",
            "Tire uma screenshot e ela aparece aqui.",
        )
        log_card_layout.addWidget(self.activity_empty)

        layout.addWidget(self.log_card, 1)
        return container

    def add_log_entry(
        self,
        event_type: str,
        game_name: str,
        message: str,
        timestamp: datetime | None = None,
    ) -> None:
        ts = timestamp or datetime.now()
        time_str = ts.strftime("%H:%M:%S")

        self.logs.append(
            {
                "time": time_str,
                "type": event_type,
                "game": game_name,
                "msg": message,
            }
        )
        self.refresh_activity()

    def refresh_activity(self) -> None:
        pending_count = len(load_pending(self.pending_path))
        self.metric_pending.set_value(pending_count)

        if self.steam_userdata_dir is not None:
            games_list = list_games(self.steam_userdata_dir, self.config_path)
            self.metric_games.set_value(len(games_list))
        else:
            self.metric_games.set_value(0)

        imported_today = sum(1 for e in self.logs if e["type"] in ("imported", "importado"))
        self.metric_today.set_value(imported_today)

        while self.log_layout.count() > 1:
            child = self.log_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        has_logs = len(self.logs) > 0
        self.activity_empty.setVisible(not has_logs)
        self.log_scroll.setVisible(has_logs)

        if has_logs:
            c = self.theme_manager.tokens.colors
            type_colors = {
                "importado": c.status_active,
                "imported": c.status_active,
                "pendente": c.status_warning,
                "pending": c.status_warning,
                "aplicado": c.accent,
                "applied": c.accent,
                "erro": c.status_error,
                "error": c.status_error,
                "aviso": c.status_warning,
            }

            for entry in reversed(self.logs[-50:]):
                row = QFrame()
                row.setFixedHeight(36)
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(8, 4, 8, 4)
                row_layout.setSpacing(12)

                lbl_time = QLabel(entry["time"])
                lbl_time.setProperty("secondary", "true")
                lbl_time.setFixedWidth(60)

                dot = QLabel()
                dot.setFixedSize(8, 8)
                color = type_colors.get(entry["type"].lower(), c.text_tertiary)
                dot.setStyleSheet(f"background-color: {color}; border-radius: 4px;")

                lbl_game = QLabel(entry["game"])
                lbl_game.setFont(QFont("Segoe UI Variable", 13, QFont.Weight.DemiBold))

                lbl_msg = QLabel(entry["msg"])
                lbl_msg.setProperty("secondary", "true")

                row_layout.addWidget(lbl_time)
                row_layout.addWidget(dot)
                row_layout.addWidget(lbl_game)
                row_layout.addWidget(lbl_msg)
                row_layout.addStretch()

                self.log_layout.insertWidget(self.log_layout.count() - 1, row)

    # -------------------------------------------------------------------------
    # 2. TELA DE JOGOS
    # -------------------------------------------------------------------------
    def _create_games_page(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(32, 28, 32, 28)
        layout.setSpacing(20)

        top_bar = QHBoxLayout()
        top_bar.setSpacing(16)

        lbl_title = QLabel("Jogos")
        lbl_title.setProperty("heading", "true")
        top_bar.addWidget(lbl_title)
        top_bar.addStretch()

        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText("Buscar jogo...")
        self.txt_search.setFixedWidth(240)
        self.txt_search.textChanged.connect(self._on_search_changed)
        top_bar.addWidget(self.txt_search)

        self.btn_show_deleted = QPushButton("Mostrar excluídos")
        self.btn_show_deleted.setCheckable(True)
        self.btn_show_deleted.toggled.connect(lambda: self.refresh_games_view())
        top_bar.addWidget(self.btn_show_deleted)

        self.btn_view_grid = QPushButton("Grade")
        self.btn_view_grid.setProperty("primary", "true")
        self.btn_view_grid.clicked.connect(lambda: self._set_view_mode("grid"))

        self.btn_view_list = QPushButton("Lista")
        self.btn_view_list.clicked.connect(lambda: self._set_view_mode("list"))

        top_bar.addWidget(self.btn_view_grid)
        top_bar.addWidget(self.btn_view_list)
        layout.addLayout(top_bar)

        self.games_empty = EmptyState(
            "Nenhum jogo encontrado",
            "Nenhum jogo corresponde à busca ou à biblioteca da Steam.",
        )
        layout.addWidget(self.games_empty)

        self.grid_scroll = QScrollArea()
        self.grid_scroll.setWidgetResizable(True)
        self.grid_scroll.setFrameShape(QFrame.Shape.NoFrame)

        self.grid_container = QWidget()
        self.grid_layout = QGridLayout(self.grid_container)
        self.grid_layout.setContentsMargins(0, 4, 0, 4)
        self.grid_layout.setSpacing(16)
        self.grid_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

        self.grid_scroll.setWidget(self.grid_container)
        layout.addWidget(self.grid_scroll, 1)

        self.table_games = QTableWidget()
        self.table_games.setColumnCount(5)
        self.table_games.setHorizontalHeaderLabels(["CAPA", "GAME_ID", "TIPO", "NOME", "SCREENSHOTS"])
        self.table_games.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table_games.setColumnWidth(0, 80)
        self.table_games.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table_games.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table_games.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table_games.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table_games.verticalHeader().setDefaultSectionSize(98)
        self.table_games.verticalHeader().setVisible(False)
        self.table_games.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table_games.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table_games.cellDoubleClicked.connect(self._on_table_cell_clicked)
        layout.addWidget(self.table_games, 1)

        return container

    def _set_view_mode(self, mode: str) -> None:
        self._view_mode = mode
        self.btn_view_grid.setProperty("primary", "true" if mode == "grid" else "false")
        self.btn_view_list.setProperty("primary", "true" if mode == "list" else "false")
        self.btn_view_grid.style().unpolish(self.btn_view_grid)
        self.btn_view_grid.style().polish(self.btn_view_grid)
        self.btn_view_list.style().unpolish(self.btn_view_list)
        self.btn_view_list.style().polish(self.btn_view_list)
        self.refresh_games_view()

    def _on_search_changed(self, text: str) -> None:
        self.refresh_games_view()

    def refresh_games(self) -> None:
        if self.steam_userdata_dir is None:
            self._games_cache = []
            self.refresh_games_view()
            return

        all_games = list_games(self.steam_userdata_dir, self.config_path)
        exe_map = load_exe_map(self.config_path)
        prov_map = {m.game_id: m.provisional for m in exe_map.values()}
        ignored_map = {m.game_id: m.ignored for m in exe_map.values()}

        cache: list[tuple[Game, int, bool, bool]] = []
        for g in all_games:
            folder = self.steam_userdata_dir / "760" / "remote" / str(g.game_id)
            count = len(screenshot_files(folder))
            is_ign = ignored_map.get(g.game_id, False)
            if count > 0 or is_ign:
                is_prov = prov_map.get(g.game_id, False)
                cache.append((g, count, is_prov, is_ign))
                if (g.kind == "steam" or g.game_id <= 0xFFFFFFFF) and g.name.startswith("AppID "):
                    self.steam_name_manager.get_or_request_name(g.game_id, allow_network=True)

        self._games_cache = cache
        self.refresh_games_view()

    def refresh_games_view(self) -> None:
        query = self.txt_search.text().strip().casefold()
        show_deleted = (
            getattr(self, "btn_show_deleted", None)
            and self.btn_show_deleted.isChecked()
        )
        filtered = [
            (g, cnt, prov, ign)
            for (g, cnt, prov, ign) in self._games_cache
            if (show_deleted or not ign)
            and (not query or query in g.name.casefold() or query in str(g.game_id))
        ]

        has_items = len(filtered) > 0
        self.games_empty.setVisible(not has_items)

        self._game_cards.clear()
        self._table_cover_labels.clear()
        self._table_name_items.clear()

        if self._view_mode == "grid":
            self.table_games.setVisible(False)
            self.grid_scroll.setVisible(has_items)

            while self.grid_layout.count() > 0:
                child = self.grid_layout.takeAt(0)
                if child.widget():
                    child.widget().deleteLater()

            cols = 5
            for idx, (game, count, is_prov, is_ign) in enumerate(filtered):
                row = idx // cols
                col = idx % cols
                pix, source = self.cover_manager.request_cover(
                    game.game_id,
                    game.name,
                    width=120,
                    height=180,
                    is_dark=self.theme_manager.is_dark,
                )
                card = GameCard(
                    game,
                    count,
                    is_prov,
                    self.theme_manager.tokens,
                    self.steam_userdata_dir,
                    cover_pixmap=pix,
                    cover_source=source,
                    is_ignored=is_ign,
                    parent=self,
                )
                card.clicked.connect(self._on_game_card_clicked)
                self._game_cards[game.game_id] = card
                self.grid_layout.addWidget(card, row, col)

        else:
            self.grid_scroll.setVisible(False)
            self.table_games.setVisible(has_items)

            self.table_games.setRowCount(len(filtered))
            for row_idx, (game, count, is_prov, is_ign) in enumerate(filtered):
                pix, source = self.cover_manager.request_cover(
                    game.game_id,
                    game.name,
                    width=60,
                    height=90,
                    is_dark=self.theme_manager.is_dark,
                )
                lbl_cover = QLabel()
                lbl_cover.setFixedSize(60, 90)
                lbl_cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
                lbl_cover.setPixmap(pix)
                lbl_cover.setToolTip(f"Capa: {source}")
                self._table_cover_labels[game.game_id] = lbl_cover

                container = QWidget()
                h_box = QHBoxLayout(container)
                h_box.setContentsMargins(0, 0, 0, 0)
                h_box.setAlignment(Qt.AlignmentFlag.AlignCenter)
                h_box.addWidget(lbl_cover)
                self.table_games.setCellWidget(row_idx, 0, container)

                self.table_games.setItem(row_idx, 1, QTableWidgetItem(str(game.game_id)))
                kind_str = (
                    f"{game.kind} (excluído)"
                    if is_ign
                    else (f"{game.kind} (provisório)" if is_prov else game.kind)
                )
                self.table_games.setItem(row_idx, 2, QTableWidgetItem(kind_str))
                name_item = QTableWidgetItem(game.name)
                self._table_name_items[game.game_id] = name_item
                self.table_games.setItem(row_idx, 3, name_item)
                self.table_games.setItem(row_idx, 4, QTableWidgetItem(str(count)))

    def _on_steam_name_resolved(self, appid: int, name: str, source: str) -> None:
        new_cache = []
        for (g, cnt, prov, ign) in self._games_cache:
            if g.game_id == appid:
                new_g = Game(name=name, game_id=g.game_id, kind=g.kind, source=g.source, orphan=g.orphan)
                new_cache.append((new_g, cnt, prov, ign))
            else:
                new_cache.append((g, cnt, prov, ign))
        self._games_cache = new_cache

        card = self._game_cards.get(appid)
        if card is not None:
            card.set_name(name)

        item = self._table_name_items.get(appid)
        if item is not None:
            item.setText(name)

        if (
            getattr(self, "_current_details_dialog", None)
            and self._current_details_dialog.game.game_id == appid
        ):
            self._current_details_dialog.set_name(name)

    def _on_cover_loaded(self, game_id: int, game_name: str, pixmap: QPixmap, source: str) -> None:
        card = self._game_cards.get(game_id)
        if card is not None:
            card.set_cover(pixmap, source)
        table_lbl = self._table_cover_labels.get(game_id)
        if table_lbl is not None:
            scaled = pixmap.scaled(
                60,
                90,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            ).copy(0, 0, 60, 90)
            table_lbl.setPixmap(scaled)
            table_lbl.setToolTip(f"Capa: {source}")
        if (
            getattr(self, "_current_details_dialog", None)
            and self._current_details_dialog.game.game_id == game_id
        ):
            details_scaled = pixmap.scaled(
                100,
                150,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            ).copy(0, 0, 100, 150)
            self._current_details_dialog.set_cover(details_scaled, source)

    def _on_game_card_clicked(self, game: Game) -> None:
        self._open_game_details(game)

    def _on_table_cell_clicked(self, row: int, col: int) -> None:
        item = self.table_games.item(row, 1)
        if item:
            gid = item.text()
            found = next((g for (g, _, _, _) in self._games_cache if str(g.game_id) == gid), None)
            if found:
                self._open_game_details(found)

    def _open_game_details(self, game: Game) -> None:
        entry = next(((g, cnt, prov, ign) for (g, cnt, prov, ign) in self._games_cache if g.game_id == game.game_id), None)
        count = entry[1] if entry else 0
        is_prov = entry[2] if entry else False
        is_ign = entry[3] if entry else False

        pix, src = self.cover_manager.get_cover_pixmap_sync(
            game.game_id,
            game.name,
            width=100,
            height=150,
            is_dark=self.theme_manager.is_dark,
        )

        removal_info = None
        if self.steam_userdata_dir is not None and can_remove_game(game.game_id):
            try:
                removal_info = inspect_removal(
                    game.game_id,
                    self.steam_userdata_dir,
                    self.config_path,
                )
            except Exception:
                removal_info = None

        dialog = GameDetailsDialog(
            game,
            count,
            is_prov,
            self.theme_manager.tokens,
            cover_pixmap=pix,
            cover_source=src,
            steam_userdata_dir=self.steam_userdata_dir,
            is_ignored=is_ign,
            is_app_managed=is_app_managed_game(game.game_id, self.config_path),
            removal_info=removal_info,
            parent=self,
        )
        self._current_details_dialog = dialog
        dialog.action_requested.connect(lambda action, payload: self._on_game_action(action, payload, dialog))
        dialog.exec()
        self._current_details_dialog = None

    def _on_game_action(self, action: str, payload: object, dialog: GameDetailsDialog | None = None) -> None:
        if action == "rename":
            game = payload if isinstance(payload, Game) else payload[0]
            new_name, ok = QInputDialog.getText(
                self,
                "Renomear jogo",
                f"Novo nome para '{game.name}':",
                text=game.name,
            )
            if not ok:
                return
            if self.steam_userdata_dir is None:
                QMessageBox.warning(
                    self,
                    "Renomear jogo",
                    "Pasta userdata da Steam não configurada.",
                )
                return
            try:
                result = rename_game(
                    game.game_id,
                    new_name,
                    self.steam_userdata_dir,
                    self.config_path,
                    self.pending_path,
                )
            except GameValidationError as exc:
                QMessageBox.warning(self, "Nome inválido", str(exc))
                return
            except Exception as exc:
                LOGGER.exception("Falha ao renomear o jogo %s", game.game_id)
                QMessageBox.warning(
                    self,
                    "Erro ao renomear",
                    f"Não foi possível renomear: {exc}",
                )
                return

            if dialog is not None:
                dialog.set_name(result.name)
                dialog.set_status(
                    "pendente"
                    if result.status == "pendente"
                    else "nome aplicado"
                )
            self.refresh_games()
            self.refresh_pending()
            if result.status == "pendente":
                message = (
                    "Nome alterado; será aplicado na Steam quando ela fechar"
                )
                self.add_log_entry("pendente", result.name, message)
            else:
                message = "Nome alterado"
                self.add_log_entry("aplicado", result.name, message)
            QMessageBox.information(self, "Renomear jogo", message)

        elif action == "resolve":
            game = payload if isinstance(payload, Game) else payload[0]
            if is_manual_game_name(game.game_id, self.config_path):
                QMessageBox.information(
                    self,
                    "Identificação",
                    "Este jogo tem um nome definido manualmente. "
                    "Use 'Renomear jogo...' para alterá-lo.",
                )
                return
            exe_map = load_exe_map(self.config_path)
            match_exe = next((m.exe for m in exe_map.values() if m.game_id == game.game_id), None)
            if match_exe:
                better_name, source = resolve_game_name(match_exe)
                if source != "exe" and better_name != game.name:
                    update_exe_mapping(
                        match_exe,
                        better_name,
                        source,
                        provisional=False,
                        config_path=self.config_path,
                    )
                    pix, src = self.cover_manager.request_cover(
                        game.game_id,
                        better_name,
                        force=True,
                        width=100,
                        height=150,
                        is_dark=self.theme_manager.is_dark,
                    )
                    if dialog is not None:
                        dialog.set_name(better_name)
                        dialog.set_cover(pix, src)
                    self.refresh_games()
                    QMessageBox.information(
                        self,
                        "Nome reidentificado",
                        f"Novo nome descoberto ({source}): '{better_name}'",
                    )
                else:
                    QMessageBox.information(
                        self,
                        "Identificação",
                        f"Nenhum nome melhor encontrado para o executável '{match_exe}'.",
                    )

        elif action == "refresh_cover":
            game = payload if isinstance(payload, Game) else payload[0]
            pix, src = self.cover_manager.request_cover(
                game.game_id,
                game.name,
                force=True,
                width=100,
                height=150,
                is_dark=self.theme_manager.is_dark,
            )
            if dialog is not None:
                dialog.set_cover(pix, src)
            self.refresh_games_view()

        elif action == "cover_updated":
            self.refresh_games_view()

        elif action == "delete":
            game = payload[0]
            remove_screenshots = bool(payload[1])
            include_published = bool(payload[2]) if len(payload) > 2 else False
            if self.steam_userdata_dir is not None:
                res = remove_game(
                    game.game_id,
                    self.steam_userdata_dir,
                    remove_screenshots=remove_screenshots,
                    include_published=include_published,
                    config_path=self.config_path,
                    pending_path=self.pending_path,
                )
                self.refresh_all()
                self.add_log_entry("aplicado", game.name, f"Jogo excluído ({res.status})")
                if res.warning:
                    self.add_log_entry("aviso", game.name, res.warning)
                self.last_remove_result = res

        elif action == "restore":
            game = payload if isinstance(payload, Game) else payload[0]
            if self.steam_userdata_dir is not None:
                res = restore_game(
                    game.game_id,
                    self.steam_userdata_dir,
                    config_path=self.config_path,
                    pending_path=self.pending_path,
                )
                self.refresh_all()
                self.add_log_entry("aplicado", game.name, f"Jogo restaurado ({res.status})")

    # -------------------------------------------------------------------------
    # 3. TELA DE PENDÊNCIAS
    # -------------------------------------------------------------------------
    def _create_pending_page(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(32, 28, 32, 28)
        layout.setSpacing(20)

        top_bar = QHBoxLayout()
        lbl_title = QLabel("Pendências")
        lbl_title.setProperty("heading", "true")
        top_bar.addWidget(lbl_title)
        top_bar.addStretch()

        self.btn_apply_now = QPushButton("Aplicar agora")
        self.btn_apply_now.setProperty("primary", "true")
        self.btn_apply_now.clicked.connect(self._on_apply_pending_clicked)
        top_bar.addWidget(self.btn_apply_now)
        layout.addLayout(top_bar)

        self.banner_steam_open = QFrame()
        self.banner_steam_open.setProperty("banner", "warning")
        banner_layout = QHBoxLayout(self.banner_steam_open)
        banner_layout.setContentsMargins(16, 12, 16, 12)
        banner_layout.setSpacing(12)

        self.lbl_banner = QLabel(
            "Steam em execução: capturas foram salvas, mas o VDF só pode ser "
            "atualizado com a Steam fechada."
        )
        self.lbl_banner.setWordWrap(True)
        banner_layout.addWidget(self.lbl_banner)
        layout.addWidget(self.banner_steam_open)

        self.pending_empty = EmptyState(
            "Nenhuma pendência",
            "Todas as screenshots e atalhos estão sincronizados.",
        )
        layout.addWidget(self.pending_empty)

        self.pending_scroll = QScrollArea()
        self.pending_scroll.setWidgetResizable(True)
        self.pending_scroll.setFrameShape(QFrame.Shape.NoFrame)

        self.pending_container = QWidget()
        self.pending_layout = QVBoxLayout(self.pending_container)
        self.pending_layout.setContentsMargins(0, 4, 0, 4)
        self.pending_layout.setSpacing(12)
        self.pending_layout.addStretch()

        self.pending_scroll.setWidget(self.pending_container)
        layout.addWidget(self.pending_scroll, 1)

        return container

    def refresh_pending(self) -> None:
        steam_running = is_steam_running()
        self.banner_steam_open.setVisible(steam_running)
        self.btn_apply_now.setEnabled(not steam_running)

        entries = load_pending(self.pending_path)
        has_entries = len(entries) > 0

        self.pending_empty.setVisible(not has_entries)
        self.pending_scroll.setVisible(has_entries)

        while self.pending_layout.count() > 1:
            child = self.pending_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        for record in entries:
            card = QFrame()
            card.setProperty("card", "true")
            card_layout = QHBoxLayout(card)
            card_layout.setContentsMargins(16, 14, 16, 14)
            card_layout.setSpacing(16)

            info_col = QVBoxLayout()
            info_col.setSpacing(2)

            rec_type = record.get("type")
            game_title = record.get("game_name") or record.get("game_id", "Jogo")
            if rec_type == "remove":
                lbl_game = QLabel(f"Remoção de jogo: {game_title}")
                with_shots = record.get("remove_screenshots", False)
                sub_text = (
                    "Remoção de atalho e VDF"
                    + (" (com screenshots movidas para a lixeira)" if with_shots else "")
                )
                lbl_file = QLabel(sub_text)
            elif rec_type == "rename":
                lbl_game = QLabel(f"Renomeação: {game_title}")
                lbl_file = QLabel("Atualização de nome pendente")
            else:
                lbl_game = QLabel(str(game_title))
                entry = record.get("entry") or {}
                filename = entry.get("filename") or "(captura pendente)"
                lbl_file = QLabel(str(filename))

            lbl_game.setFont(QFont("Segoe UI Variable", 13, QFont.Weight.DemiBold))
            lbl_file.setProperty("secondary", "true")

            info_col.addWidget(lbl_game)
            info_col.addWidget(lbl_file)

            card_layout.addLayout(info_col)
            card_layout.addStretch()

            status_lbl = QLabel("Pendente")
            status_lbl.setStyleSheet("color: #F59E0B; font-weight: 600; font-size: 11px;")
            card_layout.addWidget(status_lbl)

            self.pending_layout.insertWidget(self.pending_layout.count() - 1, card)

    def _on_apply_pending_clicked(self) -> None:
        if self.steam_userdata_dir is None:
            QMessageBox.warning(self, "Aviso", "Pasta userdata da Steam não configurada.")
            return

        result = apply_pending(self.steam_userdata_dir, self.pending_path)
        if result.status == "steam_aberta":
            QMessageBox.warning(
                self,
                "Steam aberta",
                "A Steam continua aberta. Feche a Steam antes de aplicar as pendências.",
            )
        elif result.status == "aplicado":
            QMessageBox.information(
                self,
                "Sucesso",
                f"{result.applied} pendência(s) aplicada(s) com sucesso!",
            )
            self.add_log_entry("aplicado", "Steam", f"{result.applied} pendências aplicadas")
        self.refresh_pending()
        self.refresh_activity()
        self.refresh_games()

    # -------------------------------------------------------------------------
    # 4. TELA DE CONFIGURAÇÕES
    # -------------------------------------------------------------------------
    def _create_settings_page(self) -> QWidget:
        container = QWidget()
        outer_layout = QVBoxLayout(container)
        outer_layout.setContentsMargins(32, 28, 32, 28)
        outer_layout.setSpacing(20)

        lbl_title = QLabel("Configurações")
        lbl_title.setProperty("heading", "true")
        outer_layout.addWidget(lbl_title)

        self.lbl_welcome_notice = QLabel(
            "Bem-vindo! Selecione a pasta de capturas do MSI Afterburner para iniciar o monitoramento."
        )
        self.lbl_welcome_notice.setStyleSheet(
            "color: #10B981; font-weight: 600; font-size: 13px; margin-bottom: 8px;"
        )
        self.lbl_welcome_notice.setVisible(False)
        outer_layout.addWidget(self.lbl_welcome_notice)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(20)

        # Cartão 1: Aparência
        card_theme = QFrame()
        card_theme.setProperty("card", "true")
        ct_layout = QVBoxLayout(card_theme)
        ct_layout.setContentsMargins(20, 18, 20, 18)
        ct_layout.setSpacing(12)

        lbl_ct_title = QLabel("Aparência")
        lbl_ct_title.setProperty("subheading", "true")
        ct_layout.addWidget(lbl_ct_title)

        row_theme = QHBoxLayout()
        lbl_theme = QLabel("Tema do aplicativo:")
        self.combo_theme = QComboBox()
        self.combo_theme.addItem("Automático (seguir Windows)", "auto")
        self.combo_theme.addItem("Claro", "light")
        self.combo_theme.addItem("Escuro", "dark")
        self.combo_theme.currentIndexChanged.connect(self._on_theme_selection_changed)
        row_theme.addWidget(lbl_theme)
        row_theme.addStretch()
        row_theme.addWidget(self.combo_theme)
        ct_layout.addLayout(row_theme)
        content_layout.addWidget(card_theme)

        # Cartão 2: Monitoramento
        card_watch = QFrame()
        card_watch.setProperty("card", "true")
        cw_layout = QVBoxLayout(card_watch)
        cw_layout.setContentsMargins(20, 18, 20, 18)
        cw_layout.setSpacing(12)

        lbl_cw_title = QLabel("Monitoramento de Capturas")
        lbl_cw_title.setProperty("subheading", "true")
        cw_layout.addWidget(lbl_cw_title)

        row_watch_folder = QHBoxLayout()
        lbl_wf = QLabel("Pasta monitorada (Afterburner):")
        self.txt_watch_folder = QLineEdit()
        self.txt_watch_folder.setPlaceholderText("Caminho da pasta de capturas...")
        self.lbl_wf_error = QLabel()
        self.lbl_wf_error.setStyleSheet("color: #EF4444; font-size: 11px;")
        btn_browse_wf = QPushButton("Procurar...")
        btn_browse_wf.clicked.connect(self._on_browse_watch_folder)

        row_watch_folder.addWidget(lbl_wf)
        row_watch_folder.addWidget(self.txt_watch_folder, 1)
        row_watch_folder.addWidget(self.lbl_wf_error)
        row_watch_folder.addWidget(btn_browse_wf)
        cw_layout.addLayout(row_watch_folder)

        row_fallback = QHBoxLayout()
        lbl_fb = QLabel("Jogo fallback (arquivos fora do padrão):")
        self.txt_fallback_game = QLineEdit()
        self.txt_fallback_game.setPlaceholderText("Nome do jogo (opcional)...")
        row_fallback.addWidget(lbl_fb)
        row_fallback.addWidget(self.txt_fallback_game, 1)
        cw_layout.addLayout(row_fallback)

        content_layout.addWidget(card_watch)

        # Cartão 3: Steam
        card_steam = QFrame()
        card_steam.setProperty("card", "true")
        cs_layout = QVBoxLayout(card_steam)
        cs_layout.setContentsMargins(20, 18, 20, 18)
        cs_layout.setSpacing(12)

        lbl_cs_title = QLabel("Steam")
        lbl_cs_title.setProperty("subheading", "true")
        cs_layout.addWidget(lbl_cs_title)

        row_steam_dir = QHBoxLayout()
        lbl_sd = QLabel("ID ou pasta userdata da Steam:")
        self.txt_steam_dir = QLineEdit()
        self.txt_steam_dir.setPlaceholderText("ID numérico ou caminho (vazio = autodetectar)...")
        self.lbl_sd_error = QLabel()
        self.lbl_sd_error.setStyleSheet("color: #EF4444; font-size: 11px;")
        btn_browse_sd = QPushButton("Procurar...")
        btn_browse_sd.clicked.connect(self._on_browse_steam_dir)

        row_steam_dir.addWidget(lbl_sd)
        row_steam_dir.addWidget(self.txt_steam_dir, 1)
        row_steam_dir.addWidget(self.lbl_sd_error)
        row_steam_dir.addWidget(btn_browse_sd)
        cs_layout.addLayout(row_steam_dir)

        row_shortcut = QHBoxLayout()
        lbl_sc = QLabel("Criar atalho para jogos não-Steam automaticamente:")
        self.toggle_shortcut = ToggleSwitch()
        self.toggle_shortcut.set_tokens(self.theme_manager.tokens)
        row_shortcut.addWidget(lbl_sc)
        row_shortcut.addStretch()
        row_shortcut.addWidget(self.toggle_shortcut)
        cs_layout.addLayout(row_shortcut)

        row_hidden = QHBoxLayout()
        lbl_hd = QLabel("Criar atalhos como ocultos:")
        self.toggle_hidden = ToggleSwitch()
        self.toggle_hidden.set_tokens(self.theme_manager.tokens)
        row_hidden.addWidget(lbl_hd)
        row_hidden.addStretch()
        row_hidden.addWidget(self.toggle_hidden)
        cs_layout.addLayout(row_hidden)

        content_layout.addWidget(card_steam)

        # Cartão 4: Notificações
        card_notif = QFrame()
        card_notif.setProperty("card", "true")
        cn_layout = QVBoxLayout(card_notif)
        cn_layout.setContentsMargins(20, 18, 20, 18)
        cn_layout.setSpacing(12)

        lbl_cn_title = QLabel("Notificações")
        lbl_cn_title.setProperty("subheading", "true")
        cn_layout.addWidget(lbl_cn_title)

        row_notif = QHBoxLayout()
        lbl_nt = QLabel("Exibir notificações no sistema:")
        self.toggle_notifications = ToggleSwitch()
        self.toggle_notifications.set_tokens(self.theme_manager.tokens)
        row_notif.addWidget(lbl_nt)
        row_notif.addStretch()
        row_notif.addWidget(self.toggle_notifications)
        cn_layout.addLayout(row_notif)

        content_layout.addWidget(card_notif)

        # Cartão 5: Sistema
        card_sys = QFrame()
        card_sys.setProperty("card", "true")
        csys_layout = QVBoxLayout(card_sys)
        csys_layout.setContentsMargins(20, 18, 20, 18)
        csys_layout.setSpacing(12)

        lbl_csys_title = QLabel("Sistema")
        lbl_csys_title.setProperty("subheading", "true")
        csys_layout.addWidget(lbl_csys_title)

        row_autostart = QHBoxLayout()
        lbl_as = QLabel("Iniciar com o Windows:")
        self.toggle_autostart = ToggleSwitch()
        self.toggle_autostart.set_tokens(self.theme_manager.tokens)
        row_autostart.addWidget(lbl_as)
        row_autostart.addStretch()
        row_autostart.addWidget(self.toggle_autostart)
        csys_layout.addLayout(row_autostart)

        row_minimized = QHBoxLayout()
        lbl_min = QLabel("Iniciar minimizado na bandeja:")
        self.toggle_start_minimized = ToggleSwitch()
        self.toggle_start_minimized.set_tokens(self.theme_manager.tokens)
        row_minimized.addWidget(lbl_min)
        row_minimized.addStretch()
        row_minimized.addWidget(self.toggle_start_minimized)
        csys_layout.addLayout(row_minimized)

        content_layout.addWidget(card_sys)
        content_layout.addStretch()

        scroll.setWidget(content)
        outer_layout.addWidget(scroll, 1)

        # Rodapé fixo com botão Salvar
        footer = QHBoxLayout()
        footer.addStretch()
        self.btn_save_settings = QPushButton("Salvar configurações")
        self.btn_save_settings.setProperty("primary", "true")
        self.btn_save_settings.clicked.connect(self.save_settings)
        footer.addWidget(self.btn_save_settings)
        outer_layout.addLayout(footer)

        self.load_settings()
        return container

    def _on_theme_selection_changed(self, index: int) -> None:
        data = self.combo_theme.currentData()
        self.theme_manager.set_mode(data)

    def _on_browse_watch_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Selecionar pasta de capturas")
        if folder:
            self.txt_watch_folder.setText(folder)
            self.lbl_wf_error.setText("")

    def _on_browse_steam_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Selecionar pasta userdata da Steam")
        if folder:
            self.txt_steam_dir.setText(folder)
            self.lbl_sd_error.setText("")

    def load_settings(self) -> None:
        self.settings = load_settings(self.config_path)

        if self.settings.watch_folder:
            self.txt_watch_folder.setText(self.settings.watch_folder)

        if self.settings.steam_userdata_id:
            self.txt_steam_dir.setText(self.settings.steam_userdata_id)
        elif self.steam_userdata_dir:
            self.txt_steam_dir.setText(str(self.steam_userdata_dir))

        self.txt_fallback_game.setText(self.settings.fallback_game)
        self.toggle_notifications.setChecked(self.settings.notifications)
        self.toggle_shortcut.setChecked(self.settings.create_shortcut)
        self.toggle_hidden.setChecked(self.settings.hidden_shortcut)
        self.toggle_autostart.setChecked(self.settings.start_with_windows)
        self.toggle_start_minimized.setChecked(self.settings.start_minimized)

        theme_val = self.settings.theme
        idx = self.combo_theme.findData(theme_val)
        if idx >= 0:
            self.combo_theme.setCurrentIndex(idx)

    def save_settings(self) -> bool:
        valid = True
        wf = self.txt_watch_folder.text().strip()
        sd = self.txt_steam_dir.text().strip()

        if wf and not Path(wf).is_dir():
            self.lbl_wf_error.setText("Pasta inexistente")
            self.txt_watch_folder.setProperty("invalid", "true")
            valid = False
        else:
            self.lbl_wf_error.setText("")
            self.txt_watch_folder.setProperty("invalid", "false")

        resolved_sd: Path | None = None
        if sd:
            sd_path = Path(sd)
            if sd_path.is_dir() and (sd_path / "760").is_dir():
                resolved_sd = sd_path
                self.lbl_sd_error.setText("")
                self.txt_steam_dir.setProperty("invalid", "false")
            elif sd.isdigit():
                try:
                    inst = find_steam_installation()
                    cand = userdata_path(inst.path, sd)
                    if (cand / "760").is_dir():
                        resolved_sd = cand
                        self.lbl_sd_error.setText("")
                        self.txt_steam_dir.setProperty("invalid", "false")
                    else:
                        self.lbl_sd_error.setText("Userdata não encontrado na Steam")
                        self.txt_steam_dir.setProperty("invalid", "true")
                        valid = False
                except Exception:
                    self.lbl_sd_error.setText("Steam não localizada")
                    self.txt_steam_dir.setProperty("invalid", "true")
                    valid = False
            else:
                self.lbl_sd_error.setText("Userdata inválido (falta pasta 760)")
                self.txt_steam_dir.setProperty("invalid", "true")
                valid = False

        self.txt_watch_folder.style().unpolish(self.txt_watch_folder)
        self.txt_watch_folder.style().polish(self.txt_watch_folder)
        self.txt_steam_dir.style().unpolish(self.txt_steam_dir)
        self.txt_steam_dir.style().polish(self.txt_steam_dir)

        if not valid:
            return False

        if resolved_sd is not None:
            self.steam_userdata_dir = resolved_sd

        # Atualiza o objeto Settings
        prev_folder = self.settings.watch_folder
        self.settings.watch_folder = wf
        self.settings.steam_userdata_id = sd
        self.settings.fallback_game = self.txt_fallback_game.text().strip()
        self.settings.notifications = self.toggle_notifications.isChecked()
        self.settings.create_shortcut = self.toggle_shortcut.isChecked()
        self.settings.hidden_shortcut = self.toggle_hidden.isChecked()
        self.settings.start_with_windows = self.toggle_autostart.isChecked()
        self.settings.start_minimized = self.toggle_start_minimized.isChecked()
        self.settings.theme = self.combo_theme.currentData()

        save_settings(self.settings, self.config_path)

        # Autostart
        if self.settings.start_with_windows:
            autostart.enable()
        else:
            autostart.disable()

        # Reinicia o worker se a pasta mudou ou se estava parado e agora tem pasta
        if hasattr(self, "worker") and self.worker is not None:
            self.worker.fallback_game = self.settings.fallback_game
            self.worker.create_shortcut = self.settings.create_shortcut
            self.worker.hidden_shortcut = self.settings.hidden_shortcut
            if wf != prev_folder or not self.worker.is_running:
                self.worker.restart(watch_folder=wf, steam_userdata_dir=self.steam_userdata_dir)

        self.lbl_welcome_notice.setVisible(False)
        self.add_log_entry("aplicado", "Configurações", "Configurações salvas com sucesso")
        return True

    def refresh_all(self) -> None:
        self.refresh_activity()
        self.refresh_games()
        self.refresh_pending()
