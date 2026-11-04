from __future__ import annotations

import io
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request

import pytest
from PIL import Image
from PySide6.QtCore import QSize
from PySide6.QtWidgets import QApplication

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from steamshotbridge.app.covers import (
    BLOCK_DURATION_SECONDS,
    CoverManager,
    color_from_game_id,
    generate_placeholder_cover,
    load_covers_index,
    normalize_store_name,
    resolve_game_cover,
    search_steam_store_appid,
    set_custom_cover,
)
from steamshotbridge.app.window import MainWindow


@pytest.fixture(scope="session", autouse=True)
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_network(*args, **kwargs):
        raise urllib.error.URLError("Internet desativada nos testes")

    monkeypatch.setattr(urllib.request, "urlopen", fail_network)


def _valid_jpeg_bytes(width: int = 100, height: int = 150, color=(50, 100, 150)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, "JPEG")
    return buf.getvalue()


def _create_dummy_image(path: Path, width: int = 600, height: int = 900, color=(50, 100, 150)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (width, height), color).save(path, "JPEG")
    return path


def _setup_userdata_with_games(tmp_path: Path) -> Path:
    userdata = tmp_path / "Steam" / "userdata" / "12345"
    # Jogo 730 tem 1 screenshot
    s730 = userdata / "760" / "remote" / "730" / "screenshots"
    s730.mkdir(parents=True, exist_ok=True)
    _create_dummy_image(s730 / "shot1.jpg", 320, 180)

    # Jogo 570 NÃO tem screenshots (pasta screenshots vazia ou ausente)
    s570 = userdata / "760" / "remote" / "570"
    s570.mkdir(parents=True, exist_ok=True)

    vdf = userdata / "760" / "screenshots.vdf"
    vdf.write_text(
        '"screenshots"\n{\n    "shortcutnames"\n    {\n    }\n}\n',
        encoding="utf-8",
    )
    return userdata


# 1. NORMALIZAÇÃO DE NOME
def test_normalize_store_name() -> None:
    assert normalize_store_name("Rocket League®") == "rocketleague"
    assert normalize_store_name("Counter-Strike: Global Offensive") == "counterstrikeglobaloffensive"
    assert normalize_store_name("A.O.M. - The Titans!") == "aomthetitans"


# 2. PLACEHOLDER E CORES DERIVADAS DO GAME_ID
def test_placeholder_and_color_derivation() -> None:
    c1 = color_from_game_id(730, is_dark=True)
    c2 = color_from_game_id(570, is_dark=True)
    assert c1.isValid()
    assert c2.isValid()

    pix = generate_placeholder_cover(730, "Counter-Strike", width=120, height=180, is_dark=True)
    assert not pix.isNull()
    assert pix.width() == 120
    assert pix.height() == 180


# 3. ETAPA 1a: LIBRARYCACHE ESTRUTURAS ANTIGA E NOVA
def test_librarycache_old_and_new_structure(tmp_path: Path) -> None:
    steam_path = tmp_path / "Steam"
    cache_dir = steam_path / "appcache" / "librarycache"
    covers_dir = tmp_path / "covers"
    index_path = tmp_path / "covers_index.json"

    # Estrutura antiga: <appid>_library_600x900.jpg
    _create_dummy_image(cache_dir / "730_library_600x900.jpg")
    res_old = resolve_game_cover(
        game_id=730,
        game_name="CS:GO",
        steam_path=steam_path,
        covers_dir=covers_dir,
        index_path=index_path,
    )
    assert res_old.source == "local"
    assert res_old.cover_path is not None
    assert res_old.cover_path.is_file()

    # Estrutura nova: <appid>/<hash>/library_600x900.jpg
    _create_dummy_image(cache_dir / "570" / "abc123hash" / "library_600x900.jpg")
    res_new = resolve_game_cover(
        game_id=570,
        game_name="Dota 2",
        steam_path=steam_path,
        covers_dir=covers_dir,
        index_path=index_path,
    )
    assert res_new.source == "local"
    assert res_new.cover_path is not None
    assert res_new.cover_path.is_file()


