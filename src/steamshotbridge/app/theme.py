from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication


class ThemeMode(str, Enum):
    AUTO = "auto"
    DARK = "dark"
    LIGHT = "light"


@dataclass(frozen=True)
class ColorTokens:
    bg: str
    surface: str
    surface_hover: str
    surface_active: str
    border: str
    border_subtle: str
    text: str
    text_secondary: str
    text_tertiary: str
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_subtle: str
    # Cores de estado discretas
    status_active: str
    status_warning: str
    status_error: str
    status_paused: str


DARK_COLORS = ColorTokens(
    bg="#0F1115",
    surface="#171A21",
    surface_hover="#1E222D",
    surface_active="#252B3A",
    border="#232733",
    border_subtle="#1C202B",
    text="#E8EAF0",
    text_secondary="#8B91A1",
    text_tertiary="#5B6171",
    accent="#6C7CFF",
    accent_hover="#7D8CFF",
    accent_pressed="#5B6AF0",
    accent_subtle="rgba(108, 124, 255, 0.12)",
    status_active="#10B981",
    status_warning="#F59E0B",
    status_error="#EF4444",
    status_paused="#6B7280",
)

LIGHT_COLORS = ColorTokens(
    bg="#F5F6F8",
    surface="#FFFFFF",
    surface_hover="#F9FAFC",
    surface_active="#EEF1F6",
    border="#E3E5EA",
    border_subtle="#EBECEF",
    text="#14161B",
    text_secondary="#6B7280",
    text_tertiary="#9CA3AF",
    accent="#4F5BD5",
    accent_hover="#5F6BE5",
    accent_pressed="#404CBF",
    accent_subtle="rgba(79, 91, 213, 0.10)",
    status_active="#10B981",
    status_warning="#F59E0B",
    status_error="#EF4444",
    status_paused="#6B7280",
)


@dataclass(frozen=True)
class ThemeTokens:
    is_dark: bool
    colors: ColorTokens
    radius_card: int = 12
    radius_control: int = 8
    radius_badge: int = 6
    radius_pill: int = 16
    font_family: str = '"Segoe UI Variable", "Segoe UI", -apple-system, sans-serif'
    font_size_title: int = 20
    font_size_subtitle: int = 15
    font_size_body: int = 13
    font_size_caption: int = 11


def get_tokens(is_dark: bool) -> ThemeTokens:
    return ThemeTokens(
        is_dark=is_dark,
        colors=DARK_COLORS if is_dark else LIGHT_COLORS,
    )


