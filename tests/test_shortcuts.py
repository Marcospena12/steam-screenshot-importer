from __future__ import annotations

import os
from pathlib import Path

import pytest
import vdf

from steamshotbridge import shortcuts
from steamshotbridge.games import custom_game_id
from steamshotbridge.shortcuts import (
    DEFAULT_FIELD_NAMES,
    ShortcutError,
    ensure_shortcut,
    serialize_shortcuts_vdf,
    shortcut_path,
)
from steamshotbridge.steam import (
    load_shortcuts_vdf,
    shortcut_game_id,
    signed_32,
)

USERDATA_ID = "12345"
GAME_ID = custom_game_id("Jogo Teste")
REAL_SHORTCUTS = Path(
    r"C:\Program Files (x86)\Steam\userdata\259740280\config\shortcuts.vdf"
)


@pytest.fixture(autouse=True)
def _steam_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shortcuts, "is_steam_running", lambda: False)


def _userdata(tmp_path: Path) -> Path:
    return tmp_path / "Steam" / "userdata" / USERDATA_ID


def _appid32(game_id: int) -> int:
    return (game_id >> 32) & 0xFFFFFFFF


def _write_shortcuts(
    userdata: Path,
    entries: list[dict[str, object]],
) -> Path:
    path = shortcut_path(userdata)
    path.parent.mkdir(parents=True)
    root = vdf.VDFDict()
    shortcuts_block = vdf.VDFDict()
    for index, entry in enumerate(entries):
        shortcuts_block[str(index)] = entry
    root["shortcuts"] = shortcuts_block
    path.write_bytes(serialize_shortcuts_vdf(root))
    return path


def _template_entry(appid: int) -> dict[str, object]:
    values: dict[str, object] = {
        "appid": appid,
        "AppName": "Existente",
        "Exe": '"C:\\Windows\\System32\\cmd.exe"',
        "StartDir": "C:\\Windows\\System32\\",
        "icon": "",
        "ShortcutPath": "",
        "LaunchOptions": "",
        "IsHidden": 0,
        "AllowDesktopConfig": 1,
        "AllowOverlay": 1,
        "OpenVR": 0,
        "Devkit": 0,
        "DevkitGameID": "",
        "DevkitOverrideAppID": 0,
        "LastPlayTime": 0,
        "FlatpakAppID": "",
        "sortas": "",
        "extra_custom": 7,
        "tags": vdf.VDFDict(),
    }
    return values


def test_creates_shortcut_with_default_fields_and_signed_appid(
    tmp_path: Path,
) -> None:
    userdata = _userdata(tmp_path)

    result = ensure_shortcut(
        userdata,
        GAME_ID,
        "Jogo Teste",
    )

    path = shortcut_path(userdata)
    assert result.status == "created"
    assert result.path == path
    assert path.is_file()
    assert result.shortcut_key == "0"
    assert result.appid32 == _appid32(GAME_ID)
    assert result.appid_signed == signed_32(_appid32(GAME_ID))
    assert result.backup_path is None

    root = load_shortcuts_vdf(path)
    assert list(root.keys()) == ["shortcuts"]
    assert list(root["shortcuts"].keys()) == ["0"]
    entry = root["shortcuts"]["0"]
    assert list(entry.keys()) == list(DEFAULT_FIELD_NAMES)
    assert entry["appid"] == signed_32(_appid32(GAME_ID))
    assert entry["AppName"] == "Jogo Teste"
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    expected_exe = str(Path(system_root) / "System32" / "cmd.exe")
    assert entry["Exe"] == f'"{expected_exe}"'
    assert entry["StartDir"] == str(Path(system_root) / "System32") + "\\"
    assert entry["IsHidden"] == 0
    assert entry["AllowDesktopConfig"] == 1
    assert entry["AllowOverlay"] == 1
    assert entry["LastPlayTime"] == 0
    assert isinstance(entry["tags"], vdf.VDFDict)
    assert not entry["tags"]


def test_custom_name_and_hidden_flag_are_written(tmp_path: Path) -> None:
    userdata = _userdata(tmp_path)
    name = "Jogão ç"

    result = ensure_shortcut(
        userdata,
        GAME_ID,
        name,
        hidden=True,
    )

    entry = load_shortcuts_vdf(shortcut_path(userdata))["shortcuts"]["0"]
    assert result.name == name
    assert entry["AppName"] == name
    assert entry["IsHidden"] == 1


def test_custom_exe_is_quoted_and_start_dir_is_derived(
    tmp_path: Path,
) -> None:
    userdata = _userdata(tmp_path)
    exe = r"C:\Programas\Meu Jogo\game.exe"

    result = ensure_shortcut(userdata, GAME_ID, "Jogo", exe=exe)

    entry = result.entry
    assert entry is not None
    assert entry["Exe"] == f'"{exe}"'
    assert entry["StartDir"] == "C:\\Programas\\Meu Jogo\\"
    assert load_shortcuts_vdf(shortcut_path(userdata))["shortcuts"]["0"] == entry


def test_already_quoted_exe_is_preserved(tmp_path: Path) -> None:
    userdata = _userdata(tmp_path)
    exe = '"C:\\Games\\game.exe" --windowed'

    result = ensure_shortcut(userdata, GAME_ID, "Jogo", exe=exe)

    entry = result.entry
    assert entry is not None
    assert entry["Exe"] == exe
    assert entry["StartDir"] == "C:\\Games\\"


