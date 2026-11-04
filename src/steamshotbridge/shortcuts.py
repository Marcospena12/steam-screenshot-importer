from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping

import vdf

from .steam import (
    find_mapping_key,
    get_mapping_value,
    is_steam_running,
    load_shortcuts_vdf,
    signed_32,
    unsigned_32,
    vdf_text,
)

MAX_STEAM_APPID = 0xFFFFFFFF
MAX_GAME_ID = 0xFFFFFFFFFFFFFFFF
DEFAULT_FIELD_NAMES = (
    "appid",
    "AppName",
    "Exe",
    "StartDir",
    "icon",
    "ShortcutPath",
    "LaunchOptions",
    "IsHidden",
    "AllowDesktopConfig",
    "AllowOverlay",
    "OpenVR",
    "Devkit",
    "DevkitGameID",
    "DevkitOverrideAppID",
    "LastPlayTime",
    "FlatpakAppID",
    "tags",
)
MISSING = object()


class ShortcutError(Exception):
    pass


@dataclass(frozen=True)
class PendingShortcutUpdate:
    path: Path
    shortcut_key: str
    entry: Mapping
    game_id: int
    appid32: int
    name: str


@dataclass(frozen=True)
class ShortcutResult:
    status: str
    path: Path
    game_id: int
    appid32: int
    appid_signed: int
    name: str
    shortcut_key: str | None
    entry: Mapping | None
    backup_path: Path | None = None
    pending: PendingShortcutUpdate | None = None
    steam_running: bool = False


def shortcut_path(steam_userdata_dir: Path | str) -> Path:
    return Path(steam_userdata_dir) / "config" / "shortcuts.vdf"


def serialize_shortcuts_vdf(root: Mapping) -> bytes:
    return vdf.binary_dumps(root)


def _validate_game_id(game_id: object) -> int:
    try:
        value = int(game_id)
    except (TypeError, ValueError) as exc:
        raise ShortcutError(f"game_id inválido: {game_id!r}") from exc
    if value <= MAX_STEAM_APPID or value > MAX_GAME_ID:
        raise ShortcutError(
            "ensure_shortcut exige um game_id de jogo não Steam"
        )
    return value


def _validate_name(game_name: object) -> str:
    if not isinstance(game_name, str):
        raise ShortcutError("O nome do jogo deve ser um texto")
    name = game_name.strip()
    if not name:
        raise ShortcutError("O nome do jogo não pode ficar vazio")
    return name


def _prepare_executable(exe: str | None) -> tuple[str, str]:
    if exe is None:
        system_root = os.environ.get(
            "SystemRoot",
            os.environ.get("WINDIR", r"C:\Windows"),
        )
        executable = str(Path(system_root) / "System32" / "cmd.exe")
        quoted = f'"{executable}"'
    else:
        raw = exe.strip()
        if not raw:
            raise ShortcutError("O caminho do executável não pode ficar vazio")
        if raw.startswith('"'):
            quoted = raw
            closing = raw.find('"', 1)
            executable = raw[1:closing] if closing != -1 else raw[1:]
        else:
            executable = raw
            quoted = f'"{raw}"'

    start_dir = os.path.dirname(executable)
    if not start_dir:
        start_dir = os.getcwd()
    if not start_dir.endswith(("\\", "/")):
        start_dir += os.sep
    return quoted, start_dir


def _template_field_names(shortcuts: Mapping) -> list[str]:
    for shortcut in shortcuts.values():
        if isinstance(shortcut, Mapping):
            return [str(key) for key in shortcut.keys()]
    return list(DEFAULT_FIELD_NAMES)


def _empty_value_like(value: object) -> object:
    if isinstance(value, Mapping):
        return vdf.VDFDict()
    if isinstance(value, str):
        return ""
    if isinstance(value, float):
        return 0.0
    if isinstance(value, bytes):
        return b""
    if isinstance(value, int):
        return 0
    return 0


