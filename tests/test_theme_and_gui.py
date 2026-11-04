from __future__ import annotations

import os
from pathlib import Path

import pytest
from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from steamshotbridge.app.theme import (
    DARK_COLORS,
    LIGHT_COLORS,
    ThemeManager,
    ThemeMode,
    generate_qss,
    get_tokens,
)
from steamshotbridge.app.widgets import (
    GameCard,
    Sidebar,
    ToggleSwitch,
    create_tray_icon,
)
from steamshotbridge.app.window import MainWindow
from steamshotbridge.games import Game


BASE_VDF = '''"screenshots"
{
    "shortcutnames"
    {
    }
}
'''


def _setup_test_userdata(tmp_path: Path) -> Path:
    userdata = tmp_path / "Steam" / "userdata" / "12345"
    (userdata / "760" / "remote" / "730" / "screenshots").mkdir(parents=True, exist_ok=True)
    (userdata / "760" / "remote" / "730" / "screenshots" / "shot1.jpg").write_bytes(b"\xff\xd8\xff")
    (userdata / "760" / "remote" / "730" / "screenshots" / "shot2.jpg").write_bytes(b"\xff\xd8\xff")

    (userdata / "760" / "remote" / "570" / "screenshots").mkdir(parents=True, exist_ok=True)
    (userdata / "760" / "remote" / "570" / "screenshots" / "shot.jpg").write_bytes(b"\xff\xd8\xff")

    vdf_path = userdata / "760" / "screenshots.vdf"
    vdf_path.write_text(BASE_VDF, encoding="utf-8")
    return userdata


def test_theme_generates_valid_qss_in_both_modes() -> None:
    dark_tokens = get_tokens(is_dark=True)
    light_tokens = get_tokens(is_dark=False)

    qss_dark = generate_qss(dark_tokens)
    qss_light = generate_qss(light_tokens)

    assert qss_dark and len(qss_dark) > 100
    assert qss_light and len(qss_light) > 100

    # Verifica os tokens de cor especificados na direção visual
    assert "#0F1115" in qss_dark
    assert "#171A21" in qss_dark
    assert "#232733" in qss_dark
    assert "#E8EAF0" in qss_dark
    assert "#8B91A1" in qss_dark
    assert "#6C7CFF" in qss_dark

    assert "#F5F6F8" in qss_light
    assert "#FFFFFF" in qss_light
    assert "#E3E5EA" in qss_light
    assert "#14161B" in qss_light
    assert "#6B7280" in qss_light
    assert "#4F5BD5" in qss_light