def test_existing_shortcut_is_preserved_with_backup_and_template_order(
    tmp_path: Path,
) -> None:
    userdata = _userdata(tmp_path)
    existing_appid = signed_32(0x80000001)
    original = _template_entry(existing_appid)
    path = _write_shortcuts(userdata, [original])
    original_bytes = path.read_bytes()

    result = ensure_shortcut(userdata, GAME_ID, "Novo Jogo", hidden=True)

    assert result.status == "created"
    assert result.shortcut_key == "1"
    assert result.backup_path is not None
    assert result.backup_path.read_bytes() == original_bytes

    root = load_shortcuts_vdf(path)
    assert list(root["shortcuts"].keys()) == ["0", "1"]
    assert dict(root["shortcuts"]["0"]) == original
    new_entry = root["shortcuts"]["1"]
    assert list(new_entry.keys()) == list(original.keys())
    assert new_entry["sortas"] == ""
    assert new_entry["extra_custom"] == 0
    assert new_entry["AppName"] == "Novo Jogo"
    assert new_entry["IsHidden"] == 1


def test_existing_appid_is_idempotent_and_does_not_write(
    tmp_path: Path,
) -> None:
    userdata = _userdata(tmp_path)
    appid = _appid32(GAME_ID)
    path = _write_shortcuts(
        userdata,
        [
            {
                "appid": signed_32(appid),
                "AppName": "Nome Existente",
                "sortas": "",
                "tags": vdf.VDFDict(),
            }
        ],
    )
    original_bytes = path.read_bytes()

    result = ensure_shortcut(userdata, GAME_ID, "Outro Nome")

    assert result.status == "already_exists"
    assert result.shortcut_key == "0"
    assert result.name == "Nome Existente"
    assert result.appid_signed == signed_32(appid)
    assert result.backup_path is None
    assert path.read_bytes() == original_bytes


def test_next_key_uses_highest_numeric_key(tmp_path: Path) -> None:
    userdata = _userdata(tmp_path)
    _write_shortcuts(
        userdata,
        [
            _template_entry(signed_32(0x80000001)),
            _template_entry(signed_32(0x80000002)),
        ],
    )

    result = ensure_shortcut(userdata, GAME_ID, "Jogo")

    assert result.shortcut_key == "2"
    assert list(load_shortcuts_vdf(shortcut_path(userdata))["shortcuts"]) == [
        "0",
        "1",
        "2",
    ]


def test_missing_shortcuts_block_is_created_without_losing_root(
    tmp_path: Path,
) -> None:
    userdata = _userdata(tmp_path)
    path = shortcut_path(userdata)
    path.parent.mkdir(parents=True)
    path.write_bytes(serialize_shortcuts_vdf({"outro": {"valor": 1}}))

    result = ensure_shortcut(userdata, GAME_ID, "Jogo")

    assert result.status == "created"
    root = load_shortcuts_vdf(path)
    assert root["outro"]["valor"] == 1
    assert root["shortcuts"]["0"]["AppName"] == "Jogo"


def test_dry_run_does_not_create_files_and_returns_data(
    tmp_path: Path,
) -> None:
    userdata = _userdata(tmp_path)

    result = ensure_shortcut(
        userdata,
        GAME_ID,
        "Jogo",
        hidden=True,
        dry_run=True,
    )

    assert result.status == "dry_run"
    assert result.shortcut_key == "0"
    assert result.pending is None
    assert result.entry is not None
    assert result.entry["AppName"] == "Jogo"
    assert result.entry["IsHidden"] == 1
    assert not shortcut_path(userdata).exists()
    assert not (userdata / "config").exists()


def test_steam_running_returns_pending_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shortcuts, "is_steam_running", lambda: True)
    userdata = _userdata(tmp_path)

    result = ensure_shortcut(userdata, GAME_ID, "Jogo")

    assert result.status == "pendente"
    assert result.steam_running is True
    assert result.pending is not None
    assert result.pending.path == shortcut_path(userdata)
    assert result.pending.game_id == GAME_ID
    assert result.pending.appid32 == _appid32(GAME_ID)
    assert result.pending.name == "Jogo"
    assert result.pending.shortcut_key == "0"
    assert result.pending.entry is result.entry
    assert not result.path.exists()
    assert not (userdata / "config").exists()


def test_force_writes_even_when_steam_is_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shortcuts, "is_steam_running", lambda: True)
    userdata = _userdata(tmp_path)

    result = ensure_shortcut(userdata, GAME_ID, "Jogo", force=True)

    assert result.status == "created"
    assert result.steam_running is True
    assert shortcut_path(userdata).is_file()


def test_invalid_game_id_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ShortcutError):
        ensure_shortcut(tmp_path, 730, "Jogo")
    with pytest.raises(ShortcutError):
        ensure_shortcut(tmp_path, str(GAME_ID), "   ")


def test_shortcut_game_id_round_trip(tmp_path: Path) -> None:
    appid = _appid32(GAME_ID)
    assert shortcut_game_id(appid) == GAME_ID
    result = ensure_shortcut(
        _userdata(tmp_path),
        shortcut_game_id(appid),
        "Jogo",
    )
    assert result.appid32 == appid


@pytest.mark.skipif(
    not REAL_SHORTCUTS.is_file(),
    reason="shortcuts.vdf real não encontrado",
)
def test_real_shortcuts_vdf_round_trips_byte_for_byte() -> None:
    original = REAL_SHORTCUTS.read_bytes()
    serialized = serialize_shortcuts_vdf(
        load_shortcuts_vdf(REAL_SHORTCUTS)
    )
    if serialized != original:
        index = next(
            (
                position
                for position, (left, right) in enumerate(
                    zip(original, serialized)
                )
                if left != right
            ),
            min(len(original), len(serialized)),
        )
        pytest.fail(
            "Round-trip alterou o shortcuts.vdf real: "
            f"tamanho {len(original)} -> {len(serialized)}, "
            f"primeira diferença no byte {index}"
        )
