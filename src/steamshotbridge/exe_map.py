from __future__ import annotations

import binascii
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

import vdf

from .games import GameError, default_config_path, list_games, normalize_game_name
from .importer import (
    _atomic_write,
    _create_backup,
    load_vdf_document,
    serialize_screenshots_vdf,
)
from .name_resolver import resolve_game_name
from .pending import (
    default_pending_path,
    enqueue_rename,
    update_pending_game_name,
)
from .shortcuts import serialize_shortcuts_vdf
from .steam import (
    SHORTCUT_GAMEID_SUFFIX,
    find_mapping_key,
    get_mapping_value,
    is_steam_running,
    load_shortcuts_vdf,
    unsigned_32,
)

MAX_GAME_ID = 0xFFFFFFFFFFFFFFFF
MAX_STEAM_APPID = 0xFFFFFFFF
MISSING = object()


class ExeMapError(Exception):
    pass


class ExeCollisionError(ExeMapError):
    pass


@dataclass(frozen=True)
class ExeMapping:
    key: str
    exe: str
    name: str
    game_id: int
    provisional: bool
    is_new: bool = False
    source: str = "exe"
    ignored: bool = False


@dataclass(frozen=True)
class RenameResult:
    game_id: int
    name: str
    status: str
    exe_map_updated: bool = False
    custom_updated: bool = False
    name_override_saved: bool = False
    shortcut_updated: bool = False
    shortcutname_updated: bool = False


