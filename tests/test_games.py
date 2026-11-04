from __future__ import annotations

import json
import re
import zlib
from pathlib import Path

import pytest
import vdf

from steamshotbridge import games as games_module
from steamshotbridge import shortcuts as shortcuts_module
from steamshotbridge.games import (
    Game,
    GameValidationError,
    create_custom_game,
    custom_game_id,
    default_config_path,
    list_games,
    normalize_game_name,
)
from steamshotbridge.shortcuts import shortcut_path
from steamshotbridge.steam import load_shortcuts_vdf, shortcut_game_id
from tools.list_games import main as list_games_main


@pytest.fixture(autouse=True)
def _steam_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shortcuts_module, "is_steam_running", lambda: False)


def _userdata(tmp_path: Path) -> Path:
    userdata = tmp_path / "Steam" / "userdata" / "12345"
    userdata.mkdir(parents=True)
    return userdata


def _write_shortcuts(userdata: Path, shortcuts: list[dict[str, object]]) -> Path:
    path = userdata / "config" / "shortcuts.vdf"
    path.parent.mkdir(parents=True)
    data = {
        "shortcuts": {
            str(index): shortcut
            for index, shortcut in enumerate(shortcuts)
        }
    }
    path.write_bytes(vdf.binary_dumps(data))
    return path


