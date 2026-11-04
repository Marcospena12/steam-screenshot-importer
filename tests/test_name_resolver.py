from __future__ import annotations

import json
from pathlib import Path

import pytest
import vdf
from PIL import Image

from steamshotbridge import (
    exe_map as exe_map_module,
    importer,
    pending as pending_module,
    shortcuts,
)
from steamshotbridge.exe_map import (
    generate_game_id,
    is_shortcut_applied,
    load_exe_map,
    rename_exe,
    resolve_exe,
)
from steamshotbridge.name_resolver import (
    clean_and_validate_name,
    find_installation_folder_name,
    resolve_game_name,
    resolve_game_name_diagnostic,
)
from steamshotbridge.pending import apply_pending, load_pending
from steamshotbridge.watcher import ProcessedRegistry, process_file
from tools.watch import main as watch_cli_main

USERDATA_ID = "12345"
BASE_VDF = '''"screenshots"
{
    "shortcutnames"
    {
    }
}
'''


class DummyProc:
    def __init__(self, pid: int, name: str, exe_path: str | None = None) -> None:
        self.pid = pid
        self._name = name
        self._exe = exe_path
        self.info = {"pid": pid, "name": name}

    def exe(self) -> str:
        if self._exe is None:
            raise OSError("Sem permissão")
        return self._exe


@pytest.fixture(autouse=True)
def _steam_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importer, "is_steam_running", lambda: False)
    monkeypatch.setattr(shortcuts, "is_steam_running", lambda: False)
    monkeypatch.setattr(pending_module, "is_steam_running", lambda: False)
    monkeypatch.setattr(exe_map_module, "is_steam_running", lambda: False)


def _setup_userdata(tmp_path: Path) -> tuple[Path, Path]:
    userdata = tmp_path / "Steam" / "userdata" / USERDATA_ID
    (userdata / "760").mkdir(parents=True)
    vdf_path = userdata / "760" / "screenshots.vdf"
    vdf_path.write_text(BASE_VDF, encoding="utf-8")
    return userdata, vdf_path


def _make_image(folder: Path, name: str) -> Path:
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (320, 180), (10, 30, 60)).save(path, format="JPEG")
    return path


def test_clean_and_validate_rejects_empty_digits_and_generics() -> None:
    assert clean_and_validate_name("")[0] is None
    assert clean_and_validate_name("   ")[0] is None
    assert clean_and_validate_name("123456")[0] is None
    assert clean_and_validate_name("a" * 81)[0] is None
    assert clean_and_validate_name("Unreal Engine")[0] is None
    assert clean_and_validate_name("UnrealEngine")[0] is None
    assert clean_and_validate_name("Unity")[0] is None
    assert clean_and_validate_name("Unity Player")[0] is None
    assert clean_and_validate_name("Epic Games Launcher")[0] is None

    cleaned, err = clean_and_validate_name("  Rocket \t League \n 2026 \x00 ")
    assert err is None
    assert cleaned == "Rocket League 2026"


def test_versioninfo_product_name_is_first_choice() -> None:
    proc = DummyProc(
        100,
        "RocketLeague.exe",
        r"C:\Games\RocketLeague\Binaries\Win64\RocketLeague.exe",
    )
    name, source = resolve_game_name(
        "RocketLeague",
        process=proc,
        version_info_reader=lambda p: ("Rocket League", "Rocket League Game"),
        window_title_reader=lambda pid: ["Janela do Jogo"],
    )
    assert name == "Rocket League"
    assert source == "versioninfo"


def test_versioninfo_file_description_when_product_name_missing() -> None:
    proc = DummyProc(
        100,
        "aomx.exe",
        r"C:\Games\Age of Mythology\aomx.exe",
    )
    name, source = resolve_game_name(
        "aomx",
        process=proc,
        version_info_reader=lambda p: (None, "Age of Mythology: The Titans"),
        window_title_reader=lambda pid: ["Janela"],
    )
    assert name == "Age of Mythology: The Titans"
    assert source == "versioninfo"


