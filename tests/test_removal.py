from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import vdf
from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from steamshotbridge import (
    exe_map as exe_map_module,
    importer,
    pending as pending_module,
    removal as removal_module,
    shortcuts,
)
from steamshotbridge.app.widgets import (
    GameCard,
    GameDeleteConfirmDialog,
    GameDetailsDialog,
)
from steamshotbridge.app.window import MainWindow
from steamshotbridge.exe_map import generate_game_id, load_exe_map, resolve_exe
from steamshotbridge.games import Game, create_custom_game, list_games
from steamshotbridge.pending import apply_pending, load_pending
from steamshotbridge.removal import (
    PUBLISHED_PRESERVED_WARNING,
    GameRemovalError,
    can_remove_game,
    inspect_removal,
    is_app_managed_game,
    remove_game,
    restore_game,
)
from steamshotbridge.shortcuts import load_shortcuts_vdf, shortcut_path
from steamshotbridge.steam import shortcut_game_id, signed_32
from steamshotbridge.watcher import ProcessedRegistry, process_file
from tools.watch import main as watch_cli_main


@pytest.fixture(scope="session", autouse=True)
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def _steam_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importer, "is_steam_running", lambda: False)
    monkeypatch.setattr(shortcuts, "is_steam_running", lambda: False)
    monkeypatch.setattr(pending_module, "is_steam_running", lambda: False)
    monkeypatch.setattr(exe_map_module, "is_steam_running", lambda: False)
    monkeypatch.setattr(removal_module, "is_steam_running", lambda: False)


def _setup_userdata(tmp_path: Path) -> Path:
    userdata = tmp_path / "Steam" / "userdata" / "12345"
    (userdata / "760").mkdir(parents=True, exist_ok=True)
    vdf_path = userdata / "760" / "screenshots.vdf"
    vdf_path.write_text(
        '"screenshots"\n{\n    "shortcutnames"\n    {\n    }\n}\n',
        encoding="utf-8",
    )
    return userdata


def _create_image(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (320, 180), (10, 20, 30)).save(path, "JPEG")
    return path


# 1. RECUSA DE JOGO QUE NÃO É DO APP
def test_refuse_game_not_from_app(tmp_path: Path) -> None:
    userdata = _setup_userdata(tmp_path)
    config = tmp_path / "config.json"

    # 730 é jogo Steam
    assert is_app_managed_game(730, config) is False
    assert can_remove_game(730) is False
    with pytest.raises(GameRemovalError, match="Steam"):
        remove_game(730, userdata, config_path=config)


# 2. REMOÇÃO DO ATALHO PRESERVANDO OS DEMAIS BYTE A BYTE E APPID NEGATIVO
def test_remove_shortcut_preserves_others_byte_for_byte_and_negative_appid(
    tmp_path: Path,
) -> None:
    userdata = _setup_userdata(tmp_path)
    config = tmp_path / "config.json"

    # Cadastra jogo alvo no exe_map
    entry = resolve_exe("TargetGame", config_path=config)
    target_id = entry.game_id
    target_appid32 = (target_id >> 32) & 0xFFFFFFFF

    sc_path = shortcut_path(userdata)
    sc_path.parent.mkdir(parents=True, exist_ok=True)

    other_negative_appid = signed_32(0x80001234)
    target_signed_appid = signed_32(target_appid32)

    root = {
        "shortcuts": {
            "0": {
                "appid": other_negative_appid,
                "AppName": "Other Negative",
                "Exe": '"C:\\Other.exe"',
                "StartDir": "C:\\",
                "tags": {},
            },
            "1": {
                "appid": target_signed_appid,
                "AppName": "Target Game",
                "Exe": '"C:\\Target.exe"',
                "StartDir": "C:\\",
                "tags": {},
            },
            "2": {
                "appid": 9999,
                "AppName": "Third Game",
                "Exe": '"C:\\Third.exe"',
                "StartDir": "C:\\",
                "tags": {},
            },
        }
    }
    sc_path.write_bytes(vdf.binary_dumps(root))

    res = remove_game(target_id, userdata, config_path=config)
    assert res.status == "removido"
    assert res.shortcut_removed is True

    # Verifica shortcuts sobreviventes
    loaded = load_shortcuts_vdf(sc_path)
    shortcuts_map = loaded["shortcuts"]
    assert list(shortcuts_map.keys()) == ["0", "1"]
    assert shortcuts_map["0"]["AppName"] == "Other Negative"
    assert shortcuts_map["0"]["appid"] == other_negative_appid
    assert shortcuts_map["1"]["AppName"] == "Third Game"
    assert shortcuts_map["1"]["appid"] == 9999

    # Verifica que o backup foi criado
    assert len(list(sc_path.parent.glob("shortcuts.vdf.bak_*"))) == 1