def _escape_vdf(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _write_screenshots(
    userdata: Path,
    shortcutnames: dict[int, str],
) -> Path:
    path = userdata / "760" / "screenshots.vdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ['"screenshots"', "{", '    "shortcutnames"', "    {"]
    for game_id, name in shortcutnames.items():
        lines.append(
            f'        "{game_id}"\t\t"{_escape_vdf(name)}"'
        )
    lines.extend(["    }", "}", ""])
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _write_empty_screenshots(userdata: Path) -> Path:
    return _write_screenshots(userdata, {})


def _write_config(
    path: Path,
    custom_games: list[dict[str, str]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"custom_games": custom_games},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _write_remote_game(
    userdata: Path,
    game_id: int | str,
    screenshots: int = 0,
) -> Path:
    folder = userdata / "760" / "remote" / str(game_id) / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    for number in range(screenshots):
        (folder / f"2026010100000{number}_1.jpg").write_bytes(b"jpeg")
    return folder


def test_shortcut_uses_negative_stored_appid(tmp_path: Path) -> None:
    userdata = _userdata(tmp_path)
    _write_shortcuts(
        userdata,
        [{"appname": "Teste importação", "appid": -140632515}],
    )
    config = tmp_path / "config.json"

    games = list_games(userdata, config)

    assert len(games) == 1
    game = games[0]
    assert game.game_id == 17842732021063876608
    assert game.game_id == ((-140632515 & 0xFFFFFFFF) << 32) | 0x02000000
    assert game.name == "Teste importação"
    assert game.kind == "shortcut"
    assert game.orphan is False
    assert "shortcuts.vdf" in game.source


def test_missing_shortcuts_and_screenshots_are_ignored(tmp_path: Path) -> None:
    userdata = _userdata(tmp_path)

    assert list_games(userdata, tmp_path / "missing.json") == []


def test_orphan_shortcutnames_are_marked(tmp_path: Path) -> None:
    userdata = _userdata(tmp_path)
    linked_appid = -140632515
    linked_id = shortcut_game_id(linked_appid)
    orphan_id = shortcut_game_id(999)
    _write_shortcuts(
        userdata,
        [{"appname": "Atalho", "appid": linked_appid}],
    )
    _write_screenshots(
        userdata,
        {
            linked_id: "Nome Exibido",
            orphan_id: "Órfã",
        },
    )

    games = list_games(userdata, tmp_path / "config.json")
    by_id = {game.game_id: game for game in games}

    assert len(games) == 2
    assert by_id[linked_id].kind == "shortcut"
    assert by_id[linked_id].name == "Nome Exibido"
    assert by_id[linked_id].orphan is False
    assert by_id[orphan_id].name == "Órfã"
    assert by_id[orphan_id].kind == "shortcutname"
    assert by_id[orphan_id].orphan is True
    assert "screenshots.vdf" in by_id[orphan_id].source


def test_source_priority_uses_game_id_not_name(tmp_path: Path) -> None:
    userdata = _userdata(tmp_path)
    config = tmp_path / "config.json"
    shortcut_id = shortcut_game_id(100)
    custom_and_shortcutname_id = shortcut_game_id(200)
    custom_low_id = 730
    shortcutname_low_id = 731

    _write_shortcuts(
        userdata,
        [{"appname": "Shortcut H1", "appid": 100}],
    )
    _write_screenshots(
        userdata,
        {
            shortcut_id: "Name H1",
            custom_and_shortcutname_id: "Name H2",
            custom_low_id: "Name 730",
            shortcutname_low_id: "Name 731",
        },
    )
    _write_config(
        config,
        [
            {"name": "Custom H1", "game_id": str(shortcut_id)},
            {
                "name": "Custom H2",
                "game_id": str(custom_and_shortcutname_id),
            },
            {"name": "Custom 730", "game_id": str(custom_low_id)},
        ],
    )
    _write_remote_game(userdata, custom_low_id)
    _write_remote_game(userdata, shortcutname_low_id)

    games = list_games(userdata, config)
    by_id = {game.game_id: game for game in games}

    assert len(games) == 4
    assert by_id[shortcut_id].kind == "shortcut"
    assert by_id[shortcut_id].name == "Name H1"
    assert by_id[custom_and_shortcutname_id].kind == "custom"
    assert by_id[custom_and_shortcutname_id].name == "Custom H2"
    assert by_id[custom_low_id].kind == "custom"
    assert by_id[custom_low_id].name == "Custom 730"
    assert by_id[shortcutname_low_id].kind == "shortcutname"
    assert by_id[shortcutname_low_id].orphan is True


def test_duplicate_names_remain_distinct_by_game_id(tmp_path: Path) -> None:
    userdata = _userdata(tmp_path)
    _write_shortcuts(
        userdata,
        [
            {"appname": "Teste importação", "appid": -1},
            {"appname": "Teste importação", "appid": -2},
        ],
    )

    games = list_games(userdata, tmp_path / "config.json")

    assert len(games) == 2
    assert {game.name for game in games} == {"Teste importação"}
    assert {game.game_id for game in games} == {
        shortcut_game_id(-1),
        shortcut_game_id(-2),
    }
    assert {game.kind for game in games} == {"shortcut"}


def test_custom_game_is_deterministic_and_not_duplicated(
    tmp_path: Path,
) -> None:
    config = tmp_path / "SteamShotBridge" / "config.json"
    name = "Jogo Custom"
    expected_appid = (
        zlib.crc32(name.encode("utf-8")) & 0xFFFFFFFF
    ) | 0x80000000
    expected_game_id = (expected_appid << 32) | 0x02000000

    first = create_custom_game(f"  {name}  ", config)
    second = create_custom_game(name, config)

    assert first == second == Game(
        name=name,
        game_id=expected_game_id,
        kind="custom",
        source=str(config),
    )
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data == {
        "custom_games": [
            {"name": name, "game_id": str(expected_game_id)}
        ]
    }


def test_custom_game_default_path_uses_appdata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))

    assert default_config_path() == (
        tmp_path / "Roaming" / "SteamShotBridge" / "config.json"
    )


def test_config_write_is_atomic_and_preserves_previous_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.json"
    create_custom_game("Primeiro", config)
    before = config.read_bytes()

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("falha simulada")

    monkeypatch.setattr(games_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="falha simulada"):
        create_custom_game("Segundo", config)

    assert config.read_bytes() == before
    assert not list(tmp_path.glob(".config.json.*.tmp"))


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "\tNome",
        "Nome\n",
        "Nome\x00Interno",
        "Nome\x7f",
    ],
)
def test_custom_game_rejects_invalid_names(
    tmp_path: Path,
    name: str,
) -> None:
    config = tmp_path / "config.json"

    with pytest.raises(GameValidationError):
        create_custom_game(name, config)

    assert not config.exists()


def test_normalize_game_name_rejects_non_text_and_preserves_spaces() -> None:
    assert normalize_game_name("  Jogo  ") == "Jogo"
    with pytest.raises(GameValidationError):
        normalize_game_name(None)


