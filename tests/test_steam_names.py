from __future__ import annotations

import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request

import pytest
from PySide6.QtWidgets import QApplication

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from steamshotbridge.app.theme import get_tokens
from steamshotbridge.app.widgets import GameCard, GameDetailsDialog
from steamshotbridge.app.window import MainWindow
from steamshotbridge.games import Game, list_games
from steamshotbridge.steam_names import (
    BLOCK_DURATION_SECONDS,
    SteamNameManager,
    find_manifest_game_name,
    load_steam_names_cache,
    resolve_steam_game_name,
    save_steam_names_cache,
    validate_steam_game_name,
)


@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_network(*args, **kwargs):
        raise urllib.error.URLError("Internet desativada nos testes")

    monkeypatch.setattr(urllib.request, "urlopen", fail_network)


def _setup_steam_hierarchy(tmp_path: Path) -> tuple[Path, Path]:
    steam = tmp_path / "Steam"
    (steam / "steamapps").mkdir(parents=True, exist_ok=True)
    userdata = steam / "userdata" / "12345"
    (userdata / "760" / "remote" / "730" / "screenshots").mkdir(parents=True, exist_ok=True)
    (userdata / "760" / "remote" / "730" / "screenshots" / "1.jpg").write_bytes(b"\xff\xd8\xff")
    (userdata / "760" / "screenshots.vdf").write_text('"screenshots" {}', encoding="utf-8")
    return steam, userdata


# 1. VALIDAÇÃO DO NOME
def test_validate_steam_game_name() -> None:
    assert validate_steam_game_name("") is None
    assert validate_steam_game_name("   ") is None
    assert validate_steam_game_name("a" * 121) is None
    assert validate_steam_game_name("  Half-Life \t 2 \n ") == "Half-Life 2"


# 2. MANIFESTO EM BIBLIOTECA PRINCIPAL
def test_manifest_in_primary_library(tmp_path: Path) -> None:
    steam, _ = _setup_steam_hierarchy(tmp_path)
    manifest = steam / "steamapps" / "appmanifest_730.acf"
    manifest.write_text(
        '"AppState"\n{\n    "appid" "730"\n    "name" "Counter-Strike 2"\n}\n',
        encoding="utf-8",
    )

    name = find_manifest_game_name(730, steam_path=steam)
    assert name == "Counter-Strike 2"

    res = resolve_steam_game_name(730, steam_path=steam, allow_network=False)
    assert res.name == "Counter-Strike 2"
    assert res.source == "manifest"


# 3. MANIFESTO EM BIBLIOTECA SECUNDÁRIA
def test_manifest_in_secondary_library(tmp_path: Path) -> None:
    steam, _ = _setup_steam_hierarchy(tmp_path)
    sec_lib = tmp_path / "SecondaryLibrary"
    (sec_lib / "steamapps").mkdir(parents=True, exist_ok=True)

    libfolders = steam / "steamapps" / "libraryfolders.vdf"
    libfolders.write_text(
        f'"libraryfolders"\n{{\n    "0"\n    {{\n        "path" "{steam.as_posix()}"\n    }}\n'
        f'    "1"\n    {{\n        "path" "{sec_lib.as_posix()}"\n    }}\n}}\n',
        encoding="utf-8",
    )

    manifest = sec_lib / "steamapps" / "appmanifest_570.acf"
    manifest.write_text(
        '"AppState"\n{\n    "appid" "570"\n    "name" "Dota 2"\n}\n',
        encoding="utf-8",
    )

    res = resolve_steam_game_name(570, steam_path=steam, allow_network=False)
    assert res.name == "Dota 2"
    assert res.source == "manifest"


# 4. MANIFESTO INVÁLIDO É TOLERADO
def test_invalid_manifest_is_tolerated(tmp_path: Path) -> None:
    steam, _ = _setup_steam_hierarchy(tmp_path)
    manifest = steam / "steamapps" / "appmanifest_440.acf"
    manifest.write_text("conteudo quebrado e corrompido {{{", encoding="utf-8")

    res = resolve_steam_game_name(440, steam_path=steam, allow_network=False)
    assert res.name == "AppID 440"
    assert res.source == "fallback"


# 5. CACHE HIT SEM REDE
def test_cache_hit_without_network(tmp_path: Path) -> None:
    cache_file = tmp_path / "steam_names.json"
    cache_data = {
        "730": {
            "appid": 730,
            "nome": "Cached Counter-Strike",
            "fonte": "store",
            "data": "2026-11-03T12:00:00",
            "data_ultima_falha": None,
        }
    }
    save_steam_names_cache(cache_data, cache_file)

    def fail_opener(url: str):
        raise AssertionError("Rede não deveria ter sido chamada em cache hit!")

    res = resolve_steam_game_name(
        730,
        cache_path=cache_file,
        allow_network=True,
        url_opener=fail_opener,
    )
    assert res.name == "Cached Counter-Strike"
    assert res.source == "cache"


