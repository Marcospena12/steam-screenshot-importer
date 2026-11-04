from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import (
    QEvent,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..games import Game
from ..removal import (
    PUBLISHED_PRESERVED_WARNING,
    RemovalInfo,
    can_remove_game,
)
from ..steam import is_steam_running
from .covers import CoverManager, generate_placeholder_cover, set_custom_cover
from .theme import ThemeTokens, get_tokens


def draw_icon(
    painter: QPainter,
    icon_type: str,
    x: float,
    y: float,
    size: float,
    color: QColor,
) -> None:
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(color, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    if icon_type == "activity":
        # Pulso / batimento cardíaco
        path = QPainterPath()
        path.moveTo(x + size * 0.1, y + size * 0.5)
        path.lineTo(x + size * 0.35, y + size * 0.5)
        path.lineTo(x + size * 0.45, y + size * 0.18)
        path.lineTo(x + size * 0.58, y + size * 0.82)
        path.lineTo(x + size * 0.68, y + size * 0.5)
        path.lineTo(x + size * 0.9, y + size * 0.5)
        painter.drawPath(path)

    elif icon_type == "games":
        # Gamepad moderno minimalista
        path = QPainterPath()
        rect = QRectF(x + size * 0.12, y + size * 0.22, size * 0.76, size * 0.56)
        path.addRoundedRect(rect, size * 0.2, size * 0.2)
        painter.drawPath(path)
        # D-pad cross à esquerda
        painter.drawLine(QPointF(x + size * 0.28, y + size * 0.5), QPointF(x + size * 0.42, y + size * 0.5))
        painter.drawLine(QPointF(x + size * 0.35, y + size * 0.43), QPointF(x + size * 0.35, y + size * 0.57))
        # Botões à direita
        painter.setBrush(color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPointF(x + size * 0.62, y + size * 0.46), size * 0.04, size * 0.04)
        painter.drawEllipse(QPointF(x + size * 0.72, y + size * 0.54), size * 0.04, size * 0.04)

    elif icon_type == "pending":
        # Relógio / ampulheta
        painter.drawEllipse(QRectF(x + size * 0.15, y + size * 0.15, size * 0.7, size * 0.7))
        center = QPointF(x + size * 0.5, y + size * 0.5)
        painter.drawLine(center, QPointF(x + size * 0.5, y + size * 0.3))
        painter.drawLine(center, QPointF(x + size * 0.68, y + size * 0.5))

    elif icon_type == "settings":
        # Engrenagem moderna
        radius_outer = size * 0.34
        radius_inner = size * 0.16
        center = QPointF(x + size * 0.5, y + size * 0.5)
        painter.drawEllipse(center, radius_outer, radius_outer)
        painter.drawEllipse(center, radius_inner, radius_inner)
        # Marcadores dos dentes
        for angle in range(0, 360, 45):
            painter.save()
            painter.translate(center)
            painter.rotate(angle)
            painter.drawLine(QPointF(0, -radius_outer), QPointF(0, -size * 0.42))
            painter.restore()

    painter.restore()


def create_tray_icon(
    status: str = "active",
    dark_mode: bool = True,
) -> QIcon:
    """Cria um ícone da bandeja nítido em 16, 24 e 32 px com o ponto de estado."""
    icon = QIcon()
    for size in (16, 24, 32):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Forma arredondada principal com cor de destaque
        accent_color = QColor("#6C7CFF" if dark_mode else "#4F5BD5")
        rect = QRectF(0.5, 0.5, size - 1, size - 1)
        path = QPainterPath()
        radius = size * 0.28
        path.addRoundedRect(rect, radius, radius)
        painter.fillPath(path, accent_color)

        # Câmera / abertura estilizada central
        inner_pen = QPen(QColor("#FFFFFF"), max(1.2, size * 0.08))
        painter.setPen(inner_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        center_circle = QRectF(size * 0.28, size * 0.28, size * 0.44, size * 0.44)
        painter.drawEllipse(center_circle)

        # Ponto de estado discreto no canto inferior direito
        if status == "active":
            dot_color = QColor("#10B981")  # Verde
        elif status == "warning":
            dot_color = QColor("#F59E0B")  # Âmbar
        elif status == "error":
            dot_color = QColor("#EF4444")  # Vermelho
        else:
            dot_color = QColor("#9CA3AF")  # Cinza

        dot_radius = max(2.5, size * 0.2)
        dot_center = QPointF(size - dot_radius - 0.5, size - dot_radius - 0.5)

        # Borda de recorte ao redor do dot
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#0F1115" if dark_mode else "#F5F6F8"))
        painter.drawEllipse(dot_center, dot_radius + 1.0, dot_radius + 1.0)

        # Dot
        painter.setBrush(dot_color)
        painter.drawEllipse(dot_center, dot_radius, dot_radius)

        painter.end()
        icon.addPixmap(pixmap)

    return icon


class ToggleSwitch(QAbstractButton):
    """Interruptor (toggle) estilo moderno Windows 11."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(44, 24)
        self._tokens = get_tokens(True)

    def set_tokens(self, tokens: ThemeTokens) -> None:
        self._tokens = tokens
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        checked = self.isChecked()
        c = self._tokens.colors

        # Trilho
        track_color = QColor(c.accent if checked else c.border)
        if not checked and self.underMouse():
            track_color = QColor(c.border_subtle)

        track_rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        radius = track_rect.height() / 2.0

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track_color)
        painter.drawRoundedRect(track_rect, radius, radius)

        # Botão deslizante (thumb)
        thumb_diameter = self.height() - 6.0
        x_pos = self.width() - thumb_diameter - 3.0 if checked else 3.0
        thumb_rect = QRectF(x_pos, 3.0, thumb_diameter, thumb_diameter)

        painter.setBrush(QColor("#FFFFFF"))
        painter.drawEllipse(thumb_rect)

        painter.end()


class SidebarItem(QFrame):
    clicked = Signal(str)

    def __init__(
        self,
        item_id: str,
        label: str,
        icon_type: str,
        tokens: ThemeTokens,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.item_id = item_id
        self.label = label
        self.icon_type = icon_type
        self._tokens = tokens
        self._active = False
        self._hover = False

        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(40)

    def set_tokens(self, tokens: ThemeTokens) -> None:
        self._tokens = tokens
        self.update()

    def set_active(self, active: bool) -> None:
        self._active = active
        self.update()

    def enterEvent(self, event: QEvent) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.item_id)
        super().mousePressEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        c = self._tokens.colors
        w, h = self.width(), self.height()

        # Fundo do item
        if self._active:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(c.accent_subtle))
            rect = QRectF(4, 2, w - 8, h - 4)
            painter.drawRoundedRect(rect, 8, 8)

            # Barra de destaque à esquerda
            painter.setBrush(QColor(c.accent))
            bar = QRectF(4, 8, 3.5, h - 16)
            painter.drawRoundedRect(bar, 1.75, 1.75)

        elif self._hover:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(c.surface_hover))
            rect = QRectF(4, 2, w - 8, h - 4)
            painter.drawRoundedRect(rect, 8, 8)

        # Ícone
        icon_color = QColor(c.accent if self._active else (c.text if self._hover else c.text_secondary))
        draw_icon(painter, self.icon_type, 18, (h - 18) / 2.0, 18, icon_color)

        # Texto
        font = QFont("Segoe UI Variable", 13, QFont.Weight.DemiBold if self._active else QFont.Weight.Normal)
        painter.setFont(font)
        painter.setPen(icon_color)
        text_rect = QRectF(48, 0, w - 56, h)
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.label)

        painter.end()


class Sidebar(QFrame):
    page_changed = Signal(str)

    def __init__(self, tokens: ThemeTokens, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tokens = tokens
        self.setFixedWidth(210)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(12, 20, 12, 16)
        self._layout.setSpacing(4)

        # Cabeçalho / Título do App
        self.app_title = QLabel("SteamShotBridge")
        self.app_title.setFont(QFont("Segoe UI Variable", 15, QFont.Weight.Bold))
        self.app_title.setStyleSheet("padding-left: 8px; margin-bottom: 12px;")
        self._layout.addWidget(self.app_title)

        # Itens de navegação
        self.items: dict[str, SidebarItem] = {}
        for item_id, label, icon_type in (
            ("activity", "Atividade", "activity"),
            ("games", "Jogos", "games"),
            ("pending", "Pendências", "pending"),
            ("settings", "Configurações", "settings"),
        ):
            item = SidebarItem(item_id, label, icon_type, self._tokens, self)
            item.clicked.connect(self._on_item_clicked)
            self.items[item_id] = item
            self._layout.addWidget(item)

        self._layout.addStretch()

        # Rodapé: Indicador de estado (bolinha colorida + texto)
        self.status_container = QFrame(self)
        status_layout = QHBoxLayout(self.status_container)
        status_layout.setContentsMargins(8, 8, 8, 8)
        status_layout.setSpacing(10)

        self.status_dot = QLabel()
        self.status_dot.setFixedSize(10, 10)
        self.status_label = QLabel("Monitorando")
        self.status_label.setProperty("secondary", "true")

        status_layout.addWidget(self.status_dot)
        status_layout.addWidget(self.status_label)
        status_layout.addStretch()

        self._layout.addWidget(self.status_container)

        self.set_active("activity")
        self.set_status("active", "Monitorando")

    def set_tokens(self, tokens: ThemeTokens) -> None:
        self._tokens = tokens
        c = tokens.colors
        self.setStyleSheet(
            f"background-color: {c.surface}; border-right: 1px solid {c.border};"
        )
        for item in self.items.values():
            item.set_tokens(tokens)
        self.update_status_dot()

    def set_active(self, active_id: str) -> None:
        for item_id, item in self.items.items():
            item.set_active(item_id == active_id)

    def _on_item_clicked(self, item_id: str) -> None:
        self.set_active(item_id)
        self.page_changed.emit(item_id)

    def set_status(self, state: str, text: str) -> None:
        self._status_state = state
        self.status_label.setText(text)
        self.update_status_dot()

    def update_status_dot(self) -> None:
        c = self._tokens.colors
        if getattr(self, "_status_state", "active") == "active":
            color = c.status_active
        elif self._status_state == "warning":
            color = c.status_warning
        elif self._status_state == "error":
            color = c.status_error
        else:
            color = c.status_paused
        self.status_dot.setStyleSheet(
            f"background-color: {color}; border-radius: 5px;"
        )


class MetricCard(QFrame):
    def __init__(
        self,
        label: str,
        value: str = "0",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("card", "true")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(92)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(2)

        self.lbl_value = QLabel(value)
        self.lbl_value.setProperty("metricValue", "true")

        self.lbl_title = QLabel(label)
        self.lbl_title.setProperty("metricLabel", "true")

        layout.addWidget(self.lbl_value)
        layout.addWidget(self.lbl_title)

    def set_value(self, value: str | int) -> None:
        self.lbl_value.setText(str(value))


class EmptyState(QFrame):
    def __init__(
        self,
        title: str,
        subtitle: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("card", "true")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 48, 32, 48)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.lbl_title = QLabel(title)
        self.lbl_title.setProperty("subheading", "true")
        self.lbl_title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.lbl_subtitle = QLabel(subtitle)
        self.lbl_subtitle.setProperty("secondary", "true")
        self.lbl_subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_subtitle.setWordWrap(True)

        layout.addWidget(self.lbl_title)
        if subtitle:
            layout.addWidget(self.lbl_subtitle)


class GameCard(QFrame):
    clicked = Signal(object)  # Game

    def __init__(
        self,
        game: Game,
        screenshots_count: int,
        is_provisional: bool,
        tokens: ThemeTokens,
        steam_userdata_dir: Path | str | None = None,
        cover_pixmap: QPixmap | None = None,
        cover_source: str = "placeholder",
        is_ignored: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.game = game
        self.screenshots_count = screenshots_count
        self.is_provisional = is_provisional
        self.is_ignored = is_ignored
        self._tokens = tokens
        self.steam_userdata_dir = steam_userdata_dir
        self._hover = False
        self._cover_source = cover_source

        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(140, 240)
        self.setProperty("card", "true")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        # Capa em proporção 2:3 (120x180 px)
        self.cover_label = QLabel(self)
        self.cover_label.setFixedSize(120, 180)
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if cover_pixmap is not None:
            self.set_cover(cover_pixmap, cover_source)
        else:
            self.set_cover(
                generate_placeholder_cover(
                    game.game_id, game.name, 120, 180, is_dark=tokens.is_dark
                ),
                cover_source,
            )

        # Nome do jogo
        self.title_label = QLabel(game.name, self)
        self.title_label.setFont(QFont("Segoe UI Variable", 11, QFont.Weight.DemiBold))
        self.title_label.setWordWrap(False)

        # Linha secundária com N screenshots e selo provisório / excluído
        footer = QHBoxLayout()
        footer.setSpacing(4)
        count_str = f"{screenshots_count} foto{'s' if screenshots_count != 1 else ''}"
        self.count_label = QLabel(count_str, self)
        self.count_label.setProperty("secondary", "true")
        footer.addWidget(self.count_label)
        footer.addStretch()

        if is_ignored:
            ignored_badge = QLabel("excluído", self)
            ignored_badge.setStyleSheet(
                "background-color: rgba(239, 68, 68, 0.18); color: #EF4444; "
                "border-radius: 4px; padding: 1px 4px; font-size: 9px; font-weight: 600;"
            )
            footer.addWidget(ignored_badge)
        elif is_provisional:
            prov_badge = QLabel("provisório", self)
            prov_badge.setStyleSheet(
                "background-color: rgba(245, 158, 11, 0.18); color: #F59E0B; "
                "border-radius: 4px; padding: 1px 4px; font-size: 9px; font-weight: 600;"
            )
            footer.addWidget(prov_badge)
        elif game.kind == "steam" or game.game_id <= 0xFFFFFFFF:
            steam_badge = QLabel("Steam", self)
            steam_badge.setStyleSheet(
                "background-color: rgba(108, 124, 255, 0.18); color: #6C7CFF; "
                "border-radius: 4px; padding: 1px 4px; font-size: 9px; font-weight: 600;"
            )
            footer.addWidget(steam_badge)

        layout.addWidget(self.cover_label)
        layout.addWidget(self.title_label)
        layout.addLayout(footer)

    def set_name(self, name: str) -> None:
        self.title_label.setText(name)
        tip = f"{name}\nCapa: {self._cover_source}"
        self.setToolTip(tip)

    def set_tokens(self, tokens: ThemeTokens) -> None:
        self._tokens = tokens
        self.update_style()

    def set_cover(self, pixmap: QPixmap, source: str) -> None:
        self._cover_source = source
        self.cover_label.setPixmap(pixmap)
        tip = f"{self.game.name}\nCapa: {source}"
        self.setToolTip(tip)
        self.cover_label.setToolTip(f"Capa: {source}")

    def enterEvent(self, event: QEvent) -> None:
        self._hover = True
        self.update_style()
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self._hover = False
        self.update_style()
        super().leaveEvent(event)

    def update_style(self) -> None:
        c = self._tokens.colors
        if self._hover:
            self.setStyleSheet(
                f"GameCard {{ background-color: {c.surface_hover}; border: 1.5px solid {c.accent}; border-radius: 12px; }}"
            )
        else:
            self.setStyleSheet(
                f"GameCard {{ background-color: {c.surface}; border: 1px solid {c.border}; border-radius: 12px; }}"
            )

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.game)
        super().mousePressEvent(event)


class GameDeleteConfirmDialog(QDialog):
    def __init__(
        self,
        game_name: str,
        screenshots_count: int | None = None,
        parent: QWidget | None = None,
        *,
        game_id: int | None = None,
        shortcut_exists: bool | None = None,
        normal_count: int | None = None,
        published_count: int = 0,
        steam_running: bool | None = None,
    ) -> None:
        super().__init__(parent)
        self.game_name = game_name
        self.game_id = game_id
        self.shortcut_exists = shortcut_exists
        self.normal_count = (
            int(screenshots_count or 0)
            if normal_count is None
            else int(normal_count)
        )
        self.published_count = int(published_count)
        self.setWindowTitle("Excluir jogo")
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        lbl_title = QLabel(f"Excluir '{game_name}'?")
        lbl_title.setProperty("subheading", "true")
        layout.addWidget(lbl_title)

        if game_id is not None:
            lbl_id = QLabel(f"Game ID: {game_id}")
            lbl_id.setProperty("secondary", "true")
            layout.addWidget(lbl_id)

        if shortcut_exists is not None:
            shortcut_text = (
                "Atalho na Steam: será removido"
                if shortcut_exists
                else "Atalho na Steam: já não existe"
            )
            lbl_shortcut = QLabel(shortcut_text)
            lbl_shortcut.setProperty("secondary", "true")
            layout.addWidget(lbl_shortcut)

        lbl_counts = QLabel(
            f"Screenshots normais: {self.normal_count} | "
            f"publicadas: {self.published_count}"
        )
        lbl_counts.setProperty("secondary", "true")
        layout.addWidget(lbl_counts)

        lbl_desc = QLabel(
            "O atalho será removido da Steam e o jogo será marcado como ignorado."
        )
        lbl_desc.setProperty("secondary", "true")
        lbl_desc.setWordWrap(True)
        layout.addWidget(lbl_desc)

        self.chk_screenshots = QCheckBox(
            "Também remover as screenshots "
            f"({self.normal_count} normal(is)) — movidas para a lixeira do app"
        )
        self.chk_screenshots.setChecked(False)
        layout.addWidget(self.chk_screenshots)

        self.chk_published = QCheckBox(
            "Remover também as já publicadas do índice local "
            f"({self.published_count})"
        )
        self.chk_published.setChecked(False)
        self.chk_published.setEnabled(self.published_count > 0)
        layout.addWidget(self.chk_published)
        self.chk_include_published = self.chk_published

        lbl_published_note = QLabel(
            "Isso NÃO apaga as screenshots da sua página da comunidade Steam. "
            "Para isso, use a página de screenshots do seu perfil."
        )
        lbl_published_note.setProperty("secondary", "true")
        lbl_published_note.setWordWrap(True)
        layout.addWidget(lbl_published_note)

        self.lbl_published_warning = QLabel(PUBLISHED_PRESERVED_WARNING)
        self.lbl_published_warning.setWordWrap(True)
        self.lbl_published_warning.setStyleSheet("color: #F59E0B; font-weight: 500;")
        self.lbl_published_warning.setVisible(self.published_count > 0)
        layout.addWidget(self.lbl_published_warning)
        self.chk_published.toggled.connect(
            lambda checked: self.lbl_published_warning.setVisible(
                self.published_count > 0 and not checked
            )
        )

        running = is_steam_running() if steam_running is None else steam_running
        self.steam_running = bool(running)
        if running:
            banner = QFrame()
            banner.setProperty("banner", "warning")
            b_layout = QHBoxLayout(banner)
            b_layout.setContentsMargins(12, 10, 12, 10)
            lbl_banner = QLabel(
                "A Steam está aberta: a remoção será aplicada quando a Steam fechar."
            )
            lbl_banner.setWordWrap(True)
            b_layout.addWidget(lbl_banner)
            layout.addWidget(banner)

        btn_box = QHBoxLayout()
        btn_box.setSpacing(12)
        btn_box.addStretch()

        self.btn_cancel = QPushButton("Cancelar")
        self.btn_cancel.setDefault(True)
        self.btn_cancel.clicked.connect(self.reject)

        self.btn_confirm = QPushButton("Excluir")
        self.btn_confirm.setProperty("danger", "true")
        self.btn_confirm.clicked.connect(self.accept)

        btn_box.addWidget(self.btn_cancel)
        btn_box.addWidget(self.btn_confirm)
        layout.addLayout(btn_box)

    def should_remove_screenshots(self) -> bool:
        return self.chk_screenshots.isChecked()

    def should_include_published(self) -> bool:
        return self.chk_published.isChecked()


class GameDetailsDialog(QDialog):
    action_requested = Signal(str, object)  # (action_name, game)

    def __init__(
        self,
        game: Game,
        screenshots_count: int,
        is_provisional: bool,
        tokens: ThemeTokens,
        cover_pixmap: QPixmap | None = None,
        cover_source: str = "placeholder",
        steam_userdata_dir: Path | str | None = None,
        is_ignored: bool = False,
        is_app_managed: bool = True,
        removal_info: RemovalInfo | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.game = game
        self.screenshots_count = screenshots_count
        self.is_provisional = is_provisional
        self.is_ignored = is_ignored
        self.is_app_managed = is_app_managed
        self.removal_info = removal_info
        self._tokens = tokens
        self.steam_userdata_dir = steam_userdata_dir
        self.setWindowTitle(f"Detalhes: {game.name}")
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        # Cabeçalho com capa e informações
        header = QHBoxLayout()
        header.setSpacing(16)

        self.cover_label = QLabel()
        self.cover_label.setFixedSize(100, 150)
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if cover_pixmap is not None:
            self.set_cover(cover_pixmap, cover_source)
        else:
            self.set_cover(
                generate_placeholder_cover(
                    game.game_id, game.name, 100, 150, is_dark=tokens.is_dark
                ),
                cover_source,
            )
        header.addWidget(self.cover_label)

        info_layout = QVBoxLayout()
        info_layout.setSpacing(4)
        self.lbl_name = QLabel(game.name)
        self.lbl_name.setProperty("subheading", "true")

        lbl_id = QLabel(f"Game ID: {game.game_id}")
        lbl_id.setProperty("secondary", "true")

        lbl_kind = QLabel(f"Tipo: {game.kind}")
        lbl_kind.setProperty("secondary", "true")

        lbl_count = QLabel(f"Screenshots: {screenshots_count}")
        lbl_count.setProperty("secondary", "true")

        info_layout.addWidget(self.lbl_name)
        info_layout.addWidget(lbl_id)
        info_layout.addWidget(lbl_kind)
        info_layout.addWidget(lbl_count)
        if is_ignored:
            ign = QLabel("Status: Excluído (ignorado)")
            ign.setStyleSheet("color: #EF4444; font-weight: 500;")
            info_layout.addWidget(ign)
        elif is_provisional:
            prov = QLabel("Status: Título provisório")
            prov.setStyleSheet("color: #F59E0B; font-weight: 500;")
            info_layout.addWidget(prov)
        info_layout.addStretch()

        header.addLayout(info_layout)
        layout.addLayout(header)

        # Botões de Ação
        btn_layout = QVBoxLayout()
        btn_layout.setSpacing(8)

        is_steam = game.kind == "steam" or not can_remove_game(game.game_id)

        self.btn_rename = QPushButton("Renomear jogo...")
        if is_steam:
            self.btn_rename.setEnabled(False)
            self.btn_rename.setToolTip("Jogo da Steam: o nome vem da própria Steam")
        else:
            self.btn_rename.clicked.connect(lambda: self.action_requested.emit("rename", self.game))

        self.btn_resolve = QPushButton("Reidentificar nome")
        if is_steam:
            self.btn_resolve.setEnabled(False)
            self.btn_resolve.setToolTip("Jogo da Steam: o nome vem da própria Steam")
        else:
            self.btn_resolve.clicked.connect(lambda: self.action_requested.emit("resolve", self.game))

        self.btn_refresh_cover = QPushButton("Atualizar capa")
        self.btn_refresh_cover.clicked.connect(lambda: self.action_requested.emit("refresh_cover", self.game))

        self.btn_cover = QPushButton("Usar outra capa...")
        self.btn_cover.clicked.connect(self._on_choose_cover)

        btn_layout.addWidget(self.btn_rename)
        btn_layout.addWidget(self.btn_resolve)
        btn_layout.addWidget(self.btn_refresh_cover)
        btn_layout.addWidget(self.btn_cover)

        if is_ignored:
            self.btn_restore = QPushButton("Restaurar jogo")
            self.btn_restore.clicked.connect(lambda: self.action_requested.emit("restore", self.game))
            btn_layout.addWidget(self.btn_restore)

        self.btn_delete = QPushButton("Excluir jogo...")
        self.btn_delete.setProperty("danger", "true")
        if not can_remove_game(game.game_id):
            self.btn_delete.setEnabled(False)
            self.btn_delete.setToolTip(
                "Jogos da Steam são protegidos e não podem ser excluídos."
            )
        else:
            self.btn_delete.clicked.connect(self._on_delete_clicked)
        btn_layout.addWidget(self.btn_delete)

        layout.addLayout(btn_layout)

        # Botão fechar
        bottom = QHBoxLayout()
        bottom.addStretch()
        btn_close = QPushButton("Fechar")
        btn_close.setProperty("primary", "true")
        btn_close.clicked.connect(self.accept)
        bottom.addWidget(btn_close)
        layout.addLayout(bottom)

    def _on_delete_clicked(self) -> None:
        info = self.removal_info
        dialog = GameDeleteConfirmDialog(
            game_name=self.game.name,
            screenshots_count=self.screenshots_count,
            parent=self,
            game_id=self.game.game_id,
            shortcut_exists=info.shortcut_exists if info is not None else None,
            normal_count=info.normal_count if info is not None else None,
            published_count=info.published_count if info is not None else 0,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.action_requested.emit(
                "delete",
                (
                    self.game,
                    dialog.should_remove_screenshots(),
                    dialog.should_include_published(),
                ),
            )
            self.accept()

    def set_name(self, name: str) -> None:
        self.setWindowTitle(f"Detalhes: {name}")
        self.lbl_name.setText(name)

    def set_cover(self, pixmap: QPixmap, source: str) -> None:
        self.cover_label.setPixmap(pixmap)
        self.cover_label.setToolTip(f"Capa: {source}")

    def _on_choose_cover(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Selecione uma imagem de capa",
            "",
            "Imagens (*.png *.jpg *.jpeg *.bmp)",
        )
        if file_path:
            set_custom_cover(self.game.game_id, file_path)
            self.action_requested.emit("cover_updated", self.game)
