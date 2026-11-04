from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .steam import (
    find_mapping_key,
    get_mapping_value,
    load_screenshots_vdf,
    load_shortcuts_vdf,
    shortcut_game_id,
    unsigned_32,
    vdf_text,
)

MAX_STEAM_APPID = 0xFFFFFFFF
MAX_GAME_ID = 0xFFFFFFFFFFFFFFFF
MAX_GAME_NAME_LENGTH = 120
MISSING = object()


class GameError(Exception):
    pass


class GameConfigError(GameError):
    pass


class GameValidationError(GameError, ValueError):
    pass


@dataclass(frozen=True)
class Game:
    name: str
    game_id: int
    kind: str
    source: str
    orphan: bool = False


def default_config_path() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "SteamShotBridge" / "config.json"
    if os.name == "nt":
        return Path.home() / "AppData" / "Roaming" / "SteamShotBridge" / "config.json"
    return Path.home() / ".config" / "SteamShotBridge" / "config.json"


def normalize_game_name(name: object) -> str:
    if not isinstance(name, str):
        raise GameValidationError("O nome do jogo deve ser um texto")
    cleaned = "".join(
        character
        for character in name
        if unicodedata.category(character) != "Cc"
    )
    normalized = re.sub(r"\s+", " ", cleaned).strip()
    if not normalized:
        raise GameValidationError("O nome do jogo não pode ficar vazio")
    if len(normalized) > MAX_GAME_NAME_LENGTH:
        raise GameValidationError(
            f"O nome do jogo não pode ter mais de {MAX_GAME_NAME_LENGTH} caracteres"
        )
    return normalized


def custom_game_id(name: object) -> int:
    normalized = normalize_game_name(name)
    appid32 = (
        zlib.crc32(normalized.encode("utf-8")) & 0xFFFFFFFF
    ) | 0x80000000
    return shortcut_game_id(appid32)


