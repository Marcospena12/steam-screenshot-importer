from __future__ import annotations

from pathlib import Path

import pytest

from steamshotbridge.steam import (
    UserDataError,
    calculate_shortcut_ids,
    find_steam_installation,
    list_userdata_ids,
    resolve_userdata_id,
    shortcut_game_id,
    signed_32,
    unsigned_32,
)


def test_calculate_shortcut_ids_preserves_quoted_exe() -> None:
    exe = '"C:\\Games\\My Game\\game.exe"'
    appname = "My Game"

    result = calculate_shortcut_ids(exe, appname)

    assert result.crc_input == '"C:\\Games\\My Game\\game.exe"My Game'
    assert result.appid32 == 2865894197
    assert f"0x{result.appid32:08X}" == "0xAAD21335"
    assert result.gameid64 == 12308921849944735744
    assert f"0x{result.gameid64:016X}" == "0xAAD2133502000000"


def test_crc_input_does_not_trim_or_add_quotes() -> None:
    result = calculate_shortcut_ids("game.exe", "Game")

    assert result.crc_input == "game.exeGame"


def test_finds_first_existing_steam_candidate(tmp_path: Path) -> None:
    steam = tmp_path / "Steam"
    steam.mkdir()

    result = find_steam_installation(
        [
            ("inexistente", tmp_path / "missing"),
            ("teste", steam),
        ]
    )

    assert result.path == steam
    assert result.source == "teste"


def test_lists_numeric_userdata_sorted_and_resolves_default(tmp_path: Path) -> None:
    steam = tmp_path / "Steam"
    userdata = steam / "userdata"
    userdata.mkdir(parents=True)
    (userdata / "900").mkdir()
    (userdata / "100").mkdir()
    (userdata / "not-a-user").mkdir()

    assert list_userdata_ids(steam) == ["100", "900"]
    assert resolve_userdata_id(steam) == ("100", "primeiro userdata encontrado")
    assert resolve_userdata_id(steam, "900") == (
        "900",
        "argumento da linha de comando",
    )

    with pytest.raises(UserDataError):
        resolve_userdata_id(steam, "200")


def test_signed_and_unsigned_helpers() -> None:
    assert unsigned_32(-1) == 0xFFFFFFFF
    assert signed_32(0xFFFFFFFF) == -1
    assert signed_32(0x7FFFFFFF) == 0x7FFFFFFF


def test_shortcut_game_id_uses_stored_appid_without_recalculating_crc() -> None:
    assert shortcut_game_id(4154334781) == 17842732021063876608
    assert shortcut_game_id(-140632515) == 17842732021063876608
