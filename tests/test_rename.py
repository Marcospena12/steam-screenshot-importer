from __future__ import annotations

import json
from pathlib import Path

import pytest
import vdf
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

from steamshotbridge import (
    exe_map as exe_map_module,
    importer,
    pending as pending_module,
    shortcuts as shortcuts_module,
)
from steamshotbridge.app.widgets import GameDetailsDialog
from steamshotbridge.app.window import MainWindow
from steamshotbridge.exe_map import (
    ExeMapError,
    is_manual_game_name,
    rename_game,
)
from steamshotbridge.games import GameValidationError, list_games
from steamshotbridge.pending import (
    apply_pending,
    enqueue_rename,
    load_pending,
)
from steamshotbridge.steam import shortcut_game_id, signed_32


@pytest.fixture(autouse=True)
def _steam_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importer, "is_steam_running", lambda: False)
    monkeypatch.setattr(shortcuts_module, "is_steam_running", lambda: False)
    monkeypatch.setattr(pending_module, "is_steam_running", lambda: False)
    monkeypatch.setattr(exe_map_module, "is_steam_running", lambda: False)


@pytest.fixture
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _userdata(tmp_path: Path) -> Path:
    userdata = tmp_path / "Steam" / "userdata" / "12345"
    (userdata / "760").mkdir(parents=True)
    return userdata


def _write_shortcuts(
    userdata: Path,
    entries: list[dict[str, object]],
) -> Path:
    path = userdata / "config" / "shortcuts.vdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        vdf.binary_dumps(
            {
                "shortcuts": {
                    str(index): entry
                    for index, entry in enumerate(entries)
                }
            }
        )
    )
    return path