def _load_config(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8-sig")
        data = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExeMapError(
            f"Não foi possível ler {path}: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ExeMapError(f"O config.json deve conter um objeto: {path}")
    return data


def _atomic_write_json(path: Path, data: Mapping) -> None:
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


def _parse_entry(key: str, raw: object, path: Path) -> ExeMapping:
    if not isinstance(raw, Mapping):
        raise ExeMapError(f"Entrada exe_map inválida para '{key}' em {path}")

    name_raw = raw.get("name", key)
    name = str(name_raw).strip() if name_raw is not None else key
    if not name:
        raise ExeMapError(f"Entrada exe_map sem nome para '{key}' em {path}")

    game_raw = raw.get("game_id")
    try:
        game_id = int(str(game_raw))
    except (TypeError, ValueError) as exc:
        raise ExeMapError(
            f"game_id inválido em exe_map['{key}'] de {path}: {game_raw!r}"
        ) from exc
    if game_id < 1 or game_id > MAX_GAME_ID:
        raise ExeMapError(
            f"game_id fora do intervalo em exe_map['{key}'] de {path}"
        )

    provisional_raw = raw.get("provisional", True)
    provisional = (
        provisional_raw
        if isinstance(provisional_raw, bool)
        else bool(provisional_raw)
    )

    source_raw = raw.get("source")
    source = (
        str(source_raw).strip()
        if source_raw
        else ("exe" if provisional else "manual")
    )

    ignored_raw = raw.get("ignored", False)
    ignored = (
        ignored_raw
        if isinstance(ignored_raw, bool)
        else bool(ignored_raw)
    )

    return ExeMapping(
        key=key,
        exe=key,
        name=name,
        game_id=game_id,
        provisional=provisional,
        source=source,
        ignored=ignored,
    )


def load_exe_map(config_path: Path | str | None = None) -> dict[str, ExeMapping]:
    path = (
        Path(config_path)
        if config_path is not None
        else default_config_path()
    )
    data = _load_config(path)
    raw_map = data.get("exe_map", {})
    if raw_map is None:
        raw_map = {}
    if not isinstance(raw_map, Mapping):
        raise ExeMapError(f"exe_map deve ser um objeto em {path}")

    entries: dict[str, ExeMapping] = {}
    for raw_key, raw_entry in raw_map.items():
        key = str(raw_key).lower()
        entries[key] = _parse_entry(key, raw_entry, path)
    return entries


def save_exe_map(
    config_path: Path | str | None,
    entries: Mapping[str, ExeMapping],
) -> None:
    path = (
        Path(config_path)
        if config_path is not None
        else default_config_path()
    )
    data = _load_config(path)
    exe_map_data: dict[str, dict] = {}
    for entry in sorted(entries.values(), key=lambda item: item.key):
        item_dict = {
            "name": entry.name,
            "game_id": str(entry.game_id),
            "provisional": bool(entry.provisional),
            "source": entry.source,
        }
        if entry.ignored:
            item_dict["ignored"] = True
        exe_map_data[entry.key] = item_dict
    data["exe_map"] = exe_map_data
    _atomic_write_json(path, data)


def generate_game_id(exe: str) -> int:
    crc = binascii.crc32(exe.lower().encode("utf-8")) & 0xFFFFFFFF
    appid32 = crc | 0x80000000
    return (appid32 << 32) | SHORTCUT_GAMEID_SUFFIX


def find_exe(
    exe: str,
    config_path: Path | str | None = None,
) -> ExeMapping | None:
    path = (
        Path(config_path)
        if config_path is not None
        else default_config_path()
    )
    key = exe.lower()
    entries = load_exe_map(path)
    stored = entries.get(key)
    if stored is None:
        return None
    return ExeMapping(
        key=key,
        exe=exe,
        name=stored.name,
        game_id=stored.game_id,
        provisional=stored.provisional,
        source=stored.source,
        ignored=stored.ignored,
    )


def resolve_exe(
    exe: str,
    config_path: Path | str | None = None,
    steam_userdata_dir: Path | str | None = None,
    dry_run: bool = False,
    name_resolver_fn: Callable[[str], tuple[str, str]] | None = None,
) -> ExeMapping:
    path = (
        Path(config_path)
        if config_path is not None
        else default_config_path()
    )
    key = exe.lower()
    entries = load_exe_map(path)

    stored = entries.get(key)
    if stored is not None:
        return ExeMapping(
            key=key,
            exe=exe,
            name=stored.name,
            game_id=stored.game_id,
            provisional=stored.provisional,
            source=stored.source,
            ignored=stored.ignored,
        )

    game_id = generate_game_id(key)
    for other_key, other in entries.items():
        if other.game_id == game_id:
            raise ExeCollisionError(
                f"game_id {game_id} gerado para o exe '{exe}' colide com "
                f"'{other_key}' ({other.name}) no exe_map"
            )

    if steam_userdata_dir is not None:
        try:
            games = list_games(steam_userdata_dir, path)
        except GameError as exc:
            raise ExeMapError(
                f"Não foi possível verificar colisões em {steam_userdata_dir}: "
                f"{exc}"
            ) from exc
        for game in games:
            if game.game_id == game_id:
                raise ExeCollisionError(
                    f"game_id {game_id} gerado para o exe '{exe}' colide com "
                    f"o jogo '{game.name}' ({game.kind}) em list_games"
                )

    resolver = name_resolver_fn or resolve_game_name
    resolved_name, source = resolver(exe)
    provisional = source == "exe"

    entry = ExeMapping(
        key=key,
        exe=exe,
        name=resolved_name,
        game_id=game_id,
        provisional=provisional,
        is_new=True,
        source=source,
    )
    if not dry_run:
        entries[key] = entry
        save_exe_map(path, entries)
    return entry


def update_exe_mapping(
    key: str,
    name: str,
    source: str,
    provisional: bool,
    config_path: Path | str | None = None,
) -> ExeMapping:
    path = (
        Path(config_path)
        if config_path is not None
        else default_config_path()
    )
    entries = load_exe_map(path)
    k = key.lower()
    stored = entries.get(k)
    if stored is None:
        raise ExeMapError(f"Executável '{key}' não encontrado no exe_map")
    updated = ExeMapping(
        key=stored.key,
        exe=stored.exe,
        name=name,
        game_id=stored.game_id,
        provisional=provisional,
        source=source,
        ignored=stored.ignored,
    )
    entries[k] = updated
    save_exe_map(path, entries)
    return updated


def is_shortcut_applied(
    steam_userdata_dir: Path | str,
    game_id: int | str,
) -> bool:
    userdata = Path(steam_userdata_dir)
    gid = int(game_id)
    appid32 = (gid >> 32) & 0xFFFFFFFF

    shortcuts_path = userdata / "config" / "shortcuts.vdf"
    if shortcuts_path.is_file():
        try:
            root = load_shortcuts_vdf(shortcuts_path)
            shortcuts = find_mapping_key(root, "shortcuts")
            if isinstance(shortcuts, Mapping):
                for s in shortcuts.values():
                    if isinstance(s, Mapping):
                        stored = unsigned_32(get_mapping_value(s, "appid", MISSING))
                        if stored == appid32:
                            return True
        except Exception:
            pass

    screenshots_path = userdata / "760" / "screenshots.vdf"
    if screenshots_path.is_file():
        try:
            doc = load_vdf_document(screenshots_path)
            screenshots = find_mapping_key(doc.root, "screenshots")
            if isinstance(screenshots, Mapping):
                if str(gid) in screenshots:
                    return True
                shortcutnames = find_mapping_key(screenshots, "shortcutnames")
                if isinstance(shortcutnames, Mapping) and str(gid) in shortcutnames:
                    return True
        except Exception:
            pass

    return False


def _parse_config_game_id(raw: object, label: str) -> int:
    try:
        value = int(str(raw))
    except (TypeError, ValueError) as exc:
        raise ExeMapError(f"game_id inválido em {label}: {raw!r}") from exc
    if value < 1 or value > MAX_GAME_ID:
        raise ExeMapError(f"game_id fora do intervalo em {label}: {value}")
    return value


def _update_config_for_rename(
    config: Path,
    game_id: int,
    name: str,
) -> tuple[bool, bool, bool]:
    data = _load_config(config)
    changed = False

    exe_map_updated = False
    raw_exe_map = data.get("exe_map", {})
    if raw_exe_map is None:
        raw_exe_map = {}
    if not isinstance(raw_exe_map, Mapping):
        raise ExeMapError(f"exe_map deve ser um objeto em {config}")
    new_exe_map: dict[object, object] = {}
    for raw_key, raw_entry in raw_exe_map.items():
        if not isinstance(raw_entry, Mapping):
            raise ExeMapError(
                f"Entrada exe_map inválida para '{raw_key}' em {config}"
            )
        entry_game_id = _parse_config_game_id(
            raw_entry.get("game_id"),
            f"exe_map['{raw_key}']",
        )
        if entry_game_id == game_id:
            updated_entry = dict(raw_entry)
            updated_entry["name"] = name
            updated_entry["provisional"] = False
            updated_entry["source"] = "manual"
            new_exe_map[raw_key] = updated_entry
            exe_map_updated = True
        else:
            new_exe_map[raw_key] = raw_entry
    if exe_map_updated:
        data["exe_map"] = new_exe_map
        changed = True

    custom_updated = False
    raw_custom = data.get("custom_games", [])
    if raw_custom is None:
        raw_custom = []
    if not isinstance(raw_custom, list):
        raise ExeMapError(f"custom_games deve ser uma lista em {config}")
    new_custom: list[object] = []
    for entry in raw_custom:
        if not isinstance(entry, Mapping):
            new_custom.append(entry)
            continue
        try:
            entry_game_id = int(str(entry.get("game_id")))
        except (TypeError, ValueError):
            new_custom.append(entry)
            continue
        if entry_game_id == game_id:
            updated_entry = dict(entry)
            updated_entry["name"] = name
            new_custom.append(updated_entry)
            custom_updated = True
        else:
            new_custom.append(entry)
    if custom_updated:
        data["custom_games"] = new_custom
        changed = True

    raw_overrides = data.get("name_overrides")
    if raw_overrides is None:
        overrides: dict[str, object] = {}
    elif isinstance(raw_overrides, Mapping):
        overrides = {
            str(key): value
            for key, value in raw_overrides.items()
        }
    else:
        raise ExeMapError(f"name_overrides deve ser um objeto em {config}")

    override_key = str(game_id)
    override_saved = False
    if exe_map_updated or custom_updated:
        if override_key in overrides:
            del overrides[override_key]
            changed = True
        if overrides:
            data["name_overrides"] = overrides
        elif raw_overrides is not None:
            data["name_overrides"] = {}
    else:
        if overrides.get(override_key) != name:
            overrides[override_key] = name
            changed = True
        data["name_overrides"] = overrides
        override_saved = True

    if changed:
        _atomic_write_json(config, data)
    return exe_map_updated, custom_updated, override_saved


def _set_mapping_value_casefold(
    mapping: Mapping,
    key: str,
    value: object,
) -> None:
    existing_key = next(
        (
            candidate
            for candidate in mapping.keys()
            if str(candidate).casefold() == key.casefold()
        ),
        None,
    )
    mapping[existing_key if existing_key is not None else key] = value


def _update_shortcut_name(
    userdata_dir: Path,
    game_id: int,
    name: str,
) -> bool:
    path = userdata_dir / "config" / "shortcuts.vdf"
    if not path.is_file():
        return False
    root = load_shortcuts_vdf(path)
    shortcuts = find_mapping_key(root, "shortcuts")
    if not isinstance(shortcuts, Mapping):
        return False

    appid32 = (game_id >> 32) & 0xFFFFFFFF
    changed = False
    for shortcut in shortcuts.values():
        if not isinstance(shortcut, Mapping):
            continue
        if unsigned_32(get_mapping_value(shortcut, "appid", MISSING)) == appid32:
            _set_mapping_value_casefold(shortcut, "AppName", name)
            changed = True

    if changed:
        _create_backup(path)
        _atomic_write(path, serialize_shortcuts_vdf(root))
    return changed


def _update_shortcutname(
    userdata_dir: Path,
    game_id: int,
    name: str,
) -> bool:
    path = userdata_dir / "760" / "screenshots.vdf"
    if not path.is_file():
        return False

    doc = load_vdf_document(path)
    screenshots = find_mapping_key(doc.root, "screenshots")
    if not isinstance(screenshots, Mapping):
        raise ExeMapError(
            f"O screenshots.vdf não contém um bloco 'screenshots': {path}"
        )

    shortcutnames = find_mapping_key(screenshots, "shortcutnames")
    if shortcutnames is None:
        shortcutnames = vdf.VDFDict()
        screenshots["shortcutnames"] = shortcutnames
    elif not isinstance(shortcutnames, vdf.VDFDict):
        if not isinstance(shortcutnames, Mapping):
            raise ExeMapError(
                f"O bloco shortcutnames de {path} não é um dicionário"
            )
        mutable = vdf.VDFDict()
        for key, value in shortcutnames.items():
            mutable[key] = value
        existing_key = next(
            (
                candidate
                for candidate in screenshots.keys()
                if str(candidate).casefold() == "shortcutnames"
            ),
            "shortcutnames",
        )
        screenshots[existing_key] = mutable
        shortcutnames = mutable

    game_id_key = str(game_id)
    if (
        game_id_key in shortcutnames
        and shortcutnames[game_id_key] == name
    ):
        return False
    shortcutnames[game_id_key] = name
    _create_backup(path)
    _atomic_write(
        path,
        serialize_screenshots_vdf(doc.root, doc.eol).encode("utf-8"),
    )
    return True


def is_manual_game_name(
    game_id: int | str,
    config_path: Path | str | None = None,
) -> bool:
    try:
        gid = int(game_id)
    except (TypeError, ValueError):
        return False
    path = (
        Path(config_path)
        if config_path is not None
        else default_config_path()
    )
    try:
        data = _load_config(path)
    except ExeMapError:
        return False

    raw_overrides = data.get("name_overrides", {})
    if isinstance(raw_overrides, Mapping):
        for raw_key in raw_overrides:
            try:
                if int(str(raw_key)) == gid:
                    return True
            except (TypeError, ValueError):
                continue

    custom_games = data.get("custom_games", [])
    if isinstance(custom_games, list):
        for entry in custom_games:
            if not isinstance(entry, Mapping):
                continue
            try:
                if int(str(entry.get("game_id"))) == gid:
                    return True
            except (TypeError, ValueError):
                continue

    try:
        entries = load_exe_map(path)
    except ExeMapError:
        return False
    return any(
        mapping.game_id == gid and mapping.source == "manual"
        for mapping in entries.values()
    )


def rename_game(
    game_id: int | str,
    new_name: str,
    steam_userdata_dir: Path | str,
    config_path: Path | str | None = None,
    pending_path: Path | str | None = None,
    steam_running_fn: Callable[[], bool] | None = None,
) -> RenameResult:
    try:
        gid = int(game_id)
    except (TypeError, ValueError) as exc:
        raise ExeMapError(f"game_id inválido: {game_id!r}") from exc
    if gid < 1 or gid > MAX_GAME_ID:
        raise ExeMapError(
            "game_id deve estar entre 1 e 18446744073709551615"
        )
    if gid <= MAX_STEAM_APPID:
        raise ExeMapError(
            f"Jogo {gid} é da Steam e não pode ser renomeado pelo aplicativo."
        )

    normalized = normalize_game_name(new_name)
    userdata_dir = Path(steam_userdata_dir)
    config = (
        Path(config_path)
        if config_path is not None
        else default_config_path()
    )
    pending = (
        Path(pending_path)
        if pending_path is not None
        else default_pending_path()
    )

    exe_map_updated, custom_updated, override_saved = (
        _update_config_for_rename(config, gid, normalized)
    )

    running_check = steam_running_fn or is_steam_running
    if running_check():
        enqueue_rename(gid, normalized, userdata_dir, pending)
        update_pending_game_name(gid, normalized, pending)
        return RenameResult(
            game_id=gid,
            name=normalized,
            status="pendente",
            exe_map_updated=exe_map_updated,
            custom_updated=custom_updated,
            name_override_saved=override_saved,
        )

    shortcut_updated = _update_shortcut_name(
        userdata_dir,
        gid,
        normalized,
    )
    shortcutname_updated = _update_shortcutname(
        userdata_dir,
        gid,
        normalized,
    )
    update_pending_game_name(gid, normalized, pending)
    return RenameResult(
        game_id=gid,
        name=normalized,
        status="aplicado",
        exe_map_updated=exe_map_updated,
        custom_updated=custom_updated,
        name_override_saved=override_saved,
        shortcut_updated=shortcut_updated,
        shortcutname_updated=shortcutname_updated,
    )


def rename_exe(
    exe: str,
    new_name: str,
    steam_userdata_dir: Path | str,
    config_path: Path | str | None = None,
    pending_path: Path | str | None = None,
    steam_running_fn: Callable[[], bool] | None = None,
) -> tuple[ExeMapping, str]:
    normalized = normalize_game_name(new_name)
    config = (
        Path(config_path)
        if config_path is not None
        else default_config_path()
    )
    key = exe.lower()

    entries = load_exe_map(config)
    stored = entries.get(key)
    if stored is not None:
        if stored.game_id <= MAX_STEAM_APPID:
            raise ExeMapError(
                f"Jogo {stored.game_id} é da Steam e não pode ser renomeado "
                "pelo aplicativo."
            )
        mapping = ExeMapping(
            key=key,
            exe=exe,
            name=normalized,
            game_id=stored.game_id,
            provisional=False,
            is_new=False,
            source="manual",
            ignored=stored.ignored,
        )
    else:
        game_id = generate_game_id(key)
        mapping = ExeMapping(
            key=key,
            exe=exe,
            name=normalized,
            game_id=game_id,
            provisional=False,
            is_new=True,
            source="manual",
        )
        entries[key] = mapping
        save_exe_map(config, entries)

    result = rename_game(
        mapping.game_id,
        normalized,
        steam_userdata_dir,
        config,
        pending_path,
        steam_running_fn,
    )
    return mapping, result.status


def list_exe_map(
    config_path: Path | str | None = None,
) -> list[ExeMapping]:
    entries = load_exe_map(config_path)
    return sorted(entries.values(), key=lambda item: item.key)