# 3. EXE_MAP IGNORED E WATCHER IGNORANDO
def test_exe_map_ignored_and_watcher_ignores(tmp_path: Path) -> None:
    userdata = _setup_userdata(tmp_path)
    config = tmp_path / "config.json"
    pending = tmp_path / "pending.json"
    processed = ProcessedRegistry(tmp_path / "processed.json")

    entry = resolve_exe("aomx", config_path=config)
    game_id = entry.game_id

    # Exclui o jogo sem screenshots
    res = remove_game(game_id, userdata, config_path=config)
    assert res.status == "removido"

    # Verifica que exe_map tem ignored=True
    map_data = json.loads(config.read_text(encoding="utf-8"))
    assert map_data["exe_map"]["aomx"]["ignored"] is True

    # Watcher tenta processar captura do exe ignorado
    source = _create_image(tmp_path / "aomx_2026_11_03_12_42_05_932.jpg")
    outcome = process_file(
        source,
        steam_userdata_dir=userdata,
        config_path=config,
        processed=processed,
        pending_path=pending,
        stabilize_interval=0,
    )

    assert outcome.status == "ignored"
    assert outcome.lines == ("aviso: jogo ignorado: aomx",)
    assert not (userdata / "760" / "remote" / str(game_id)).exists()


# 4. CUSTOM REMOVIDO
def test_custom_game_removed_from_config(tmp_path: Path) -> None:
    userdata = _setup_userdata(tmp_path)
    config = tmp_path / "config.json"

    custom_game = create_custom_game("My Custom Game", config_path=config)
    gid = custom_game.game_id

    assert is_app_managed_game(gid, config) is True

    res = remove_game(gid, userdata, config_path=config)
    assert res.status == "removido"

    cfg_data = json.loads(config.read_text(encoding="utf-8"))
    assert len(cfg_data.get("custom_games", [])) == 0


# 5. SCREENSHOTS MOVIDAS PARA LIXEIRA E PUBLICADAS PRESERVADAS
def test_screenshots_moved_to_lixeira_and_published_preserved(
    tmp_path: Path,
) -> None:
    userdata = _setup_userdata(tmp_path)
    config = tmp_path / "config.json"
    lixeira_dir = tmp_path / "lixeira"

    entry = resolve_exe("mygame", config_path=config)
    gid = entry.game_id
    gid_str = str(gid)

    # Cria arquivos no disco
    s_dir = userdata / "760" / "remote" / gid_str / "screenshots"
    t_dir = s_dir / "thumbnails"
    _create_image(s_dir / "unpub.jpg")
    _create_image(t_dir / "unpub.jpg")
    _create_image(s_dir / "pub.jpg")
    _create_image(t_dir / "pub.jpg")

    # Configura screenshots.vdf com 1 publicada e 1 não publicada
    vdf_content = f'''"screenshots"
{{
    "{gid_str}"
    {{
        "0"
        {{
            "type" "1"
            "filename" "{gid_str}/screenshots/unpub.jpg"
            "thumbnail" "{gid_str}/screenshots/thumbnails/unpub.jpg"
            "creation" "100"
            "publishedfileid" "0"
        }}
        "1"
        {{
            "type" "1"
            "filename" "{gid_str}/screenshots/pub.jpg"
            "thumbnail" "{gid_str}/screenshots/thumbnails/pub.jpg"
            "creation" "200"
            "publishedfileid" "99887766"
        }}
    }}
    "shortcutnames"
    {{
        "{gid_str}" "My Game"
    }}
}}
'''
    vdf_file = userdata / "760" / "screenshots.vdf"
    vdf_file.write_text(vdf_content, encoding="utf-8")

    res = remove_game(
        gid,
        userdata,
        remove_screenshots=True,
        config_path=config,
        lixeira_dir=lixeira_dir,
    )

    assert res.status == "removido"
    assert res.screenshots_moved == 2  # unpub.jpg + thumb
    assert res.published_preserved == 1  # pub.jpg mantida
    assert res.published_removed == 0
    assert res.game_still_listed is True
    assert res.warning == PUBLISHED_PRESERVED_WARNING
    assert res.lixeira_path is not None
    assert res.lixeira_path.is_dir()

    # Confirma que o arquivo não publicado foi movido para a lixeira
    assert (res.lixeira_path / "screenshots" / "unpub.jpg").is_file()
    assert (res.lixeira_path / "screenshots" / "thumbnails" / "unpub.jpg").is_file()

    # Confirma que o arquivo publicado continua no disco original
    assert (s_dir / "pub.jpg").is_file()
    assert (t_dir / "pub.jpg").is_file()

    # Confirma que no screenshots.vdf sobrou apenas a entrada publicada e shortcutnames ficou
    root = vdf.loads(vdf_file.read_text(encoding="utf-8"))
    screenshots = root["screenshots"]
    assert gid_str in screenshots
    assert list(screenshots[gid_str].keys()) == ["0"]
    assert screenshots[gid_str]["0"]["publishedfileid"] == "99887766"
    assert screenshots["shortcutnames"][gid_str] == "My Game"