def test_list_cli_prints_ids_kinds_names_and_screenshot_counts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    userdata = _userdata(tmp_path)
    config = tmp_path / "config.json"
    orphan_id = shortcut_game_id(999)
    _write_shortcuts(
        userdata,
        [{"appname": "Atalho", "appid": -140632515}],
    )
    _write_screenshots(userdata, {orphan_id: "Órfã"})
    _write_remote_game(userdata, 999, screenshots=2)
    _write_config(
        config,
        [{"name": "Custom", "game_id": str(shortcut_game_id(500))}],
    )
    config_before = config.read_bytes()

    exit_code = list_games_main(
        [
            "list",
            "--steam-userdata",
            str(userdata),
            "--config",
            str(config),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert "GAME_ID" in captured.out
    assert "SCREENSHOTS" in captured.out
    assert re.search(
        r"^999\s+steam\s+AppID 999\s+2$",
        captured.out,
        re.MULTILINE,
    )
    assert "shortcutname (órfã)" in captured.out
    assert "Atalho" in captured.out
    assert "Custom" in captured.out
    assert config.read_bytes() == config_before


def test_list_cli_accepts_options_before_subcommand(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    userdata = _userdata(tmp_path)
    config = tmp_path / "config.json"

    exit_code = list_games_main(
        [
            "--steam-userdata",
            str(userdata),
            "--config",
            str(config),
            "list",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "GAME_ID" in captured.out
    assert captured.err == ""


def test_add_cli_creates_custom_game_and_prints_id(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "config.json"

    exit_code = list_games_main(
        [
            "add",
            "--name",
            "Novo Jogo",
            "--config",
            str(config),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert int(captured.out.strip()) == custom_game_id("Novo Jogo")
    assert json.loads(config.read_text(encoding="utf-8"))[
        "custom_games"
    ] == [
        {
            "name": "Novo Jogo",
            "game_id": str(custom_game_id("Novo Jogo")),
        }
    ]


def test_create_shortcut_cli_by_name(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    userdata = _userdata(tmp_path)
    config = tmp_path / "config.json"
    name = "Jogo Custom"
    game = create_custom_game(name, config)

    exit_code = list_games_main(
        [
            "create-shortcut",
            "--name",
            name,
            "--steam-userdata",
            str(userdata),
            "--config",
            str(config),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert "Status: created" in captured.out
    assert f"Jogo: {game.game_id} ({name})" in captured.out
    root = load_shortcuts_vdf(shortcut_path(userdata))
    entry = root["shortcuts"]["0"]
    assert entry["AppName"] == name
    assert entry["appid"] == -(0x100000000 - (game.game_id >> 32))


def test_create_shortcut_cli_by_game_id_hidden_and_dry_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    userdata = _userdata(tmp_path)
    config = tmp_path / "config.json"
    name = "Jogo Oculto"
    game = create_custom_game(name, config)

    dry_exit = list_games_main(
        [
            "create-shortcut",
            "--game-id",
            str(game.game_id),
            "--hidden",
            "--dry-run",
            "--steam-userdata",
            str(userdata),
            "--config",
            str(config),
        ]
    )

    captured = capsys.readouterr()
    unsigned_appid = game.game_id >> 32
    signed_appid = -(0x100000000 - unsigned_appid)
    assert dry_exit == 0
    assert "Status: dry_run" in captured.out
    assert "Campos do atalho que seria adicionado:" in captured.out
    assert f"  appid: int = {signed_appid}" in captured.out
    assert f"  AppName: string = '{name}'" in captured.out
    assert "  IsHidden: int = 1" in captured.out
    assert "  tags: dict = {}" in captured.out
    assert f"appid int32 com sinal: {signed_appid}" in captured.out
    assert (
        f"appid uint32 decimal sem sinal: {unsigned_appid}"
        in captured.out
    )
    assert (
        captured.out.index("  appid: int =")
        < captured.out.index(f"  AppName: string = '{name}'")
        < captured.out.index("  tags: dict = {}")
        < captured.out.index("appid int32 com sinal:")
    )
    assert not shortcut_path(userdata).exists()
    assert not (userdata / "config").exists()

    exit_code = list_games_main(
        [
            "create-shortcut",
            "--game-id",
            str(game.game_id),
            "--hidden",
            "--steam-userdata",
            str(userdata),
            "--config",
            str(config),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Status: created" in captured.out
    entry = load_shortcuts_vdf(shortcut_path(userdata))["shortcuts"]["0"]
    assert entry["AppName"] == name
    assert entry["IsHidden"] == 1


def test_create_shortcut_cli_rejects_steam_game(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    userdata = _userdata(tmp_path)
    config = tmp_path / "config.json"
    _write_remote_game(userdata, 730)

    exit_code = list_games_main(
        [
            "create-shortcut",
            "--game-id",
            "730",
            "--steam-userdata",
            str(userdata),
            "--config",
            str(config),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "jogo não Steam de 64 bits" in captured.err
    assert not shortcut_path(userdata).exists()


def test_create_shortcut_cli_pending_when_steam_running(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    userdata = _userdata(tmp_path)
    config = tmp_path / "config.json"
    name = "Jogo Pendente"
    game = create_custom_game(name, config)
    monkeypatch.setattr(shortcuts_module, "is_steam_running", lambda: True)

    exit_code = list_games_main(
        [
            "create-shortcut",
            "--game-id",
            str(game.game_id),
            "--steam-userdata",
            str(userdata),
            "--config",
            str(config),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Status: pendente" in captured.out
    assert "Atalho pendente:" in captured.out
    assert not shortcut_path(userdata).exists()