def generate_qss(tokens: ThemeTokens) -> str:
    c = tokens.colors
    r_card = tokens.radius_card
    r_ctrl = tokens.radius_control
    ff = tokens.font_family

    return f"""
    * {{
        font-family: {ff};
        font-size: {tokens.font_size_body}px;
        color: {c.text};
        outline: none;
    }}

    QMainWindow, QDialog {{
        background-color: {c.bg};
    }}

    QWidget#centralWidget {{
        background-color: {c.bg};
    }}

    /* Cartões e superfícies */
    QFrame[card="true"] {{
        background-color: {c.surface};
        border: 1px solid {c.border};
        border-radius: {r_card}px;
    }}

    QFrame[card="subtle"] {{
        background-color: {c.surface_hover};
        border: 1px solid {c.border_subtle};
        border-radius: {r_ctrl}px;
    }}

    /* Rótulos e textos */
    QLabel {{
        background: transparent;
        color: {c.text};
    }}

    QLabel[heading="true"] {{
        font-size: {tokens.font_size_title}px;
        font-weight: 600;
        color: {c.text};
    }}

    QLabel[subheading="true"] {{
        font-size: {tokens.font_size_subtitle}px;
        font-weight: 600;
        color: {c.text};
    }}

    QLabel[secondary="true"] {{
        font-size: {tokens.font_size_caption}px;
        color: {c.text_secondary};
    }}

    QLabel[metricValue="true"] {{
        font-size: 28px;
        font-weight: 700;
        color: {c.text};
    }}

    QLabel[metricLabel="true"] {{
        font-size: 12px;
        font-weight: 500;
        color: {c.text_secondary};
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }}

    /* Botões */
    QPushButton {{
        background-color: {c.surface};
        color: {c.text};
        border: 1px solid {c.border};
        border-radius: {r_ctrl}px;
        padding: 7px 16px;
        font-size: {tokens.font_size_body}px;
        font-weight: 500;
    }}

    QPushButton:hover {{
        background-color: {c.surface_hover};
        border-color: {c.border};
    }}

    QPushButton:pressed {{
        background-color: {c.surface_active};
    }}

    QPushButton:focus {{
        border: 1px solid {c.accent};
    }}

    QPushButton[primary="true"] {{
        background-color: {c.accent};
        color: #FFFFFF;
        border: 1px solid {c.accent};
        font-weight: 600;
    }}

    QPushButton[primary="true"]:hover {{
        background-color: {c.accent_hover};
        border-color: {c.accent_hover};
    }}

    QPushButton[primary="true"]:pressed {{
        background-color: {c.accent_pressed};
        border-color: {c.accent_pressed};
    }}

    QPushButton[danger="true"] {{
        background-color: transparent;
        color: {c.status_error};
        border: 1px solid {c.status_error};
    }}

    QPushButton[danger="true"]:hover {{
        background-color: rgba(239, 68, 68, 0.12);
    }}

    QPushButton[iconButton="true"] {{
        padding: 6px;
        border-radius: {r_ctrl}px;
    }}

    /* Campos de entrada */
    QLineEdit, QTextEdit, QPlainTextEdit {{
        background-color: {c.surface};
        color: {c.text};
        border: 1px solid {c.border};
        border-radius: {r_ctrl}px;
        padding: 8px 12px;
        selection-background-color: {c.accent};
        selection-color: #FFFFFF;
    }}

    QLineEdit:hover, QTextEdit:hover {{
        border-color: {c.border};
        background-color: {c.surface_hover};
    }}

    QLineEdit:focus, QTextEdit:focus {{
        border: 1px solid {c.accent};
        background-color: {c.surface};
    }}

    QLineEdit[invalid="true"] {{
        border: 1px solid {c.status_error};
    }}

    /* Combobox */
    QComboBox {{
        background-color: {c.surface};
        color: {c.text};
        border: 1px solid {c.border};
        border-radius: {r_ctrl}px;
        padding: 7px 12px;
        min-width: 120px;
    }}

    QComboBox:hover {{
        background-color: {c.surface_hover};
    }}

    QComboBox:focus {{
        border: 1px solid {c.accent};
    }}

    QComboBox::drop-down {{
        subcontrol-origin: padding;
        subcontrol-position: top right;
        width: 24px;
        border: none;
    }}

    QComboBox QAbstractItemView {{
        background-color: {c.surface};
        color: {c.text};
        border: 1px solid {c.border};
        border-radius: {r_ctrl}px;
        padding: 4px;
        selection-background-color: {c.accent_subtle};
        selection-color: {c.accent};
    }}

    /* Tabelas e Listas */
    QTableWidget, QTableView {{
        background-color: {c.surface};
        border: 1px solid {c.border};
        border-radius: {r_card}px;
        gridline-color: {c.border_subtle};
        selection-background-color: {c.accent_subtle};
        selection-color: {c.text};
    }}

    QHeaderView::section {{
        background-color: {c.surface};
        color: {c.text_secondary};
        font-weight: 600;
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        border: none;
        border-bottom: 1px solid {c.border};
        padding: 10px 12px;
    }}

    QTableWidget::item {{
        padding: 10px 12px;
        border-bottom: 1px solid {c.border_subtle};
    }}

    QTableWidget::item:hover {{
        background-color: {c.surface_hover};
    }}

    QTableWidget::item:selected {{
        background-color: {c.accent_subtle};
        color: {c.text};
    }}

    /* Scrollbars finas e discretas */
    QScrollBar:vertical {{
        border: none;
        background: transparent;
        width: 6px;
        margin: 4px 1px 4px 1px;
    }}

    QScrollBar::handle:vertical {{
        background-color: {c.border};
        min-height: 24px;
        border-radius: 3px;
    }}

    QScrollBar::handle:vertical:hover {{
        background-color: {c.text_tertiary};
    }}

    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        border: none;
        background: none;
        height: 0px;
    }}

    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
        background: none;
    }}

    QScrollBar:horizontal {{
        border: none;
        background: transparent;
        height: 6px;
        margin: 1px 4px 1px 4px;
    }}

    QScrollBar::handle:horizontal {{
        background-color: {c.border};
        min-width: 24px;
        border-radius: 3px;
    }}

    QScrollBar::handle:horizontal:hover {{
        background-color: {c.text_tertiary};
    }}

    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
        border: none;
        background: none;
        width: 0px;
    }}

    QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
        background: none;
    }}

    /* Tooltips */
    QToolTip {{
        background-color: {c.surface};
        color: {c.text};
        border: 1px solid {c.border};
        border-radius: {tokens.radius_badge}px;
        padding: 6px 10px;
        font-size: {tokens.font_size_caption}px;
    }}

    /* Faixa de aviso/alerta */
    QFrame[banner="warning"] {{
        background-color: rgba(245, 158, 11, 0.12);
        border: 1px solid rgba(245, 158, 11, 0.35);
        border-radius: {r_ctrl}px;
    }}
    """


class ThemeManager(QObject):
    theme_changed = Signal(object, bool)  # (ThemeTokens, is_dark)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._mode = ThemeMode.AUTO
        self._is_dark = self._detect_windows_dark_mode()

        app = QGuiApplication.instance()
        if app is not None:
            hints = app.styleHints()
            if hasattr(hints, "colorSchemeChanged"):
                hints.colorSchemeChanged.connect(self._on_system_scheme_changed)

    def _detect_windows_dark_mode(self) -> bool:
        app = QGuiApplication.instance()
        if app is not None:
            hints = app.styleHints()
            if hasattr(hints, "colorScheme"):
                return hints.colorScheme() == Qt.ColorScheme.Dark
        return True

    def _on_system_scheme_changed(self, scheme: Qt.ColorScheme) -> None:
        if self._mode == ThemeMode.AUTO:
            is_dark = scheme == Qt.ColorScheme.Dark
            if is_dark != self._is_dark:
                self._is_dark = is_dark
                self.theme_changed.emit(self.tokens, self._is_dark)

    @property
    def mode(self) -> ThemeMode:
        return self._mode

    @property
    def is_dark(self) -> bool:
        return self._is_dark

    @property
    def tokens(self) -> ThemeTokens:
        return get_tokens(self._is_dark)

    def set_mode(self, mode: ThemeMode | str) -> None:
        if isinstance(mode, str):
            try:
                mode = ThemeMode(mode.lower())
            except ValueError:
                mode = ThemeMode.AUTO

        self._mode = mode
        if mode == ThemeMode.DARK:
            is_dark = True
        elif mode == ThemeMode.LIGHT:
            is_dark = False
        else:
            is_dark = self._detect_windows_dark_mode()

        changed = is_dark != self._is_dark
        self._is_dark = is_dark
        self.theme_changed.emit(self.tokens, self._is_dark)

    def apply_to_app(self) -> None:
        app = QGuiApplication.instance()
        if app is not None:
            qss = generate_qss(self.tokens)
            app.setStyleSheet(qss)