# 6. BLOCO E SHORTCUTNAMES REMOVIDOS SÓ QUANDO VAZIO
def test_app_block_and_shortcutnames_removed_when_no_published_entries(
    tmp_path: Path,
) -> None:
    userdata = _setup_userdata(tmp_path)
    config = tmp_path / "config.json"
    lixeira_dir = tmp_path / "lixeira"

    entry = resolve_exe("mygame", config_path=config)
    gid = entry.game_id
    gid_str = str(gid)

    s_dir = userdata / "760" / "remote" / gid_str / "screenshots"
    _create_image(s_dir / "only_unpub.jpg")

    vdf_content = f'''"screenshots"
{{
    "{gid_str}"
    {{
        "0"
        {{
            "filename" "{gid_str}/screenshots/only_unpub.jpg"
            "creation" "100"
        }}
    }}
    "shortcutnames"
    {{
        "{gid_str}" "My Game"
    }}
}}
'''
    vdf_file = userdata / "760" / "screenshots.vdf"
    vdf_file.write_text(vdf_content, encoding="utf-8")

    res = remove_game(
        gid,
        userdata,
        remove_screenshots=True,
        config_path=config,
        lixeira_dir=lixeira_dir,
    )
    assert res.published_preserved == 0

    root = vdf.loads(vdf_file.read_text(encoding="utf-8"))
    screenshots = root["screenshots"]
    assert gid_str not in screenshots
    assert gid_str not in screenshots.get("shortcutnames", {})


# 7. STEAM ABERTA -> FILA E APLICAÇÃO POSTERIOR COM UM BACKUP POR ARQUIVO
def test_steam_open_queues_and_applies_later_with_single_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    userdata = _setup_userdata(tmp_path)
    config = tmp_path / "config.json"
    pending = tmp_path / "pending.json"
    lixeira_dir = tmp_path / "lixeira"

    entry = resolve_exe("mygame", config_path=config)
    gid = entry.game_id
    gid_str = str(gid)

    s_dir = userdata / "760" / "remote" / gid_str / "screenshots"
    _create_image(s_dir / "shot.jpg")

    vdf_file = userdata / "760" / "screenshots.vdf"
    vdf_content = f'''"screenshots"
{{
    "{gid_str}"
    {{
        "0"
        {{
            "filename" "{gid_str}/screenshots/shot.jpg"
            "creation" "100"
        }}
    }}
    "shortcutnames"
    {{
        "{gid_str}" "My Game"
    }}
}}
'''
    vdf_file.write_text(vdf_content, encoding="utf-8")

    sc_file = shortcut_path(userdata)
    sc_file.parent.mkdir(parents=True, exist_ok=True)
    root_sc = {
        "shortcuts": {
            "0": {
                "appid": signed_32((gid >> 32) & 0xFFFFFFFF),
                "AppName": "My Game",
                "tags": {},
            }
        }
    }
    sc_file.write_bytes(vdf.binary_dumps(root_sc))

    vdf_before = vdf_file.read_bytes()
    sc_before = sc_file.read_bytes()

    # 1. Executa com Steam aberta e force=False
    res = remove_game(
        gid,
        userdata,
        remove_screenshots=True,
        config_path=config,
        pending_path=pending,
        force=False,
        lixeira_dir=lixeira_dir,
        steam_running_fn=lambda: True,
    )

    assert res.status == "pendente"
    assert vdf_file.read_bytes() == vdf_before
    assert sc_file.read_bytes() == sc_before
    assert len(load_pending(pending)) == 1

    # exe_map é atualizado imediatamente
    assert load_exe_map(config)["mygame"].ignored is True

    # 2. Aplica pendências com Steam fechada
    apply_res = apply_pending(
        userdata,
        pending_path=pending,
        steam_running=lambda: False,
    )

    assert apply_res.status == "aplicado"
    assert load_pending(pending) == []

    # Um único backup por arquivo gerado
    assert len(list(vdf_file.parent.glob("screenshots.vdf.bak_*"))) == 1
    assert len(list(sc_file.parent.glob("shortcuts.vdf.bak_*"))) == 1

    # Atalho e screenshots removidos
    loaded_sc = load_shortcuts_vdf(sc_file)
    assert len(loaded_sc.get("shortcuts", {})) == 0

    loaded_vdf = vdf.loads(vdf_file.read_text(encoding="utf-8"))
    assert gid_str not in loaded_vdf["screenshots"]