def _build_entry(
    shortcuts: Mapping,
    appid32: int,
    game_name: str,
    exe: str | None,
    hidden: bool,
) -> vdf.VDFDict:
    exe_value, start_dir = _prepare_executable(exe)
    template = next(
        (
            shortcut
            for shortcut in shortcuts.values()
            if isinstance(shortcut, Mapping)
        ),
        None,
    )
    values = {
        "appid": signed_32(appid32),
        "AppName": game_name,
        "Exe": exe_value,
        "StartDir": start_dir,
        "icon": "",
        "ShortcutPath": "",
        "LaunchOptions": "",
        "IsHidden": 1 if hidden else 0,
        "AllowDesktopConfig": 1,
        "AllowOverlay": 1,
        "OpenVR": 0,
        "Devkit": 0,
        "DevkitGameID": "",
        "DevkitOverrideAppID": 0,
        "LastPlayTime": 0,
        "FlatpakAppID": "",
        "sortas": "",
        "tags": vdf.VDFDict(),
    }
    folded_values = {key.casefold(): value for key, value in values.items()}

    entry = vdf.VDFDict()
    for field_name in _template_field_names(shortcuts):
        if field_name in values:
            value = values[field_name]
        elif field_name.casefold() in folded_values:
            value = folded_values[field_name.casefold()]
        else:
            template_value = (
                get_mapping_value(template, field_name, 0)
                if template is not None
                else 0
            )
            value = _empty_value_like(template_value)
        entry[field_name] = value
    return entry


def _next_shortcut_key(shortcuts: Mapping) -> str:
    numeric_keys = [
        int(str(key))
        for key in shortcuts.keys()
        if str(key).isdigit()
    ]
    return str(max(numeric_keys) + 1 if numeric_keys else 0)


def _create_backup(path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = path.with_name(f"{path.name}.bak_{timestamp}")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(
            f"{path.name}.bak_{timestamp}_{counter}"
        )
        counter += 1
    shutil.copy2(path, candidate)
    return candidate


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def ensure_shortcut(
    steam_userdata_dir: Path | str,
    game_id: int | str,
    game_name: str,
    exe: str | None = None,
    hidden: bool = False,
    force: bool = False,
    dry_run: bool = False,
) -> ShortcutResult:
    game_id_value = _validate_game_id(game_id)
    name = _validate_name(game_name)
    appid32 = (game_id_value >> 32) & 0xFFFFFFFF
    appid_signed = signed_32(appid32)
    path = shortcut_path(steam_userdata_dir)

    if path.is_file():
        try:
            root = load_shortcuts_vdf(path)
        except Exception as exc:
            raise ShortcutError(
                f"Não foi possível ler {path}: {type(exc).__name__}: {exc}"
            ) from exc
    else:
        root = vdf.VDFDict()

    shortcuts = find_mapping_key(root, "shortcuts")
    if shortcuts is None:
        shortcuts = vdf.VDFDict()
        root["shortcuts"] = shortcuts
    if not isinstance(shortcuts, Mapping):
        raise ShortcutError(
            f"O bloco 'shortcuts' de {path} não é um dicionário"
        )

    for shortcut_key, shortcut in shortcuts.items():
        if not isinstance(shortcut, Mapping):
            continue
        stored_appid = unsigned_32(
            get_mapping_value(shortcut, "appid", MISSING)
        )
        if stored_appid != appid32:
            continue
        existing_name = vdf_text(
            get_mapping_value(shortcut, "AppName", name)
        ).strip()
        return ShortcutResult(
            status="already_exists",
            path=path,
            game_id=game_id_value,
            appid32=appid32,
            appid_signed=appid_signed,
            name=existing_name or name,
            shortcut_key=str(shortcut_key),
            entry=shortcut,
        )

    entry = _build_entry(
        shortcuts,
        appid32,
        name,
        exe,
        hidden,
    )
    shortcut_key = _next_shortcut_key(shortcuts)
    shortcuts[shortcut_key] = entry

    if dry_run:
        return ShortcutResult(
            status="dry_run",
            path=path,
            game_id=game_id_value,
            appid32=appid32,
            appid_signed=appid_signed,
            name=name,
            shortcut_key=shortcut_key,
            entry=entry,
        )

    steam_running = is_steam_running()
    if steam_running and not force:
        return ShortcutResult(
            status="pendente",
            path=path,
            game_id=game_id_value,
            appid32=appid32,
            appid_signed=appid_signed,
            name=name,
            shortcut_key=shortcut_key,
            entry=entry,
            pending=PendingShortcutUpdate(
                path=path,
                shortcut_key=shortcut_key,
                entry=entry,
                game_id=game_id_value,
                appid32=appid32,
                name=name,
            ),
            steam_running=True,
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = _create_backup(path) if path.is_file() else None
    _atomic_write(path, serialize_shortcuts_vdf(root))
    return ShortcutResult(
        status="created",
        path=path,
        game_id=game_id_value,
        appid32=appid32,
        appid_signed=appid_signed,
        name=name,
        shortcut_key=shortcut_key,
        entry=entry,
        backup_path=backup_path,
        steam_running=steam_running,
    )