def _write_screenshots(
    userdata: Path,
    shortcutnames: dict[int, str],
) -> Path:
    path = userdata / "760" / "screenshots.vdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ['"screenshots"', "{", '    "shortcutnames"', "    {"]
    for game_id, name in shortcutnames.items():
        escaped = name.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'        "{game_id}"\t\t"{escaped}"')
    lines.extend(["    }", "}", ""])
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _write_config(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _read_config(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _shortcut_entry(game_id: int, name: str) -> dict[str, object]:
    appid32 = (game_id >> 32) & 0xFFFFFFFF
    return {
        "appid": signed_32(appid32),
        "AppName": name,
        "tags": {},
    }


def test_rename_custom_game_keeps_game_id_and_updates_config(
    tmp_path: Path,
) -> None:
    userdata = _userdata(tmp_path)
    config = tmp_path / "config.json"
    pending = tmp_path / "pending.json"
    game_id = shortcut_game_id(0x70000001)
    _write_config(
        config,
        {
            "custom_games": [
                {"name": "Nome antigo", "game_id": str(game_id)},
            ]
        },
    )
    vdf_path = _write_screenshots(userdata, {game_id: "Nome antigo"})

    result = rename_game(
        game_id,
        "  Nome   novo  ",
        userdata,
        config,
        pending,
        steam_running_fn=lambda: False,
    )

    assert result.status == "aplicado"
    assert result.game_id == game_id
    assert result.name == "Nome novo"
    assert result.custom_updated is True
    assert result.exe_map_updated is False
    assert result.name_override_saved is False

    data = _read_config(config)
    assert data["custom_games"] == [
        {"name": "Nome novo", "game_id": str(game_id)},
    ]
    assert "name_overrides" not in data

    games = list_games(userdata, config)
    assert [(game.game_id, game.name) for game in games] == [
        (game_id, "Nome novo"),
    ]
    assert "Nome novo" in vdf_path.read_text(encoding="utf-8")
    assert list(vdf_path.parent.glob("screenshots.vdf.bak_*"))


def test_name_overrides_have_priority_over_other_sources(
    tmp_path: Path,
) -> None:
    userdata = _userdata(tmp_path)
    config = tmp_path / "config.json"
    game_id = shortcut_game_id(0x70000000)
    _write_config(
        config,
        {
            "custom_games": [
                {"name": "Custom", "game_id": str(game_id)},
            ],
            "exe_map": {
                "game.exe": {
                    "name": "Exe",
                    "game_id": str(game_id),
                },
            },
            "name_overrides": {str(game_id): "Override"},
        },
    )
    _write_screenshots(userdata, {game_id: "Shortcutname"})

    games = list_games(userdata, config)

    assert [(game.game_id, game.name) for game in games] == [
        (game_id, "Override"),
    ]


def test_rename_orphan_shortcutname_keeps_id_and_saves_override(
    tmp_path: Path,
) -> None:
    userdata = _userdata(tmp_path)
    config = tmp_path / "config.json"
    pending = tmp_path / "pending.json"
    game_id = shortcut_game_id(0x70000002)
    vdf_path = _write_screenshots(userdata, {game_id: "Antigo"})

    result = rename_game(
        game_id,
        "Novo",
        userdata,
        config,
        pending,
        steam_running_fn=lambda: False,
    )

    assert result.status == "aplicado"
    assert result.name_override_saved is True
    data = _read_config(config)
    assert data["name_overrides"] == {str(game_id): "Novo"}

    games = list_games(userdata, config)
    assert len(games) == 1
    assert games[0].game_id == game_id
    assert games[0].name == "Novo"
    assert games[0].kind == "shortcutname"
    assert "Novo" in vdf_path.read_text(encoding="utf-8")


def test_rename_manual_shortcut_updates_app_name_and_override(
    tmp_path: Path,
) -> None:
    userdata = _userdata(tmp_path)
    config = tmp_path / "config.json"
    pending = tmp_path / "pending.json"
    game_id = shortcut_game_id(0x70000003)
    shortcuts_path = _write_shortcuts(
        userdata,
        [_shortcut_entry(game_id, "Antigo")],
    )

    result = rename_game(
        game_id,
        "Novo",
        userdata,
        config,
        pending,
        steam_running_fn=lambda: False,
    )

    assert result.status == "aplicado"
    assert result.shortcut_updated is True
    assert result.shortcutname_updated is False
    loaded = vdf.binary_load(shortcuts_path.open("rb"))
    assert loaded["shortcuts"]["0"]["AppName"] == "Novo"
    assert _read_config(config)["name_overrides"] == {
        str(game_id): "Novo"
    }


def test_rename_exe_map_game_updates_all_sources(
    tmp_path: Path,
) -> None:
    userdata = _userdata(tmp_path)
    config = tmp_path / "config.json"
    pending = tmp_path / "pending.json"
    game_id = shortcut_game_id(0x70000004)
    _write_config(
        config,
        {
            "exe_map": {
                "game.exe": {
                    "exe": "C:/Games/game.exe",
                    "name": "Antigo",
                    "game_id": str(game_id),
                    "provisional": True,
                    "source": "versioninfo",
                }
            },
            "name_overrides": {str(game_id): "Override antigo"},
        },
    )
    shortcuts_path = _write_shortcuts(
        userdata,
        [_shortcut_entry(game_id, "Antigo")],
    )
    vdf_path = _write_screenshots(userdata, {game_id: "Antigo"})

    result = rename_game(
        game_id,
        "Novo",
        userdata,
        config,
        pending,
        steam_running_fn=lambda: False,
    )

    assert result.status == "aplicado"
    assert result.exe_map_updated is True
    assert result.name_override_saved is False
    assert result.shortcut_updated is True
    assert result.shortcutname_updated is True

    data = _read_config(config)
    stored = data["exe_map"]["game.exe"]
    assert stored["name"] == "Novo"
    assert stored["provisional"] is False
    assert stored["source"] == "manual"
    assert data["name_overrides"] == {}

    loaded_shortcuts = vdf.binary_load(shortcuts_path.open("rb"))
    assert loaded_shortcuts["shortcuts"]["0"]["AppName"] == "Novo"
    assert "Novo" in vdf_path.read_text(encoding="utf-8")
    assert [game.name for game in list_games(userdata, config)] == [
        "Novo"
    ]


def test_rename_with_steam_open_enqueues_without_touching_vdfs(
    tmp_path: Path,
) -> None:
    userdata = _userdata(tmp_path)
    config = tmp_path / "config.json"
    pending = tmp_path / "pending.json"
    game_id = shortcut_game_id(0x70000005)
    shortcuts_path = _write_shortcuts(
        userdata,
        [_shortcut_entry(game_id, "Antigo")],
    )
    vdf_path = _write_screenshots(userdata, {game_id: "Antigo"})
    shortcuts_before = shortcuts_path.read_bytes()
    vdf_before = vdf_path.read_bytes()

    result = rename_game(
        game_id,
        "Novo",
        userdata,
        config,
        pending,
        steam_running_fn=lambda: True,
    )

    assert result.status == "pendente"
    assert shortcuts_path.read_bytes() == shortcuts_before
    assert vdf_path.read_bytes() == vdf_before
    records = load_pending(pending)
    assert len(records) == 1
    assert records[0]["type"] == "rename"
    assert records[0]["game_name"] == "Novo"

    applied = apply_pending(
        userdata,
        pending,
        steam_running=lambda: False,
    )
    assert applied.status == "aplicado"
    assert load_pending(pending) == []
    loaded_shortcuts = vdf.binary_load(shortcuts_path.open("rb"))
    assert loaded_shortcuts["shortcuts"]["0"]["AppName"] == "Novo"
    assert "Novo" in vdf_path.read_text(encoding="utf-8")


def test_apply_rename_pending_without_screenshots_vdf(
    tmp_path: Path,
) -> None:
    userdata = _userdata(tmp_path)
    pending = tmp_path / "pending.json"
    game_id = shortcut_game_id(0x70000006)
    shortcuts_path = _write_shortcuts(
        userdata,
        [_shortcut_entry(game_id, "Antigo")],
    )
    enqueue_rename(game_id, "Novo", userdata, pending)

    result = apply_pending(
        userdata,
        pending,
        steam_running=lambda: False,
    )

    assert result.status == "aplicado"
    assert load_pending(pending) == []
    loaded = vdf.binary_load(shortcuts_path.open("rb"))
    assert loaded["shortcuts"]["0"]["AppName"] == "Novo"


def test_rename_rejects_steam_games(tmp_path: Path) -> None:
    userdata = _userdata(tmp_path)
    config = tmp_path / "config.json"

    with pytest.raises(ExeMapError, match="Steam"):
        rename_game(
            730,
            "Novo",
            userdata,
            config,
            steam_running_fn=lambda: False,
        )


@pytest.mark.parametrize("name", ["", "   ", "\x00", "A" * 121])
def test_rename_rejects_invalid_names(tmp_path: Path, name: str) -> None:
    userdata = _userdata(tmp_path)
    config = tmp_path / "config.json"
    game_id = shortcut_game_id(0x70000007)

    with pytest.raises(GameValidationError):
        rename_game(
            game_id,
            name,
            userdata,
            config,
            steam_running_fn=lambda: False,
        )

    assert not config.exists()


def test_rename_cleans_control_characters(tmp_path: Path) -> None:
    userdata = _userdata(tmp_path)
    config = tmp_path / "config.json"
    game_id = shortcut_game_id(0x70000008)

    result = rename_game(
        game_id,
        "\tNome\x00  novo\n",
        userdata,
        config,
        steam_running_fn=lambda: False,
    )

    assert result.name == "Nome novo"


def test_manual_names_are_detected_for_resolve(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    custom_id = shortcut_game_id(0x70000009)
    manual_id = shortcut_game_id(0x7000000A)
    automatic_id = shortcut_game_id(0x7000000B)
    override_id = shortcut_game_id(0x7000000C)
    _write_config(
        config,
        {
            "custom_games": [
                {"name": "Custom", "game_id": str(custom_id)},
            ],
            "exe_map": {
                "manual.exe": {
                    "name": "Manual",
                    "game_id": str(manual_id),
                    "source": "manual",
                },
                "auto.exe": {
                    "name": "Auto",
                    "game_id": str(automatic_id),
                    "source": "versioninfo",
                },
            },
            "name_overrides": {str(override_id): "Override"},
        },
    )

    assert is_manual_game_name(custom_id, config) is True
    assert is_manual_game_name(manual_id, config) is True
    assert is_manual_game_name(automatic_id, config) is False
    assert is_manual_game_name(override_id, config) is True


def test_window_rename_refreshes_grid_and_open_dialog(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    userdata = _userdata(tmp_path)
    config = tmp_path / "config.json"
    pending = tmp_path / "pending.json"
    game_id = shortcut_game_id(0x7000000D)
    _write_screenshots(userdata, {game_id: "Antigo"})
    screenshot = (
        userdata
        / "760"
        / "remote"
        / str(game_id)
        / "screenshots"
        / "shot.jpg"
    )
    screenshot.parent.mkdir(parents=True)
    screenshot.write_bytes(b"jpeg")
    monkeypatch.setattr(
        "steamshotbridge.app.covers.CoverManager.request_cover",
        lambda *args, **kwargs: (QPixmap(1, 1), "placeholder"),
    )

    window = MainWindow(
        config_path=config,
        steam_userdata_dir=userdata,
        pending_path=pending,
    )
    window.refresh_games()
    game = next(
        game
        for (game, *_) in window._games_cache
        if game.game_id == game_id
    )
    dialog = GameDetailsDialog(
        game,
        1,
        False,
        window.theme_manager.tokens,
    )

    monkeypatch.setattr(
        "steamshotbridge.app.window.QInputDialog.getText",
        lambda *args, **kwargs: ("Novo Nome", True),
    )
    messages: list[str] = []
    monkeypatch.setattr(
        "steamshotbridge.app.window.QMessageBox.information",
        lambda _parent, _title, message: messages.append(message),
    )

    window._on_game_action("rename", game, dialog)

    assert messages == ["Nome alterado"]
    assert dialog.game.game_id == game_id
    assert dialog.game.name == "Novo Nome"
    assert dialog.lbl_name.text() == "Novo Nome"
    assert dialog.lbl_status.text() == "nome aplicado"
    cached = next(
        item for item in window._games_cache if item[0].game_id == game_id
    )
    assert cached[0].name == "Novo Nome"

    window._set_view_mode("list")
    assert window._table_name_items[game_id].text() == "Novo Nome"

    monkeypatch.setattr(
        exe_map_module,
        "is_steam_running",
        lambda: True,
    )
    messages.clear()
    monkeypatch.setattr(
        "steamshotbridge.app.window.QInputDialog.getText",
        lambda *args, **kwargs: ("Nome pendente", True),
    )

    window._on_game_action("rename", dialog.game, dialog)

    assert messages == [
        "Nome alterado; será aplicado na Steam quando ela fechar"
    ]
    assert dialog.game.name == "Nome pendente"
    assert dialog.lbl_status.text() == "pendente"
    assert load_pending(pending)[0]["game_name"] == "Nome pendente"

    window.close()