def test_switch_theme_does_not_break_window(qapp: QApplication, tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    userdata = _setup_test_userdata(tmp_path)
    window = MainWindow(config_path=config, steam_userdata_dir=userdata)

    window.theme_manager.set_mode(ThemeMode.DARK)
    assert window.theme_manager.is_dark is True

    window.theme_manager.set_mode(ThemeMode.LIGHT)
    assert window.theme_manager.is_dark is False

    window.theme_manager.set_mode(ThemeMode.AUTO)
    window.close()


def test_sidebar_switches_pages(qapp: QApplication, tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    userdata = _setup_test_userdata(tmp_path)
    shots = tmp_path / "shots"
    shots.mkdir()
    config.write_text(f'{{"settings": {{"watch_folder": "{shots.as_posix()}"}}}}', encoding="utf-8")
    window = MainWindow(config_path=config, steam_userdata_dir=userdata)

    assert window.pages.currentIndex() == 0

    window.sidebar.items["games"].clicked.emit("games")
    assert window.pages.currentIndex() == 1

    window.sidebar.items["pending"].clicked.emit("pending")
    assert window.pages.currentIndex() == 2

    window.sidebar.items["settings"].clicked.emit("settings")
    assert window.pages.currentIndex() == 3

    window.sidebar.items["activity"].clicked.emit("activity")
    assert window.pages.currentIndex() == 0

    window.close()


def test_empty_states_appear_when_lists_are_empty(qapp: QApplication, tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    pending = tmp_path / "pending.json"
    empty_userdata = tmp_path / "Steam" / "userdata" / "99999"
    (empty_userdata / "760").mkdir(parents=True, exist_ok=True)
    (empty_userdata / "760" / "screenshots.vdf").write_text(BASE_VDF, encoding="utf-8")

    window = MainWindow(config_path=config, steam_userdata_dir=empty_userdata, pending_path=pending)
    window.show()
    qapp.processEvents()

    window.refresh_all()
    qapp.processEvents()

    assert not window.activity_empty.isHidden()
    assert not window.pending_empty.isHidden()
    assert not window.games_empty.isHidden()

    window.close()


def test_games_grid_creates_card_per_game(qapp: QApplication, tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    userdata = _setup_test_userdata(tmp_path)
    window = MainWindow(config_path=config, steam_userdata_dir=userdata)

    window.refresh_games()
    # Espera 2 jogos no grid layout
    cards = [
        window.grid_layout.itemAt(i).widget()
        for i in range(window.grid_layout.count())
        if isinstance(window.grid_layout.itemAt(i).widget(), GameCard)
    ]
    assert len(cards) == 2

    window.close()


def test_search_filters_games(qapp: QApplication, tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    userdata = _setup_test_userdata(tmp_path)
    window = MainWindow(config_path=config, steam_userdata_dir=userdata)

    window.refresh_games()

    # Busca por ID 730
    window.txt_search.setText("730")
    cards = [
        window.grid_layout.itemAt(i).widget()
        for i in range(window.grid_layout.count())
        if isinstance(window.grid_layout.itemAt(i).widget(), GameCard)
    ]
    assert len(cards) == 1
    assert "730" in cards[0].game.name

    # Busca por termo inexistente
    window.txt_search.setText("Inexistente12345")
    cards_none = [
        window.grid_layout.itemAt(i).widget()
        for i in range(window.grid_layout.count())
        if isinstance(window.grid_layout.itemAt(i).widget(), GameCard)
    ]
    assert len(cards_none) == 0
    assert not window.games_empty.isHidden()

    window.close()


def test_tray_icon_generation_in_all_states_and_sizes() -> None:
    for status in ("active", "warning", "error", "paused"):
        for is_dark in (True, False):
            icon = create_tray_icon(status=status, dark_mode=is_dark)
            assert not icon.isNull()
            for size in (16, 24, 32):
                pixmap = icon.pixmap(QSize(size, size))
                assert not pixmap.isNull()
                assert pixmap.width() == size
                assert pixmap.height() == size


def _is_image_not_blank(image_path: Path) -> bool:
    img = QImage(str(image_path))
    if img.isNull() or img.width() == 0 or img.height() == 0:
        return False
    # Checa que não é 100% transparente ou mono-cor sem conteúdo
    first_pixel = img.pixelColor(0, 0)
    has_different_pixel = False
    step_x = max(1, img.width() // 20)
    step_y = max(1, img.height() // 20)
    for y in range(0, img.height(), step_y):
        for x in range(0, img.width(), step_x):
            if img.pixelColor(x, y) != first_pixel:
                has_different_pixel = True
                break
        if has_different_pixel:
            break
    return has_different_pixel


def test_grab_screens_not_blank_in_both_themes(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.json"
    userdata = _setup_test_userdata(tmp_path)
    window = MainWindow(config_path=config, steam_userdata_dir=userdata)
    window.show()

    pages = ["activity", "games", "pending", "settings"]

    for mode in (ThemeMode.DARK, ThemeMode.LIGHT):
        window.theme_manager.set_mode(mode)
        theme_name = mode.value

        for page in pages:
            window.sidebar.items[page].clicked.emit(page)
            qapp.processEvents()

            pixmap = window.grab()
            assert not pixmap.isNull()
            assert pixmap.width() >= 960
            assert pixmap.height() >= 640

            out_file = tmp_path / f"screen_{page}_{theme_name}.png"
            saved = pixmap.save(str(out_file), "PNG")
            assert saved is True
            assert out_file.is_file()
            assert out_file.stat().st_size > 1024
            assert _is_image_not_blank(out_file) is True

    window.close()
