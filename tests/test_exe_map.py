from __future__ import annotations

import json
import zlib
from pathlib import Path

import pytest
import vdf

from steamshotbridge.exe_map import (
    ExeCollisionError,
    generate_game_id,
    list_exe_map,
    load_exe_map,
    resolve_exe,
)
from steamshotbridge.steam import signed_32


def _expected_game_id(exe: str) -> int:
    crc = zlib.crc32(exe.lower().encode("utf-8")) & 0xFFFFFFFF
    return ((crc | 0x80000000) << 32) | 0x02000000


def _write_config(path: Path, data: dict[str, object]) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_shortcut_with_game_id(userdata: Path, game_id: int, name: str) -> None:
    path = userdata / "config" / "shortcuts.vdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    appid32 = (game_id >> 32) & 0xFFFFFFFF
    root = {
        "shortcuts": {
            "0": {
                "appid": signed_32(appid32),
                "AppName": name,
                "tags": {},
            }
        }
    }
    path.write_bytes(vdf.binary_dumps(root))


def test_generate_game_id_matches_spec_formula() -> None:
    assert generate_game_id("aomx") == _expected_game_id("aomx")
    assert generate_game_id("AOMX") == _expected_game_id("aomx")


def test_new_exe_is_created_as_provisional_with_original_case(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.json"

    entry = resolve_exe("ProjectZomboid64", config)

    assert entry.is_new is True
    assert entry.key == "projectzomboid64"
    assert entry.name == "ProjectZomboid64"
    assert entry.provisional is True
    assert entry.game_id == _expected_game_id("projectzomboid64")
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data == {
        "exe_map": {
            "projectzomboid64": {
                "name": "ProjectZomboid64",
                "game_id": str(_expected_game_id("projectzomboid64")),
                "provisional": True,
                "source": "exe",
            }
        }
    }


def test_existing_exe_reuses_stored_name_and_game_id(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    first = resolve_exe("aomx", config)
    second = resolve_exe("AOMX", config)

    assert second.is_new is False
    assert second.game_id == first.game_id
    assert second.name == "aomx"
    assert second.provisional is True


def test_game_id_is_stable_after_name_is_changed_in_config(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.json"
    first = resolve_exe("aomx", config)
    data = json.loads(config.read_text(encoding="utf-8"))
    data["exe_map"]["aomx"]["name"] = "Age of Mythology"
    config.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    second = resolve_exe("aomx", config)

    assert second.game_id == first.game_id
    assert second.name == "Age of Mythology"
    assert second.is_new is False


def test_collision_with_existing_map_entry_is_rejected_without_write(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.json"
    conflicting = _expected_game_id("aomx")
    _write_config(
        config,
        {
            "exe_map": {
                "outro": {
                    "name": "Outro",
                    "game_id": str(conflicting),
                    "provisional": True,
                }
            }
        },
    )
    before = config.read_bytes()

    with pytest.raises(ExeCollisionError, match="colide"):
        resolve_exe("aomx", config)

    assert config.read_bytes() == before


def test_collision_with_list_games_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    userdata = tmp_path / "Steam" / "userdata" / "12345"
    userdata.mkdir(parents=True)
    _write_shortcut_with_game_id(
        userdata,
        _expected_game_id("aomx"),
        "Jogo Existente",
    )

    with pytest.raises(ExeCollisionError, match="list_games"):
        resolve_exe("aomx", config, userdata)

    assert not config.exists()


def test_config_preserves_other_keys(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    _write_config(
        config,
        {
            "custom_games": [
                {"name": "Custom", "game_id": "123456789"}
            ]
        },
    )

    resolve_exe("aomx", config)

    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["custom_games"] == [
        {"name": "Custom", "game_id": "123456789"}
    ]
    assert "aomx" in data["exe_map"]


def test_list_and_load_exe_map(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    resolve_exe("b_exe", config)
    resolve_exe("a_exe", config)

    loaded = load_exe_map(config)
    listed = list_exe_map(config)

    assert list(loaded) == ["a_exe", "b_exe"]
    assert [entry.key for entry in listed] == ["a_exe", "b_exe"]