def test_generic_folders_ignored_until_useful_name() -> None:
    exe_path = (
        r"C:\Program Files (x86)\Steam\steamapps\common\Dark Souls III\Game\bin\win64\DarkSoulsIII.exe"
    )
    folder = find_installation_folder_name(exe_path)
    assert folder == "Dark Souls III"

    proc = DummyProc(200, "DarkSoulsIII.exe", exe_path)
    name, source = resolve_game_name(
        "DarkSoulsIII",
        process=proc,
        version_info_reader=lambda p: (None, None),
        window_title_reader=lambda pid: [],
    )
    assert name == "Dark Souls III"
    assert source == "pasta"


def test_generic_versioninfo_rejected_and_falls_back_to_folder() -> None:
    proc = DummyProc(
        300,
        "MyGame.exe",
        r"C:\Games\My Real Game\Binaries\Win64\MyGame.exe",
    )
    name, source = resolve_game_name(
        "MyGame",
        process=proc,
        version_info_reader=lambda p: ("Unreal Engine 4", "Shipping Client"),
        window_title_reader=lambda pid: [],
    )
    assert name == "My Real Game"
    assert source == "pasta"


def test_window_title_chosen_when_versioninfo_and_folder_unavailable() -> None:
    proc = DummyProc(400, "game.exe", r"C:\bin\win64\game.exe")
    name, source = resolve_game_name(
        "game",
        process=proc,
        version_info_reader=lambda p: (None, None),
        window_title_reader=lambda pid: ["Generic Crash Reporter", "Hollow Knight v1.5"],
    )
    assert name == "Hollow Knight v1.5"
    assert source == "janela"


def test_process_missing_returns_exe_name_as_source_exe() -> None:
    name, source = resolve_game_name(
        "CustomGame",
        process_finder=lambda n: None,
    )
    assert name == "CustomGame"
    assert source == "exe"


def test_diagnostic_result_contains_all_candidates() -> None:
    proc = DummyProc(
        500,
        "RocketLeague.exe",
        r"C:\Games\Rocket League\Binaries\Win64\RocketLeague.exe",
    )
    diag = resolve_game_name_diagnostic(
        "RocketLeague",
        process=proc,
        version_info_reader=lambda p: ("Rocket League", None),
        window_title_reader=lambda pid: ["Rocket League Window"],
    )
    assert diag.process_found is True
    assert diag.pid == 500
    assert diag.chosen_name == "Rocket League"
    assert diag.chosen_source == "versioninfo"
    sources = [c.source for c in diag.candidates]
    assert "versioninfo" in sources
    assert "pasta" in sources
    assert "janela" in sources
    assert "exe" in sources