# 8. FALHA NO MEIO -> FILA INTACTA
def test_failure_keeps_queue_intact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    userdata = _setup_userdata(tmp_path)
    config = tmp_path / "config.json"
    pending = tmp_path / "pending.json"

    entry = resolve_exe("mygame", config_path=config)
    gid = entry.game_id

    remove_game(
        gid,
        userdata,
        remove_screenshots=True,
        config_path=config,
        pending_path=pending,
        force=False,
        steam_running_fn=lambda: True,
    )
    pending_before = pending.read_bytes()

    # Simula erro antes das escritas
    def fail_prepare(*args, **kwargs):
        raise RuntimeError("falha forçada")

    monkeypatch.setattr("steamshotbridge.pending._prepare_screenshots", fail_prepare)

    with pytest.raises(Exception):
        apply_pending(
            userdata,
            pending_path=pending,
            steam_running=lambda: False,
        )

    assert pending.read_bytes() == pending_before


# 9. DRY RUN SEM ESCRITA
def test_dry_run_does_not_write_anything(tmp_path: Path) -> None:
    userdata = _setup_userdata(tmp_path)
    config = tmp_path / "config.json"

    entry = resolve_exe("mygame", config_path=config)
    gid = entry.game_id

    config_before = config.read_bytes()
    vdf_file = userdata / "760" / "screenshots.vdf"
    vdf_before = vdf_file.read_bytes()

    res = remove_game(
        gid,
        userdata,
        remove_screenshots=True,
        config_path=config,
        dry_run=True,
    )

    assert res.status == "dry_run"
    assert config.read_bytes() == config_before
    assert vdf_file.read_bytes() == vdf_before
    assert not (tmp_path / "lixeira").exists()


# 10. RESTORE RECRIA O ATALHO E TIRA IGNORED
def test_restore_game_recreates_shortcut_and_unignores(tmp_path: Path) -> None:
    userdata = _setup_userdata(tmp_path)
    config = tmp_path / "config.json"
    lixeira_dir = tmp_path / "lixeira"

    entry = resolve_exe("mygame", config_path=config)
    gid = entry.game_id

    remove_game(
        gid,
        userdata,
        remove_screenshots=True,
        config_path=config,
        lixeira_dir=lixeira_dir,
    )
    assert load_exe_map(config)["mygame"].ignored is True

    restore_res = restore_game(
        gid,
        userdata,
        config_path=config,
        lixeira_dir=lixeira_dir,
    )

    assert restore_res.status == "restaurado"
    assert load_exe_map(config)["mygame"].ignored is False

    # Atalho recriado
    sc_file = shortcut_path(userdata)
    loaded_sc = load_shortcuts_vdf(sc_file)
    assert len(loaded_sc["shortcuts"]) == 1
    assert loaded_sc["shortcuts"]["0"]["AppName"] == "mygame"