def _load_config(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {"custom_games": []}
    try:
        raw = path.read_text(encoding="utf-8-sig")
        data = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GameConfigError(
            f"Não foi possível ler {path}: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise GameConfigError(f"O config.json deve conter um objeto: {path}")
    custom_games = data.get("custom_games", [])
    if not isinstance(custom_games, list):
        raise GameConfigError(
            f"custom_games deve ser uma lista em {path}"
        )
    data["custom_games"] = custom_games
    return data


def _entry_game_id(entry: object) -> int | None:
    if not isinstance(entry, Mapping):
        return None
    raw = entry.get("game_id")
    if isinstance(raw, bool):
        return None
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        return None
    if value < 1 or value > MAX_GAME_ID:
        return None
    return value


def _entry_name(entry: object) -> str | None:
    if not isinstance(entry, Mapping):
        return None
    raw = entry.get("name")
    if not isinstance(raw, str):
        return None
    name = raw.strip()
    return name or None


def _parse_name_overrides(data: Mapping) -> dict[int, str]:
    raw_overrides = data.get("name_overrides", {})
    if not isinstance(raw_overrides, Mapping):
        return {}
    overrides: dict[int, str] = {}
    for raw_game_id, raw_name in raw_overrides.items():
        if isinstance(raw_game_id, bool):
            continue
        try:
            game_id = int(str(raw_game_id))
        except (TypeError, ValueError):
            continue
        if game_id < 1 or game_id > MAX_GAME_ID:
            continue
        if not isinstance(raw_name, str):
            continue
        name = raw_name.strip()
        if name:
            overrides[game_id] = name
    return overrides


def load_name_overrides(
    config_path: Path | str | None = None,
) -> dict[int, str]:
    path = (
        Path(config_path)
        if config_path is not None
        else default_config_path()
    )
    return _parse_name_overrides(_load_config(path))


def _app_game_names(config_path: Path) -> dict[int, str]:
    data = _load_config(config_path)
    names: dict[int, str] = {}

    custom_entries = data.get("custom_games", [])
    if isinstance(custom_entries, list):
        for entry in custom_entries:
            game_id = _entry_game_id(entry)
            name = _entry_name(entry)
            if game_id is not None and name is not None:
                names[game_id] = name

    exe_map = data.get("exe_map", {})
    if isinstance(exe_map, Mapping):
        for entry in exe_map.values():
            game_id = _entry_game_id(entry)
            name = _entry_name(entry)
            if game_id is not None and name is not None:
                names[game_id] = name

    names.update(_parse_name_overrides(data))
    return names


def _atomic_write_config(path: Path, data: Mapping) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def create_custom_game(
    name: str,
    config_path: Path | str | None = None,
) -> Game:
    normalized = normalize_game_name(name)
    game_id = custom_game_id(normalized)
    path = Path(config_path) if config_path is not None else default_config_path()
    data = _load_config(path)
    entries = data.get("custom_games", [])
    if not isinstance(entries, list):
        raise GameConfigError(f"custom_games deve ser uma lista em {path}")

    for entry in entries:
        if _entry_game_id(entry) == game_id:
            existing_name = _entry_name(entry) or normalized
            return Game(
                name=existing_name,
                game_id=game_id,
                kind="custom",
                source=str(path),
            )

    entries.append({"name": normalized, "game_id": str(game_id)})
    data["custom_games"] = entries
    _atomic_write_config(path, data)
    return Game(
        name=normalized,
        game_id=game_id,
        kind="custom",
        source=str(path),
    )


def _shortcut_games(
    userdata_dir: Path,
    shortcutnames: Mapping[int, str],
) -> tuple[list[Game], set[int]]:
    path = userdata_dir / "config" / "shortcuts.vdf"
    if not path.is_file():
        return [], set()
    try:
        root = load_shortcuts_vdf(path)
        shortcuts = find_mapping_key(root, "shortcuts")
    except Exception as exc:
        raise GameError(
            f"Não foi possível ler {path}: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(shortcuts, Mapping):
        return [], set()

    games: list[Game] = []
    game_ids: set[int] = set()
    for shortcut_key, shortcut in shortcuts.items():
        if not isinstance(shortcut, Mapping):
            continue
        stored_raw = get_mapping_value(shortcut, "appid", MISSING)
        stored_appid = unsigned_32(stored_raw)
        if stored_appid is None:
            continue
        game_id = shortcut_game_id(stored_appid)
        game_ids.add(game_id)
        appname = vdf_text(get_mapping_value(shortcut, "appname", "")).strip()
        display_name = shortcutnames.get(game_id)
        name = display_name or appname or f"Atalho {shortcut_key}"
        games.append(
            Game(
                name=name,
                game_id=game_id,
                kind="shortcut",
                source=f"{path} [{shortcut_key}]",
            )
        )
    return games, game_ids


def _custom_games(config_path: Path) -> list[Game]:
    data = _load_config(config_path)
    entries = data.get("custom_games", [])
    if not isinstance(entries, list):
        raise GameConfigError(f"custom_games deve ser uma lista em {config_path}")
    games: list[Game] = []
    for entry in entries:
        name = _entry_name(entry)
        game_id = _entry_game_id(entry)
        if name is None or game_id is None:
            continue
        games.append(
            Game(
                name=name,
                game_id=game_id,
                kind="custom",
                source=str(config_path),
            )
        )
    return games


def _load_shortcutnames(userdata_dir: Path) -> tuple[Path, dict[int, str]]:
    path = userdata_dir / "760" / "screenshots.vdf"
    if not path.is_file():
        return path, {}
    try:
        root = load_screenshots_vdf(path)
        screenshots = find_mapping_key(root, "screenshots")
    except Exception as exc:
        raise GameError(
            f"Não foi possível ler {path}: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(screenshots, Mapping):
        return path, {}
    shortcutnames = find_mapping_key(screenshots, "shortcutnames")
    if not isinstance(shortcutnames, Mapping):
        return path, {}

    names: dict[int, str] = {}
    for raw_game_id, raw_name in shortcutnames.items():
        if isinstance(raw_game_id, bool):
            continue
        try:
            game_id = int(str(raw_game_id))
        except (TypeError, ValueError):
            continue
        if game_id < 1 or game_id > 0xFFFFFFFFFFFFFFFF:
            continue
        name = vdf_text(raw_name).strip()
        if name and game_id not in names:
            names[game_id] = name
    return path, names


def _shortcutname_games(
    path: Path,
    shortcutnames: Mapping[int, str],
    shortcut_game_ids: set[int],
) -> list[Game]:
    return [
        Game(
            name=name,
            game_id=game_id,
            kind="shortcutname",
            source=str(path),
            orphan=True,
        )
        for game_id, name in shortcutnames.items()
        if game_id not in shortcut_game_ids
    ]


def _steam_games(userdata_dir: Path) -> list[Game]:
    remote = userdata_dir / "760" / "remote"
    if not remote.is_dir():
        return []
    games: list[Game] = []
    steam_path = None
    try:
        cand = userdata_dir.parents[1]
        if (cand / "steamapps").is_dir():
            steam_path = cand
    except Exception:
        steam_path = None

    from .steam_names import resolve_steam_game_name

    for entry in remote.iterdir():
        if not entry.is_dir() or not entry.name.isdigit():
            continue
        game_id = int(entry.name)
        if game_id > MAX_STEAM_APPID:
            continue

        res = resolve_steam_game_name(game_id, steam_path=steam_path, allow_network=False)
        display_name = res.name if res.name else f"AppID {game_id}"

        games.append(
            Game(
                name=display_name,
                game_id=game_id,
                kind="steam",
                source=str(entry),
            )
        )
    return games


def list_games(
    steam_userdata_dir: Path | str,
    config_path: Path | str | None = None,
) -> list[Game]:
    userdata_dir = Path(steam_userdata_dir)
    path = Path(config_path) if config_path is not None else default_config_path()
    app_names = _app_game_names(path)

    screenshots_path, shortcutnames = _load_shortcutnames(userdata_dir)
    shortcut_games, shortcut_game_ids = _shortcut_games(
        userdata_dir,
        shortcutnames,
    )
    source_games = [
        shortcut_games,
        _custom_games(path),
        _shortcutname_games(
            screenshots_path,
            shortcutnames,
            shortcut_game_ids,
        ),
        _steam_games(userdata_dir),
    ]

    games: list[Game] = []
    seen: set[int] = set()
    for batch in source_games:
        for game in batch:
            if game.game_id in seen:
                continue
            seen.add(game.game_id)
            app_name = app_names.get(game.game_id)
            if app_name is not None and app_name != game.name:
                game = Game(
                    name=app_name,
                    game_id=game.game_id,
                    kind=game.kind,
                    source=game.source,
                    orphan=game.orphan,
                )
            games.append(game)

    return sorted(games, key=lambda game: (game.name.casefold(), game.game_id))