# 4. ETAPA 1b: DOWNLOAD CDN (QUANDO NÃO EXISTE LOCAL)
def test_cdn_download_and_header_fallback(tmp_path: Path) -> None:
    covers_dir = tmp_path / "covers"
    index_path = tmp_path / "covers_index.json"
    dummy_jpeg = _valid_jpeg_bytes()

    def fake_opener(url: str) -> bytes | None:
        if "library_600x900.jpg" in url:
            return dummy_jpeg
        return None

    res = resolve_game_cover(
        game_id=440,
        game_name="Team Fortress 2",
        covers_dir=covers_dir,
        index_path=index_path,
        url_opener=fake_opener,
    )
    assert res.source == "cdn"
    assert res.cover_path is not None
    assert res.cover_path.is_file()


# 5. ETAPA 2: JOGO NÃO-STEAM COM ARTE NO GRID
def test_non_steam_grid_art(tmp_path: Path) -> None:
    userdata = tmp_path / "userdata" / "123"
    grid = userdata / "config" / "grid"
    covers_dir = tmp_path / "covers"
    index_path = tmp_path / "covers_index.json"

    non_steam_id = 17842732021063876608
    appid32 = (non_steam_id >> 32) & 0xFFFFFFFF

    _create_dummy_image(grid / f"{appid32}p.png")

    res = resolve_game_cover(
        game_id=non_steam_id,
        game_name="Non Steam Game",
        steam_userdata_dir=userdata,
        covers_dir=covers_dir,
        index_path=index_path,
    )
    assert res.source == "grid"
    assert res.cover_path is not None
    assert res.cover_path.is_file()


# 6. ETAPA 3: BUSCA STEAM STORE ACEITA SÓ COM NOME IDÊNTICO E REJEITA SE DIFERENTE
def test_store_search_accepts_only_identical_normalized_name(tmp_path: Path) -> None:
    dummy_jpeg = _valid_jpeg_bytes()

    # 1. Busca aceita (nome normalizado idêntico)
    def fake_opener_match(url: str) -> bytes | None:
        if "storesearch" in url:
            return json.dumps({
                "items": [{"id": 252950, "name": "Rocket League®"}]
            }).encode("utf-8")
        if "cdn.akamai" in url:
            return dummy_jpeg
        return None

    res_match = resolve_game_cover(
        game_id=11183602996342685696,
        game_name="Rocket League",
        covers_dir=tmp_path / "covers",
        index_path=tmp_path / "covers_index.json",
        url_opener=fake_opener_match,
    )
    assert res_match.source == "busca"
    assert res_match.appid == 252950

    # 2. Busca rejeitada (nome diferente, ex: Portal 2 vs Portal)
    def fake_opener_mismatch(url: str) -> bytes | None:
        if "storesearch" in url:
            return json.dumps({
                "items": [{"id": 620, "name": "Portal 2"}]
            }).encode("utf-8")
        return None

    res_mismatch = resolve_game_cover(
        game_id=1234567890123456789,
        game_name="Portal",
        covers_dir=tmp_path / "covers",
        index_path=tmp_path / "covers_index.json",
        url_opener=fake_opener_mismatch,
    )
    assert res_mismatch.source != "busca"
    assert res_mismatch.source in ("screenshot", "placeholder")


# 7. ETAPA 4a: FALLBACK SCREENSHOT THUMBNAIL
def test_fallback_screenshot_thumbnail(tmp_path: Path) -> None:
    userdata = tmp_path / "userdata" / "123"
    thumb_dir = userdata / "760" / "remote" / "999" / "screenshots" / "thumbnails"
    _create_dummy_image(thumb_dir / "20260101_1.jpg", width=400, height=225)

    res = resolve_game_cover(
        game_id=999,
        game_name="Unknown Game",
        steam_userdata_dir=userdata,
        covers_dir=tmp_path / "covers",
        index_path=tmp_path / "covers_index.json",
    )
    assert res.source == "screenshot"
    assert res.cover_path is not None
    assert res.cover_path.is_file()


# 8. ETAPA 4b: FALLBACK PLACEHOLDER
def test_fallback_placeholder_when_no_screenshot(tmp_path: Path) -> None:
    userdata = tmp_path / "userdata" / "123"
    res = resolve_game_cover(
        game_id=888,
        game_name="Game With No Art",
        steam_userdata_dir=userdata,
        covers_dir=tmp_path / "covers",
        index_path=tmp_path / "covers_index.json",
    )
    assert res.source == "placeholder"
    assert res.cover_path is None