# 11. DIÁLOGO COM CAIXA DESMARCADA POR PADRÃO E BOTÃO DESABILITADO PARA PROTEGIDOS
def test_ui_delete_dialog_and_protected_games(qapp: QApplication, tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    userdata = _setup_userdata(tmp_path)

    # 1. Jogo protegido da Steam (não gerenciado pelo app)
    steam_game = Game("Counter-Strike", 730, "steam", "steam")
    dlg_prot = GameDetailsDialog(
        game=steam_game,
        screenshots_count=5,
        is_provisional=False,
        tokens=MainWindow(config_path=config, steam_userdata_dir=userdata).theme_manager.tokens,
        is_app_managed=False,
    )
    assert dlg_prot.btn_delete.isEnabled() is False
    assert "Steam" in dlg_prot.btn_delete.toolTip()

    # 2. Jogo do app (gerenciado)
    entry = resolve_exe("mygame", config_path=config)
    app_game = Game("My Game", entry.game_id, "shortcut", "config.json")
    dlg_app = GameDetailsDialog(
        game=app_game,
        screenshots_count=3,
        is_provisional=False,
        tokens=MainWindow(config_path=config, steam_userdata_dir=userdata).theme_manager.tokens,
        is_app_managed=True,
    )
    assert dlg_app.btn_delete.isEnabled() is True

    # 3. Jogo não Steam que não está no exe_map nem em custom_games também pode
    orphan_id = shortcut_game_id(0x81239876)
    orphan_game = Game("Orphan", orphan_id, "shortcutname", "screenshots.vdf", orphan=True)
    dlg_orphan = GameDetailsDialog(
        game=orphan_game,
        screenshots_count=1,
        is_provisional=False,
        tokens=MainWindow(config_path=config, steam_userdata_dir=userdata).theme_manager.tokens,
        is_app_managed=False,
    )
    assert dlg_orphan.btn_delete.isEnabled() is True

    # Diálogo de confirmação de exclusão
    confirm_dlg = GameDeleteConfirmDialog(
        game_name="My Game",
        screenshots_count=3,
        game_id=app_game.game_id,
        shortcut_exists=True,
        normal_count=3,
        published_count=0,
        steam_running=False,
    )
    # Caixas desmarcadas por padrão
    assert confirm_dlg.chk_screenshots.isChecked() is False
    assert confirm_dlg.chk_published.isChecked() is False
    assert confirm_dlg.chk_published.isEnabled() is False
    assert confirm_dlg.should_remove_screenshots() is False
    assert confirm_dlg.should_include_published() is False
    # Botão cancelar é o padrão
    assert confirm_dlg.btn_cancel.isDefault() is True
    # Texto obrigatório sobre a comunidade Steam
    labels = [child.text() for child in confirm_dlg.findChildren(QLabel)]
    assert any("Isso NÃO apaga as screenshots" in text for text in labels)
    assert any("será removido" in text for text in labels)

    # 4. Diálogo com publicadas e Steam aberta
    confirm_pub = GameDeleteConfirmDialog(
        game_name="My Game",
        screenshots_count=1,
        published_count=2,
        shortcut_exists=False,
        steam_running=True,
    )
    assert confirm_pub.chk_published.isEnabled() is True
    labels_pub = [child.text() for child in confirm_pub.findChildren(QLabel)]
    assert any("já não existe" in text for text in labels_pub)
    assert any("A Steam está aberta" in text for text in labels_pub)
    assert PUBLISHED_PRESERVED_WARNING in labels_pub


# 12. CLI WATCH REMOVE E RESTORE
def test_cli_watch_remove_and_restore(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    userdata = _setup_userdata(tmp_path)
    config = tmp_path / "config.json"
    pending = tmp_path / "pending.json"

    entry = resolve_exe("mygame", config_path=config)
    gid = entry.game_id

    # CLI remove com dry-run
    exit_code = watch_cli_main([
        "remove",
        "--game-id", str(gid),
        "--dry-run",
        "--config", str(config),
        "--steam-userdata", str(userdata),
    ])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "[dry-run]" in captured.out

    # CLI remove real
    exit_code = watch_cli_main([
        "remove",
        "--game-id", str(gid),
        "--config", str(config),
        "--steam-userdata", str(userdata),
        "--pending", str(pending),
    ])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert f"Removido: mygame ({gid})" in captured.out
    assert load_exe_map(config)["mygame"].ignored is True

    # CLI restore
    exit_code = watch_cli_main([
        "restore",
        "--game-id", str(gid),
        "--config", str(config),
        "--steam-userdata", str(userdata),
        "--pending", str(pending),
    ])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert f"Restaurado: mygame ({gid})" in captured.out
    assert load_exe_map(config)["mygame"].ignored is False

# 13. EXCLUIR ATALHO MANUAL SEM EXE_MAP/CUSTOM_GAMES
def test_remove_manual_shortcut_not_in_config(tmp_path: Path) -> None:
    userdata = _setup_userdata(tmp_path)
    config = tmp_path / "config.json"

    gid = shortcut_game_id(0x81234567)
    appid32 = (gid >> 32) & 0xFFFFFFFF
    sc_path = shortcut_path(userdata)
    sc_path.parent.mkdir(parents=True, exist_ok=True)
    sc_path.write_bytes(
        vdf.binary_dumps(
            {
                "shortcuts": {
                    "0": {
                        "appid": signed_32(appid32),
                        "AppName": "Manual Game",
                        "tags": {},
                    },
                    "1": {
                        "appid": 9999,
                        "AppName": "Other Game",
                        "tags": {},
                    },
                }
            }
        )
    )

    res = remove_game(gid, userdata, config_path=config)

    assert res.status == "removido"
    assert res.shortcut_removed is True
    assert res.shortcutname_removed is False
    assert not config.exists()

    loaded = load_shortcuts_vdf(sc_path)
    assert list(loaded["shortcuts"].keys()) == ["0"]
    assert loaded["shortcuts"]["0"]["AppName"] == "Other Game"
    assert len(list(sc_path.parent.glob("shortcuts.vdf.bak_*"))) == 1


# 14. EXCLUIR JOGO SEM ATALHO EXISTENTE E LIMPAR SHORTCUTNAMES ÓRFÃO
def test_remove_without_existing_shortcut_cleans_orphan_shortcutname(
    tmp_path: Path,
) -> None:
    userdata = _setup_userdata(tmp_path)
    config = tmp_path / "config.json"
    gid = shortcut_game_id(0x81234568)
    gid_str = str(gid)

    vdf_file = userdata / "760" / "screenshots.vdf"
    vdf_file.write_text(
        f'"screenshots"\n{{\n    "shortcutnames"\n    {{\n        "{gid_str}" "Orphan"\n    }}\n}}\n',
        encoding="utf-8",
    )

    before = list_games(userdata, config)
    assert any(game.game_id == gid for game in before)

    res = remove_game(gid, userdata, config_path=config)

    assert res.status == "removido"
    assert res.shortcut_removed is False
    assert res.shortcutname_removed is True
    root = vdf.loads(vdf_file.read_text(encoding="utf-8"))
    assert gid_str not in root["screenshots"].get("shortcutnames", {})
    assert all(game.game_id != gid for game in list_games(userdata, config))


# 15. QUALQUER GAME_ID DE 64 BITS PODE SER EXCLUÍDO (PASTA/ENTRADAS)
def test_remove_non_steam_game_with_only_folder_and_entries(tmp_path: Path) -> None:
    userdata = _setup_userdata(tmp_path)
    config = tmp_path / "config.json"
    lixeira_dir = tmp_path / "lixeira"
    gid = shortcut_game_id(0x81234569)
    gid_str = str(gid)

    assert can_remove_game(gid) is True
    assert can_remove_game(730) is False

    image = userdata / "760" / "remote" / gid_str / "screenshots" / "only.jpg"
    _create_image(image)
    vdf_file = userdata / "760" / "screenshots.vdf"
    vdf_file.write_text(
        f'''"screenshots"
{{
    "{gid_str}"
    {{
        "0"
        {{
            "filename" "{gid_str}/screenshots/only.jpg"
            "creation" "100"
        }}
    }}
}}
''',
        encoding="utf-8",
    )

    info = inspect_removal(gid, userdata, config)
    assert info.game_id == gid
    assert info.normal_count == 1
    assert info.published_count == 0
    assert info.shortcut_exists is False

    res = remove_game(
        gid,
        userdata,
        remove_screenshots=True,
        config_path=config,
        lixeira_dir=lixeira_dir,
    )

    assert res.status == "removido"
    assert res.screenshots_moved == 1
    assert res.block_removed is True
    assert not image.exists()
    assert res.lixeira_path is not None
    root = vdf.loads(vdf_file.read_text(encoding="utf-8"))
    assert gid_str not in root["screenshots"]


# 16. PUBLICADAS SÓ SAEM COM A OPÇÃO SEPARADA; LIMPEZA SÓ QUANDO VAZIO
def test_include_published_removes_published_entries_and_files(
    tmp_path: Path,
) -> None:
    userdata = _setup_userdata(tmp_path)
    config = tmp_path / "config.json"
    lixeira_dir = tmp_path / "lixeira"

    entry = resolve_exe("mygame", config_path=config)
    gid = entry.game_id
    gid_str = str(gid)

    s_dir = userdata / "760" / "remote" / gid_str / "screenshots"
    _create_image(s_dir / "normal.jpg")
    _create_image(s_dir / "published.jpg")

    vdf_file = userdata / "760" / "screenshots.vdf"
    vdf_file.write_text(
        f'''"screenshots"
{{
    "{gid_str}"
    {{
        "0"
        {{
            "filename" "{gid_str}/screenshots/normal.jpg"
            "creation" "100"
        }}
        "1"
        {{
            "filename" "{gid_str}/screenshots/published.jpg"
            "creation" "200"
            "publishedfileid" "99887766"
        }}
    }}
    "shortcutnames"
    {{
        "{gid_str}" "My Game"
    }}
}}
''',
        encoding="utf-8",
    )

    # Opção separada: remove só publicadas e mantém o resto listado.
    first = remove_game(
        gid,
        userdata,
        include_published=True,
        config_path=config,
        lixeira_dir=lixeira_dir,
    )
    assert first.status == "removido"
    assert first.published_removed == 1
    assert first.published_preserved == 0
    assert first.game_still_listed is False
    assert not (s_dir / "published.jpg").exists()
    assert (s_dir / "normal.jpg").is_file()

    root = vdf.loads(vdf_file.read_text(encoding="utf-8"))
    block = root["screenshots"][gid_str]
    assert list(block.keys()) == ["0"]
    assert block["0"]["filename"].endswith("normal.jpg")
    assert gid_str in root["screenshots"]["shortcutnames"]

    # Depois remove as normais: agora o bloco e o shortcutnames somem.
    second = remove_game(
        gid,
        userdata,
        remove_screenshots=True,
        config_path=config,
        lixeira_dir=lixeira_dir,
    )
    assert second.status == "removido"
    assert second.block_removed is True
    assert second.shortcutname_removed is True
    assert not (s_dir / "normal.jpg").exists()
    root = vdf.loads(vdf_file.read_text(encoding="utf-8"))
    assert gid_str not in root["screenshots"]
    assert gid_str not in root["screenshots"].get("shortcutnames", {})


# 17. STEAM ABERTA: FILA COM --INCLUDE-PUBLISHED E UM BACKUP POR ARQUIVO
def test_steam_open_queues_include_published_and_applies_later(
    tmp_path: Path,
) -> None:
    userdata = _setup_userdata(tmp_path)
    config = tmp_path / "config.json"
    pending = tmp_path / "pending.json"
    lixeira_dir = tmp_path / "lixeira"

    entry = resolve_exe("mygame", config_path=config)
    gid = entry.game_id
    gid_str = str(gid)

    s_dir = userdata / "760" / "remote" / gid_str / "screenshots"
    _create_image(s_dir / "normal.jpg")
    _create_image(s_dir / "published.jpg")

    vdf_file = userdata / "760" / "screenshots.vdf"
    vdf_file.write_text(
        f'''"screenshots"
{{
    "{gid_str}"
    {{
        "0"
        {{
            "filename" "{gid_str}/screenshots/normal.jpg"
        }}
        "1"
        {{
            "filename" "{gid_str}/screenshots/published.jpg"
            "publishedfileid" "99887766"
        }}
    }}
    "shortcutnames"
    {{
        "{gid_str}" "My Game"
    }}
}}
''',
        encoding="utf-8",
    )

    sc_file = shortcut_path(userdata)
    sc_file.parent.mkdir(parents=True, exist_ok=True)
    sc_file.write_bytes(
        vdf.binary_dumps(
            {
                "shortcuts": {
                    "0": {
                        "appid": signed_32((gid >> 32) & 0xFFFFFFFF),
                        "AppName": "My Game",
                        "tags": {},
                    }
                }
            }
        )
    )

    vdf_before = vdf_file.read_bytes()
    sc_before = sc_file.read_bytes()

    res = remove_game(
        gid,
        userdata,
        remove_screenshots=True,
        include_published=True,
        config_path=config,
        pending_path=pending,
        lixeira_dir=lixeira_dir,
        steam_running_fn=lambda: True,
    )

    assert res.status == "pendente"
    assert vdf_file.read_bytes() == vdf_before
    assert sc_file.read_bytes() == sc_before
    entries = load_pending(pending)
    assert len(entries) == 1
    assert entries[0]["include_published"] is True

    apply_res = apply_pending(
        userdata,
        pending_path=pending,
        steam_running=lambda: False,
    )
    assert apply_res.status == "aplicado"
    assert load_pending(pending) == []

    assert len(list(vdf_file.parent.glob("screenshots.vdf.bak_*"))) == 1
    assert len(list(sc_file.parent.glob("shortcuts.vdf.bak_*"))) == 1

    loaded_sc = load_shortcuts_vdf(sc_file)
    assert len(loaded_sc.get("shortcuts", {})) == 0
    root = vdf.loads(vdf_file.read_text(encoding="utf-8"))
    assert gid_str not in root["screenshots"]
    assert gid_str not in root["screenshots"].get("shortcutnames", {})
    assert not (s_dir / "normal.jpg").exists()
    assert not (s_dir / "published.jpg").exists()
    trash_files = [p for p in lixeira_dir.rglob("*") if p.is_file()]
    assert len(trash_files) == 2


# 18. CLI: --INCLUDE-PUBLISHED ACEITO NO DRY-RUN
def test_cli_remove_include_published_dry_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    userdata = _setup_userdata(tmp_path)
    config = tmp_path / "config.json"
    entry = resolve_exe("mygame", config_path=config)
    gid = entry.game_id
    gid_str = str(gid)

    vdf_file = userdata / "760" / "screenshots.vdf"
    vdf_file.write_text(
        f'''"screenshots"
{{
    "{gid_str}"
    {{
        "0"
        {{
            "filename" "{gid_str}/screenshots/pub.jpg"
            "publishedfileid" "99887766"
        }}
    }}
}}
''',
        encoding="utf-8",
    )
    before = vdf_file.read_bytes()

    exit_code = watch_cli_main(
        [
            "remove",
            "--game-id",
            str(gid),
            "--with-screenshots",
            "--include-published",
            "--dry-run",
            "--config",
            str(config),
            "--steam-userdata",
            str(userdata),
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "[dry-run]" in captured.out
    assert "Publicadas a remover: 1" in captured.out
    assert vdf_file.read_bytes() == before


# 19. GRADE ATUALIZA APÓS EXCLUSÃO DIRETA E APÓS APLICAR A FILA
def test_grid_refreshes_after_delete_and_after_queue_apply(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.json"
    userdata = _setup_userdata(tmp_path)
    pending = tmp_path / "pending.json"

    direct_id = shortcut_game_id(0x81234570)
    direct_str = str(direct_id)
    _create_image(
        userdata / "760" / "remote" / direct_str / "screenshots" / "shot.jpg"
    )
    vdf_file = userdata / "760" / "screenshots.vdf"
    vdf_file.write_text(
        f'"screenshots"\n{{\n    "shortcutnames"\n    {{\n        "{direct_str}" "Direct Game"\n    }}\n}}\n',
        encoding="utf-8",
    )

    window = MainWindow(
        config_path=config,
        steam_userdata_dir=userdata,
        pending_path=pending,
    )
    window.refresh_games()
    assert direct_id in {game.game_id for (game, *_) in window._games_cache}

    direct_game = next(
        game for (game, *_) in window._games_cache if game.game_id == direct_id
    )
    window._on_game_action("delete", (direct_game, False, False))
    assert direct_id not in {game.game_id for (game, *_) in window._games_cache}
    assert direct_id not in window._game_cards

    queued_id = shortcut_game_id(0x81234571)
    queued_str = str(queued_id)
    _create_image(
        userdata / "760" / "remote" / queued_str / "screenshots" / "shot.jpg"
    )
    sc_file = shortcut_path(userdata)
    sc_file.parent.mkdir(parents=True, exist_ok=True)
    sc_file.write_bytes(
        vdf.binary_dumps(
            {
                "shortcuts": {
                    "0": {
                        "appid": signed_32((queued_id >> 32) & 0xFFFFFFFF),
                        "AppName": "Queued Game",
                        "tags": {},
                    }
                }
            }
        )
    )
    vdf_file.write_text(
        f'''"screenshots"
{{
    "shortcutnames"
    {{
        "{queued_str}" "Queued Game"
    }}
}}
''',
        encoding="utf-8",
    )

    remove_game(
        queued_id,
        userdata,
        config_path=config,
        pending_path=pending,
        steam_running_fn=lambda: True,
    )
    window.refresh_games()
    assert queued_id in {game.game_id for (game, *_) in window._games_cache}

    monkeypatch.setattr(
        "steamshotbridge.app.window.QMessageBox.information",
        lambda *args, **kwargs: None,
    )
    window._on_apply_pending_clicked()
    assert queued_id not in {game.game_id for (game, *_) in window._games_cache}
    assert queued_id not in window._game_cards

    window.close()

# 20. JOGO SEM screenshots.vdf: REMOÇÃO DIRETA E FILA
def test_remove_without_screenshots_vdf_direct_and_queued(
    tmp_path: Path,
) -> None:
    userdata = tmp_path / "Steam" / "userdata" / "12345"
    (userdata / "760").mkdir(parents=True)
    config = tmp_path / "config.json"
    lixeira_dir = tmp_path / "lixeira"

    direct_id = shortcut_game_id(0x81234572)
    direct_str = str(direct_id)
    direct_shot = _create_image(
        userdata / "760" / "remote" / direct_str / "screenshots" / "shot.jpg"
    )
    sc_file = shortcut_path(userdata)
    sc_file.parent.mkdir(parents=True, exist_ok=True)
    sc_file.write_bytes(
        vdf.binary_dumps(
            {
                "shortcuts": {
                    "0": {
                        "appid": signed_32((direct_id >> 32) & 0xFFFFFFFF),
                        "AppName": "Direct Game",
                        "tags": {},
                    }
                }
            }
        )
    )

    res = remove_game(
        direct_id,
        userdata,
        remove_screenshots=True,
        config_path=config,
        lixeira_dir=lixeira_dir,
    )
    assert res.status == "removido"
    assert res.screenshots_moved == 1
    assert not direct_shot.exists()
    assert len(load_shortcuts_vdf(sc_file)["shortcuts"]) == 0
    assert not (userdata / "760" / "screenshots.vdf").exists()

    queued_id = shortcut_game_id(0x81234573)
    queued_str = str(queued_id)
    queued_shot = _create_image(
        userdata / "760" / "remote" / queued_str / "screenshots" / "shot.jpg"
    )
    sc_file.write_bytes(
        vdf.binary_dumps(
            {
                "shortcuts": {
                    "0": {
                        "appid": signed_32((queued_id >> 32) & 0xFFFFFFFF),
                        "AppName": "Queued Game",
                        "tags": {},
                    }
                }
            }
        )
    )
    pending = tmp_path / "pending.json"

    res = remove_game(
        queued_id,
        userdata,
        remove_screenshots=True,
        config_path=config,
        pending_path=pending,
        steam_running_fn=lambda: True,
    )
    assert res.status == "pendente"
    assert queued_shot.exists()
    assert len(load_pending(pending)) == 1

    apply_res = apply_pending(
        userdata,
        pending_path=pending,
        steam_running=lambda: False,
    )
    assert apply_res.status == "aplicado"
    assert load_pending(pending) == []
    assert not queued_shot.exists()
    assert len(load_shortcuts_vdf(sc_file)["shortcuts"]) == 0
    assert not (userdata / "760" / "screenshots.vdf").exists()