def test_new_exe_uses_resolver_and_records_source(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    entry = resolve_exe(
        "RocketLeague",
        config_path=config,
        name_resolver_fn=lambda exe: ("Rocket League", "versioninfo"),
    )
    assert entry.name == "Rocket League"
    assert entry.source == "versioninfo"
    assert entry.provisional is False

    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["exe_map"]["rocketleague"]["name"] == "Rocket League"
    assert data["exe_map"]["rocketleague"]["source"] == "versioninfo"
    assert data["exe_map"]["rocketleague"]["provisional"] is False


def test_provisional_entry_updated_before_applied(tmp_path: Path) -> None:
    userdata, _ = _setup_userdata(tmp_path)
    config = tmp_path / "config.json"
    pending = tmp_path / "pending.json"
    processed = ProcessedRegistry(tmp_path / "processed.json")

    first_entry = resolve_exe(
        "aomx",
        config_path=config,
        name_resolver_fn=lambda exe: ("aomx", "exe"),
    )
    assert first_entry.provisional is True
    assert first_entry.source == "exe"

    image = _make_image(tmp_path, "aomx_2026_11_03_12_42_05_932.jpg")
    outcome = process_file(
        image,
        steam_userdata_dir=userdata,
        config_path=config,
        processed=processed,
        pending_path=pending,
        stabilize_interval=0,
        name_resolver_fn=lambda exe: ("Age of Mythology", "versioninfo"),
    )

    assert outcome.status == "imported"
    assert any("Age of Mythology" in line for line in outcome.lines)

    map_data = load_exe_map(config)
    assert map_data["aomx"].name == "Age of Mythology"
    assert map_data["aomx"].source == "versioninfo"
    assert map_data["aomx"].provisional is False


def test_provisional_entry_does_not_touch_vdfs_if_already_applied(
    tmp_path: Path,
) -> None:
    userdata, vdf_path = _setup_userdata(tmp_path)
    config = tmp_path / "config.json"
    pending = tmp_path / "pending.json"
    processed = ProcessedRegistry(tmp_path / "processed.json")

    game_id = generate_game_id("aomx")
    vdf_content = f'''"screenshots"
{{
    "{game_id}"
    {{
        "0"
        {{
            "type" "1"
            "filename" "{game_id}/screenshots/20260101_1.jpg"
            "thumbnail" "{game_id}/screenshots/thumbnails/20260101_1.jpg"
            "imported" "1"
            "width" "400"
            "height" "225"
            "gameid" "{game_id}"
            "creation" "100"
            "Permissions" "2"
            "hscreenshot" "18446744073709551615"
        }}
    }}
    "shortcutnames"
    {{
        "{game_id}" "aomx"
    }}
}}
'''
    vdf_path.write_text(vdf_content, encoding="utf-8")
    before_vdf = vdf_path.read_bytes()

    resolve_exe(
        "aomx",
        config_path=config,
        name_resolver_fn=lambda exe: ("aomx", "exe"),
    )
    assert is_shortcut_applied(userdata, game_id) is True

    image = _make_image(tmp_path, "aomx_2026_11_03_12_42_05_932.jpg")
    outcome = process_file(
        image,
        steam_userdata_dir=userdata,
        config_path=config,
        processed=processed,
        pending_path=pending,
        stabilize_interval=0,
        name_resolver_fn=lambda exe: ("Age of Mythology", "versioninfo"),
    )

    assert any("aviso: nome melhor disponível: Age of Mythology" in line for line in outcome.lines)
    assert load_exe_map(config)["aomx"].name == "aomx"


def test_rename_when_steam_is_closed_updates_vdfs_with_backup(
    tmp_path: Path,
) -> None:
    userdata, vdf_path = _setup_userdata(tmp_path)
    config = tmp_path / "config.json"
    pending = tmp_path / "pending.json"

    entry = resolve_exe("aomx", config_path=config)
    game_id = entry.game_id
    appid32 = (game_id >> 32) & 0xFFFFFFFF

    shortcuts_path = userdata / "config" / "shortcuts.vdf"
    shortcuts_path.parent.mkdir(parents=True)
    root = {
        "shortcuts": {
            "0": {
                "appid": appid32 - 0x100000000 if appid32 & 0x80000000 else appid32,
                "AppName": "aomx",
                "tags": {},
            }
        }
    }
    shortcuts_path.write_bytes(vdf.binary_dumps(root))

    screenshots_content = f'''"screenshots"
{{
    "shortcutnames"
    {{
        "{game_id}" "aomx"
    }}
}}
'''
    vdf_path.write_text(screenshots_content, encoding="utf-8")

    updated, status = rename_exe(
        "aomx",
        "Age of Mythology: Retold",
        steam_userdata_dir=userdata,
        config_path=config,
        pending_path=pending,
        steam_running_fn=lambda: False,
    )

    assert status == "aplicado"
    assert updated.name == "Age of Mythology: Retold"
    assert updated.game_id == game_id
    assert updated.source == "manual"

    loaded_shortcuts = vdf.binary_load(shortcuts_path.open("rb"))
    assert loaded_shortcuts["shortcuts"]["0"]["AppName"] == "Age of Mythology: Retold"

    loaded_screenshots = vdf.loads(vdf_path.read_text(encoding="utf-8"))
    assert loaded_screenshots["screenshots"]["shortcutnames"][str(game_id)] == "Age of Mythology: Retold"

    assert len(list(shortcuts_path.parent.glob("shortcuts.vdf.bak_*"))) == 1
    assert len(list(vdf_path.parent.glob("screenshots.vdf.bak_*"))) == 1


def test_rename_when_steam_is_open_enqueues_and_applies_later(
    tmp_path: Path,
) -> None:
    userdata, vdf_path = _setup_userdata(tmp_path)
    config = tmp_path / "config.json"
    pending = tmp_path / "pending.json"

    entry = resolve_exe("aomx", config_path=config)
    game_id = entry.game_id
    appid32 = (game_id >> 32) & 0xFFFFFFFF

    shortcuts_path = userdata / "config" / "shortcuts.vdf"
    shortcuts_path.parent.mkdir(parents=True)
    root = {
        "shortcuts": {
            "0": {
                "appid": appid32 - 0x100000000 if appid32 & 0x80000000 else appid32,
                "AppName": "aomx",
                "tags": {},
            }
        }
    }
    shortcuts_path.write_bytes(vdf.binary_dumps(root))
    before_shortcuts = shortcuts_path.read_bytes()
    before_vdf = vdf_path.read_bytes()

    updated, status = rename_exe(
        "aomx",
        "Age of Mythology",
        steam_userdata_dir=userdata,
        config_path=config,
        pending_path=pending,
        steam_running_fn=lambda: True,
    )

    assert status == "pendente"
    assert shortcuts_path.read_bytes() == before_shortcuts
    assert vdf_path.read_bytes() == before_vdf
    assert len(load_pending(pending)) == 1

    apply_res = apply_pending(
        userdata,
        pending_path=pending,
        steam_running=lambda: False,
    )
    assert apply_res.status == "aplicado"
    assert load_pending(pending) == []

    loaded_shortcuts = vdf.binary_load(shortcuts_path.open("rb"))
    assert loaded_shortcuts["shortcuts"]["0"]["AppName"] == "Age of Mythology"
    loaded_screenshots = vdf.loads(vdf_path.read_text(encoding="utf-8"))
    assert loaded_screenshots["screenshots"]["shortcutnames"][str(game_id)] == "Age of Mythology"


def test_multiple_processes_chooses_one_with_visible_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from steamshotbridge import name_resolver

    p1 = DummyProc(10, "game.exe", r"C:\p1\game.exe")
    p2 = DummyProc(20, "game.exe", r"C:\p2\game.exe")

    monkeypatch.setattr(name_resolver.psutil, "process_iter", lambda attrs: [p1, p2])
    monkeypatch.setattr(
        name_resolver,
        "get_process_window_titles",
        lambda pid: ["Janela Real"] if pid == 20 else [],
    )

    found = name_resolver.find_running_process("game")
    assert found is not None
    assert found.pid == 20


def test_rename_updates_existing_pending_queue_entries(tmp_path: Path) -> None:
    userdata, vdf_path = _setup_userdata(tmp_path)
    config = tmp_path / "config.json"
    pending = tmp_path / "pending.json"

    entry = resolve_exe("aomx", config_path=config)
    game_id = entry.game_id

    pending_record = {
        "game_id": str(game_id),
        "game_name": "aomx",
        "entry": {"filename": f"{game_id}/screenshots/2026.jpg", "creation": "100"},
        "vdf_path": str(vdf_path),
        "shortcut": {
            "path": str(userdata / "config" / "shortcuts.vdf"),
            "appid32": (game_id >> 32) & 0xFFFFFFFF,
            "name": "aomx",
            "entry": {"AppName": "aomx"},
        },
    }
    pending.write_text(json.dumps({"pending": [pending_record]}), encoding="utf-8")

    updated, status = rename_exe(
        "aomx",
        "Age of Mythology: Retold",
        steam_userdata_dir=userdata,
        config_path=config,
        pending_path=pending,
        steam_running_fn=lambda: True,
    )

    assert status == "pendente"
    entries = load_pending(pending)
    for rec in entries:
        assert rec["game_name"] == "Age of Mythology: Retold"
        if rec.get("shortcut"):
            assert rec["shortcut"]["name"] == "Age of Mythology: Retold"


def test_watch_cli_rename_command(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(exe_map_module, "is_steam_running", lambda: False)
    userdata, _ = _setup_userdata(tmp_path)
    config = tmp_path / "config.json"
    pending = tmp_path / "pending.json"
    resolve_exe("aomx", config_path=config)

    exit_code = watch_cli_main(
        [
            "rename",
            "--exe",
            "aomx",
            "--name",
            "Novo Titulo",
            "--steam-userdata",
            str(userdata),
            "--config",
            str(config),
            "--pending",
            str(pending),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Renomeado: aomx -> Novo Titulo" in captured.out
    assert load_exe_map(config)["aomx"].name == "Novo Titulo"