# 9. REDE: FALHA SEM EXCEÇÃO E BLOQUEIO DE 24 H
def test_network_failure_without_exception_and_24h_block(tmp_path: Path) -> None:
    covers_dir = tmp_path / "covers"
    index_path = tmp_path / "covers_index.json"

    call_count = 0

    def failing_opener(url: str) -> bytes | None:
        nonlocal call_count
        call_count += 1
        raise OSError("Falha de conexão de rede")

    # Primeira tentativa: falha de rede tratada sem exceção
    res1 = resolve_game_cover(
        game_id=123,
        game_name="Game",
        covers_dir=covers_dir,
        index_path=index_path,
        url_opener=failing_opener,
    )
    assert res1.source == "placeholder"
    assert call_count > 0

    # Segunda tentativa imediata: bloqueada por 24 h, não tenta a rede de novo
    prev_calls = call_count
    res2 = resolve_game_cover(
        game_id=123,
        game_name="Game",
        covers_dir=covers_dir,
        index_path=index_path,
        url_opener=failing_opener,
    )
    assert res2.source == "placeholder"
    assert call_count == prev_calls

    # Terceira tentativa com force=True: ignora o bloqueio de 24 h
    res3 = resolve_game_cover(
        game_id=123,
        game_name="Game",
        covers_dir=covers_dir,
        index_path=index_path,
        force=True,
        url_opener=failing_opener,
    )
    assert call_count > prev_calls


# 10. CAPA MANUAL NUNCA É SOBRESCRITA AUTOMATICAMENTE
def test_manual_cover_is_preserved(tmp_path: Path) -> None:
    covers_dir = tmp_path / "covers"
    index_path = tmp_path / "covers_index.json"

    manual_source = _create_dummy_image(tmp_path / "my_custom_cover.png", color=(10, 20, 30))
    set_custom_cover(730, manual_source, covers_dir=covers_dir, index_path=index_path)

    # Nova resolução não deve sobrescrever capa manual
    res = resolve_game_cover(
        game_id=730,
        game_name="Counter-Strike",
        covers_dir=covers_dir,
        index_path=index_path,
    )
    assert res.source == "manual"


# 11. CACHE HIT SEM REQUISIÇÃO
def test_cache_hit_does_not_make_requests(tmp_path: Path) -> None:
    covers_dir = tmp_path / "covers"
    index_path = tmp_path / "covers_index.json"

    _create_dummy_image(covers_dir / "730.jpg")
    index = {
        "730": {
            "game_id": "730",
            "fonte": "cdn",
            "appid": 730,
            "data": "2026-11-03T00:00:00",
            "data_ultima_falha": None,
        }
    }
    index_path.write_text(json.dumps(index), encoding="utf-8")

    requests_made = []

    def tracking_opener(url: str) -> bytes | None:
        requests_made.append(url)
        return None

    res = resolve_game_cover(
        game_id=730,
        game_name="CS",
        covers_dir=covers_dir,
        index_path=index_path,
        url_opener=tracking_opener,
    )
    assert res.source == "cdn"
    assert len(requests_made) == 0


# 12. RESOLUÇÃO ASSÍNCRONA VIA COVERMANAGER
def test_cover_manager_async_resolution(qapp: QApplication, tmp_path: Path) -> None:
    covers_dir = tmp_path / "covers"
    index_path = tmp_path / "covers_index.json"

    manager = CoverManager(covers_dir=covers_dir, index_path=index_path)

    signals_received = []
    manager.cover_loaded.connect(lambda gid, name, pix, src: signals_received.append((gid, src)))

    ph, src = manager.request_cover(730, "CS:GO")
    assert not ph.isNull()
    assert src == "placeholder"

    for _ in range(50):
        qapp.processEvents()
        if signals_received:
            break
        time.sleep(0.02)

    assert len(signals_received) == 1
    assert signals_received[0][0] == 730


# 13. UI: JOGOS SEM SCREENSHOTS NÃO APARECEM
def test_games_without_screenshots_are_not_displayed(qapp: QApplication, tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    userdata = _setup_userdata_with_games(tmp_path)

    window = MainWindow(config_path=config, steam_userdata_dir=userdata)
    window.refresh_games()

    cached_ids = [g.game_id for (g, *_) in window._games_cache]
    assert 730 in cached_ids
    assert 570 not in cached_ids

    window.close()
