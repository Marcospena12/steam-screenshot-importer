from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile
from typing import Mapping

from ..games import default_config_path


@dataclass
class Settings:
    watch_folder: str = ""
    steam_userdata_id: str = ""  # vazio = autodetectar
    notifications: bool = True
    create_shortcut: bool = True
    hidden_shortcut: bool = False
    start_with_windows: bool = False
    start_minimized: bool = False
    theme: str = "auto"  # "auto" | "light" | "dark"
    fallback_game: str = ""  # opcional


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


def load_settings(config_path: Path | str | None = None) -> Settings:
    path = (
        Path(config_path)
        if config_path is not None
        else default_config_path()
    )
    defaults = Settings()
    if not path.is_file():
        return defaults

    try:
        raw = path.read_text(encoding="utf-8-sig")
        data = json.loads(raw)
    except Exception:
        return defaults

    if not isinstance(data, dict):
        return defaults

    # Se já existir chave "settings"
    raw_settings = data.get("settings")
    if isinstance(raw_settings, dict):
        return Settings(
            watch_folder=str(raw_settings.get("watch_folder", defaults.watch_folder)),
            steam_userdata_id=str(raw_settings.get("steam_userdata_id", defaults.steam_userdata_id)),
            notifications=bool(raw_settings.get("notifications", defaults.notifications)),
            create_shortcut=bool(raw_settings.get("create_shortcut", defaults.create_shortcut)),
            hidden_shortcut=bool(raw_settings.get("hidden_shortcut", defaults.hidden_shortcut)),
            start_with_windows=bool(raw_settings.get("start_with_windows", defaults.start_with_windows)),
            start_minimized=bool(raw_settings.get("start_minimized", defaults.start_minimized)),
            theme=str(raw_settings.get("theme", defaults.theme)),
            fallback_game=str(raw_settings.get("fallback_game", defaults.fallback_game)),
        )

    # Migração: chaves soltas no topo do config.json
    has_legacy = False
    migrated = Settings()

    if "watch_folder" in data:
        migrated.watch_folder = str(data.get("watch_folder", ""))
        has_legacy = True

    if "steam_userdata_dir" in data:
        raw_sd = str(data.get("steam_userdata_dir", "")).strip()
        if raw_sd:
            p = Path(raw_sd)
            if p.name.isdigit():
                migrated.steam_userdata_id = p.name
            else:
                migrated.steam_userdata_id = raw_sd
        has_legacy = True
    elif "steam_userdata_id" in data:
        migrated.steam_userdata_id = str(data.get("steam_userdata_id", ""))
        has_legacy = True

    if "notifications" in data:
        migrated.notifications = bool(data.get("notifications", True))
        has_legacy = True

    if "create_shortcut" in data:
        migrated.create_shortcut = bool(data.get("create_shortcut", True))
        has_legacy = True

    if "hidden_shortcut" in data:
        migrated.hidden_shortcut = bool(data.get("hidden_shortcut", False))
        has_legacy = True

    if "start_with_windows" in data:
        migrated.start_with_windows = bool(data.get("start_with_windows", False))
        has_legacy = True

    if "start_minimized" in data:
        migrated.start_minimized = bool(data.get("start_minimized", False))
        has_legacy = True

    if "theme" in data:
        migrated.theme = str(data.get("theme", "auto"))
        has_legacy = True

    if "fallback_game" in data:
        migrated.fallback_game = str(data.get("fallback_game", ""))
        has_legacy = True

    if has_legacy:
        # Grava sob a chave "settings" preservando as demais chaves
        save_settings(migrated, path)

    return migrated


def save_settings(
    settings: Settings,
    config_path: Path | str | None = None,
) -> None:
    path = (
        Path(config_path)
        if config_path is not None
        else default_config_path()
    )

    data: dict[str, object] = {}
    if path.is_file():
        try:
            raw = path.read_text(encoding="utf-8-sig")
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}

    data["settings"] = asdict(settings)
    _atomic_write_json(path, data)