# 6. LOJA ACEITA SÓ COM SUCCESS=TRUE E NOME NÃO VAZIO
def test_store_accepts_only_success_true_and_valid_name(tmp_path: Path) -> None:
    cache_file = tmp_path / "steam_names.json"

    # 1. success = True com nome válido
    def opener_success(url: str) -> bytes | None:
        return json.dumps({
            "730": {
                "success": True,
                "data": {"name": "Counter-Strike 2"},
            }
        }).encode("utf-8")

    res = resolve_steam_game_name(
        730,
        cache_path=cache_file,
        allow_network=True,
        url_opener=opener_success,
    )
    assert res.name == "Counter-Strike 2"
    assert res.source == "store"

    # 2. success = False -> rejeitado
    def opener_fail_flag(url: str) -> bytes | None:
        return json.dumps({"570": {"success": False}}).encode("utf-8")

    res_fail = resolve_steam_game_name(
        570,
        cache_path=cache_file,
        allow_network=True,
        url_opener=opener_fail_flag,
    )
    assert res_fail.name == "AppID 570"
    assert res_fail.source == "fallback"


# 7. FALHA DE REDE COM BLOQUEIO DE 24 H
def test_network_failure_and_24h_block(tmp_path: Path) -> None:
    cache_file = tmp_path / "steam_names.json"
    call_count = 0

    def failing_opener(url: str):
        nonlocal call_count
        call_count += 1
        raise OSError("Falha na conexão")

    # Primeira chamada: tenta e falha
    res1 = resolve_steam_game_name(
        440,
        cache_path=cache_file,
        allow_network=True,
        url_opener=failing_opener,
    )
    assert res1.name == "AppID 440"
    assert res1.source == "fallback"
    assert call_count == 1

    # Segunda chamada: bloqueio de 24 h ativado, não tenta a rede
    res2 = resolve_steam_game_name(
        440,
        cache_path=cache_file,
        allow_network=True,
        url_opener=failing_opener,
    )
    assert res2.name == "AppID 440"
    assert call_count == 1

    # Terceira chamada forçada: ignora bloqueio
    res3 = resolve_steam_game_name(
        440,
        cache_path=cache_file,
        allow_network=True,
        url_opener=failing_opener,
        force=True,
    )
    assert call_count == 2


# 8. RESOLUÇÃO FORA DA THREAD DA UI (ASSÍNCRONA)
def test_async_resolution_via_manager(qapp: QApplication, tmp_path: Path) -> None:
    cache_file = tmp_path / "steam_names.json"

    def opener_mock(url: str) -> bytes | None:
        return json.dumps({
            "730": {
                "success": True,
                "data": {"name": "Async Counter-Strike"},
            }
        }).encode("utf-8")

    manager = SteamNameManager(cache_path=cache_file)
    received = []
    manager.name_resolved.connect(lambda aid, name, src: received.append((aid, name, src)))

    initial_name, initial_src = manager.get_or_request_name(
        730,
        allow_network=True,
        url_opener=opener_mock,
    )
    assert initial_name == "AppID 730"
    assert initial_src == "fallback"

    for _ in range(50):
        qapp.processEvents()
        if received:
            break
        time.sleep(0.02)

    assert len(received) == 1
    assert received[0] == (730, "Async Counter-Strike", "store")


# 9. UI: BOTÕES DESABILITADOS PARA JOGOS DA STEAM E JOGOS NÃO-STEAM INALTERADOS
def test_ui_disabled_buttons_for_steam_games_and_badge(qapp: QApplication, tmp_path: Path) -> None:
    tokens = get_tokens(is_dark=True)

    # 1. Jogo Steam
    steam_game = Game("Counter-Strike 2", 730, "steam", "steam")
    dlg_steam = GameDetailsDialog(
        game=steam_game,
        screenshots_count=1,
        is_provisional=False,
        tokens=tokens,
        is_app_managed=False,
    )
    assert dlg_steam.btn_rename.isEnabled() is False
    assert dlg_steam.btn_rename.toolTip() == "Jogo da Steam: o nome vem da própria Steam"
    assert dlg_steam.btn_resolve.isEnabled() is False
    assert dlg_steam.btn_resolve.toolTip() == "Jogo da Steam: o nome vem da própria Steam"

    card_steam = GameCard(
        game=steam_game,
        screenshots_count=1,
        is_provisional=False,
        tokens=tokens,
    )
    # Verifica que tem o selo "Steam"
    assert any(
        isinstance(child, object) and getattr(child, "text", lambda: "")() == "Steam"
        for child in card_steam.findChildren(object)
    )

    # 2. Jogo não Steam (deve permanecer habilitado)
    non_steam_game = Game("My Game", 17842732021063876608, "shortcut", "config")
    dlg_non_steam = GameDetailsDialog(
        game=non_steam_game,
        screenshots_count=1,
        is_provisional=False,
        tokens=tokens,
        is_app_managed=True,
    )
    assert dlg_non_steam.btn_rename.isEnabled() is True
    assert dlg_non_steam.btn_resolve.isEnabled() is True


# 10. LISTAGEM EM GAMES.PY MOSTRA NOME REAL DO MANIFESTO
def test_games_list_shows_real_name_for_steam_games(tmp_path: Path) -> None:
    steam, userdata = _setup_steam_hierarchy(tmp_path)
    manifest = steam / "steamapps" / "appmanifest_730.acf"
    manifest.write_text(
        '"AppState"\n{\n    "appid" "730"\n    "name" "Counter-Strike 2"\n}\n',
        encoding="utf-8",
    )

    games = list_games(userdata, tmp_path / "config.json")
    matching = [g for g in games if g.game_id == 730]
    assert len(matching) == 1
    assert matching[0].name == "Counter-Strike 2"
