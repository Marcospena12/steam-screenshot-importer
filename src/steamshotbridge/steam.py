from __future__ import annotations

import binascii
import csv
import io
import os
import subprocess
import winreg
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

import vdf

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
SHORTCUT_GAMEID_SUFFIX = 0x02000000


class SteamError(Exception):
    pass


class SteamNotFoundError(SteamError):
    pass


class UserDataError(SteamError):
    pass


@dataclass(frozen=True)
class SteamInstallation:
    path: Path
    source: str


@dataclass(frozen=True)
class ShortcutIds:
    crc_input: str
    appid32: int
    gameid64: int


def _registry_value(root: int, subkey: str, value_name: str) -> str | None:
    try:
        with winreg.OpenKey(root, subkey) as key:
            value, _ = winreg.QueryValueEx(key, value_name)
    except OSError:
        return None
    return str(value) if value else None


def _default_steam_candidates() -> list[tuple[str, Path]]:
    candidates: list[tuple[str, Path]] = []

    registry_sources = (
        (
            winreg.HKEY_CURRENT_USER,
            r"Software\Valve\Steam",
            "SteamPath",
            "HKCU\\Software\\Valve\\Steam (SteamPath)",
        ),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\WOW6432Node\Valve\Steam",
            "InstallPath",
            "HKLM\\SOFTWARE\\WOW6432Node\\Valve\\Steam (InstallPath)",
        ),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Valve\Steam",
            "InstallPath",
            "HKLM\\SOFTWARE\\Valve\\Steam (InstallPath)",
        ),
    )

    for root, subkey, value_name, label in registry_sources:
        value = _registry_value(root, subkey, value_name)
        if value:
            candidates.append((label, Path(os.path.expandvars(value))))

    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    candidates.append(("Fallback Program Files (x86)", Path(program_files_x86) / "Steam"))
    candidates.append(("Fallback Program Files", Path(program_files) / "Steam"))

    return candidates


def find_steam_installation(
    candidates: Iterable[tuple[str, Path]] | None = None,
) -> SteamInstallation:
    tried: list[str] = []
    for source, candidate in candidates or _default_steam_candidates():
        path = Path(os.path.expandvars(str(candidate)))
        tried.append(f"{source}: {path}")
        if path.is_dir():
            return SteamInstallation(path=path, source=source)

    details = "\n".join(f"  - {item}" for item in tried)
    raise SteamNotFoundError(f"Não foi possível localizar a pasta da Steam.\n{details}")


def list_userdata_ids(steam_path: Path) -> list[str]:
    userdata = steam_path / "userdata"
    if not userdata.is_dir():
        return []
    ids = [
        entry.name
        for entry in userdata.iterdir()
        if entry.is_dir() and entry.name.isdigit()
    ]
    return sorted(ids, key=int)


def resolve_userdata_id(steam_path: Path, requested: str | None = None) -> tuple[str, str]:
    ids = list_userdata_ids(steam_path)
    if requested is not None:
        if not requested.isdigit():
            raise UserDataError(f"Userdata inválido: {requested!r}. Use um ID numérico.")
        if requested not in ids:
            available = ", ".join(ids) if ids else "(nenhum)"
            raise UserDataError(
                f"Userdata {requested} não encontrado. IDs disponíveis: {available}"
            )
        return requested, "argumento da linha de comando"

    if not ids:
        raise UserDataError(
            f"Nenhum userdata numérico encontrado em {steam_path / 'userdata'}"
        )
    return ids[0], "primeiro userdata encontrado"


def userdata_path(steam_path: Path, user_id: str) -> Path:
    return steam_path / "userdata" / user_id


def _numeric_directory_sort_key(entry: Path) -> tuple[int, int | str]:
    return (0, int(entry.name)) if entry.name.isdigit() else (1, entry.name.casefold())


def list_remote_app_dirs(steam_path: Path, user_id: str) -> list[Path]:
    remote = userdata_path(steam_path, user_id) / "760" / "remote"
    if not remote.is_dir():
        return []
    return sorted(
        (entry for entry in remote.iterdir() if entry.is_dir()),
        key=_numeric_directory_sort_key,
    )


def screenshot_files(app_dir: Path) -> list[Path]:
    screenshots = app_dir / "screenshots"
    if not screenshots.is_dir():
        return []
    return sorted(
        (
            entry
            for entry in screenshots.iterdir()
            if entry.is_file() and entry.suffix.lower() in IMAGE_SUFFIXES
        ),
        key=lambda entry: entry.name.casefold(),
    )


def thumbnail_path(screenshot: Path) -> Path:
    return screenshot.parent / "thumbnails" / screenshot.name


def screenshots_vdf_path(steam_path: Path, user_id: str) -> Path:
    return userdata_path(steam_path, user_id) / "760" / "screenshots.vdf"


def shortcuts_vdf_path(steam_path: Path, user_id: str) -> Path:
    return userdata_path(steam_path, user_id) / "config" / "shortcuts.vdf"


def load_screenshots_vdf(path: Path) -> vdf.VDFDict:
    raw = path.read_bytes()
    decoded: str | None = None
    last_error: UnicodeDecodeError | None = None
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            decoded = raw.decode(encoding)
            break
        except UnicodeDecodeError as exc:
            last_error = exc
    if decoded is None:
        raise last_error or UnicodeDecodeError("utf-8", b"", 0, 1, "falha ao decodificar")

    result = vdf.loads(decoded, mapper=vdf.VDFDict, merge_duplicate_keys=False)
    if not isinstance(result, vdf.VDFDict):
        raise ValueError("O parser VDF não retornou um VDFDict")
    return result


def load_shortcuts_vdf(path: Path) -> vdf.VDFDict:
    with path.open("rb") as handle:
        result = vdf.binary_load(
            handle,
            mapper=vdf.VDFDict,
            merge_duplicate_keys=False,
        )
    if not isinstance(result, vdf.VDFDict):
        raise ValueError("O parser VDF binário não retornou um VDFDict")
    return result


def find_mapping_key(mapping: Mapping, key: str):
    folded = key.casefold()
    for candidate, value in mapping.items():
        if str(candidate).casefold() == folded:
            return value
    return None


def get_mapping_value(mapping: Mapping, key: str, default=None):
    value = find_mapping_key(mapping, key)
    return default if value is None else value


def vdf_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="surrogateescape")
    if value is None:
        return ""
    return str(value)


def calculate_shortcut_ids(exe: object, appname: object) -> ShortcutIds:
    crc_input = vdf_text(exe) + vdf_text(appname)
    crc = binascii.crc32(crc_input.encode("utf-8", errors="surrogateescape")) & 0xFFFFFFFF
    appid32 = crc | 0x80000000
    gameid64 = (appid32 << 32) | SHORTCUT_GAMEID_SUFFIX
    return ShortcutIds(crc_input=crc_input, appid32=appid32, gameid64=gameid64)


def shortcut_game_id(appid: object) -> int:
    return ((int(appid) & 0xFFFFFFFF) << 32) | SHORTCUT_GAMEID_SUFFIX


def is_steam_running() -> bool:
    try:
        completed = subprocess.run(
            [
                "tasklist",
                "/FI",
                "IMAGENAME eq steam.exe",
                "/FO",
                "CSV",
                "/NH",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return True

    for row in csv.reader(io.StringIO(completed.stdout)):
        if row and row[0].casefold() == "steam.exe":
            return True
    return False


def unsigned_32(value: object) -> int | None:
    try:
        return int(value) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return None


def signed_32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value
