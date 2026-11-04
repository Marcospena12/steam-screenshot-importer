from __future__ import annotations

import ctypes
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Callable
import unicodedata

import psutil

GENERIC_FOLDER_NAMES = {
    "bin",
    "binaries",
    "bin32",
    "bin64",
    "win64",
    "win32",
    "x64",
    "x86",
    "win_x64",
    "win_x86",
    "shipping",
    "game",
    "games",
    "release",
    "retail",
    "debug",
    "steamapps",
    "common",
    "program files",
    "program files (x86)",
    "app",
    "apps",
    "client",
    "build",
    "builds",
    "dist",
    "system",
    "system32",
    "syswow64",
    "windows",
    "userdata",
    "remote",
    "screenshots",
    "thumbnails",
    "scripts",
}

GENERIC_NAMES = {
    "unreal engine",
    "unrealengine",
    "unreal",
    "unity",
    "unity player",
    "unityplayer",
    "unity crash handler",
    "epic games launcher",
    "epic games",
    "epic launcher",
    "steam",
    "steam client",
    "steam client bootstrapper",
    "origin",
    "ea desktop",
    "ubisoft connect",
    "uplay",
    "gog galaxy",
    "battle.net",
    "riot client",
    "godot",
    "godot engine",
    "cryengine",
    "cry engine",
    "source engine",
    "frostbite",
    "launcher",
    "crash reporter",
    "crash handler",
}


@dataclass(frozen=True)
class NameCandidate:
    source: str
    value: str | None
    valid: bool
    rejection_reason: str | None = None


@dataclass(frozen=True)
class DiagnosticResult:
    chosen_name: str
    chosen_source: str
    exe_name: str
    process_found: bool
    pid: int | None
    exe_path: Path | None
    candidates: list[NameCandidate]


def clean_and_validate_name(raw: str | None) -> tuple[str | None, str | None]:
    if not raw or not isinstance(raw, str):
        return None, "vazio"
    cleaned = "".join(ch for ch in raw if unicodedata.category(ch) != "Cc")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return None, "vazio após normalização"
    if len(cleaned) > 80:
        return None, f"mais de 80 caracteres ({len(cleaned)})"
    if cleaned.isdigit():
        return None, "apenas números"

    low = cleaned.casefold()
    if low in GENERIC_NAMES:
        return None, f"nome genérico: '{cleaned}'"
    if "crash reporter" in low or "crash handler" in low:
        return None, f"nome genérico: '{cleaned}'"
    for gen in (
        "unreal engine",
        "unrealengine",
        "unity player",
        "epic games launcher",
        "shipping client",
        "game client",
    ):
        if low == gen or low.startswith(gen):
            return None, f"nome genérico: '{cleaned}'"

    return cleaned, None


_version_dll = None


def _get_version_dll():
    global _version_dll
    if _version_dll is None and hasattr(ctypes, "windll"):
        try:
            ver = ctypes.windll.version
            ver.GetFileVersionInfoSizeW.argtypes = [
                ctypes.c_wchar_p,
                ctypes.POINTER(ctypes.c_ulong),
            ]
            ver.GetFileVersionInfoSizeW.restype = ctypes.c_ulong
            ver.GetFileVersionInfoW.argtypes = [
                ctypes.c_wchar_p,
                ctypes.c_ulong,
                ctypes.c_ulong,
                ctypes.c_void_p,
            ]
            ver.GetFileVersionInfoW.restype = ctypes.c_bool
            ver.VerQueryValueW.argtypes = [
                ctypes.c_void_p,
                ctypes.c_wchar_p,
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(ctypes.c_uint),
            ]
            ver.VerQueryValueW.restype = ctypes.c_bool
            _version_dll = ver
        except OSError:
            _version_dll = None
    return _version_dll


def get_version_info_strings(exe_path: Path | str) -> tuple[str | None, str | None]:
    path_str = str(exe_path)
    ver = _get_version_dll()
    if ver is None:
        return None, None

    dummy = ctypes.c_ulong()
    size = ver.GetFileVersionInfoSizeW(path_str, ctypes.byref(dummy))
    if size == 0:
        return None, None

    buf = ctypes.create_string_buffer(size)
    if not ver.GetFileVersionInfoW(path_str, 0, size, buf):
        return None, None

    trans_ptr = ctypes.c_void_p()
    trans_len = ctypes.c_uint()
    translations: list[tuple[int, int]] = []
    if ver.VerQueryValueW(
        buf,
        r"\VarFileInfo\Translation",
        ctypes.byref(trans_ptr),
        ctypes.byref(trans_len),
    ):
        count = trans_len.value // 4
        if count > 0:
            words = (ctypes.c_ushort * (count * 2)).from_address(trans_ptr.value)
            for i in range(count):
                translations.append((words[i * 2], words[i * 2 + 1]))

    if not translations:
        translations = [(0x0409, 0x04B0), (0, 0x04B0)]

    product_name: str | None = None
    file_description: str | None = None

    for lang, cp in translations:
        if product_name is None:
            str_ptr = ctypes.c_void_p()
            str_len = ctypes.c_uint()
            sub_block = f"\\StringFileInfo\\{lang:04x}{cp:04x}\\ProductName"
            if ver.VerQueryValueW(
                buf,
                sub_block,
                ctypes.byref(str_ptr),
                ctypes.byref(str_len),
            ):
                val = ctypes.wstring_at(str_ptr.value).strip()
                if val:
                    product_name = val

        if file_description is None:
            str_ptr = ctypes.c_void_p()
            str_len = ctypes.c_uint()
            sub_block = f"\\StringFileInfo\\{lang:04x}{cp:04x}\\FileDescription"
            if ver.VerQueryValueW(
                buf,
                sub_block,
                ctypes.byref(str_ptr),
                ctypes.byref(str_len),
            ):
                val = ctypes.wstring_at(str_ptr.value).strip()
                if val:
                    file_description = val

    return product_name, file_description


def find_installation_folder_name(exe_path: Path | str) -> str | None:
    path = Path(exe_path)
    parent = path.parent
    while parent != parent.parent and parent.name:
        folder = parent.name
        if folder.casefold() not in GENERIC_FOLDER_NAMES:
            cleaned, _ = clean_and_validate_name(folder)
            if cleaned is not None:
                return cleaned
        parent = parent.parent
    return None


def get_process_window_titles(pid: int) -> list[str]:
    titles: list[str] = []
    if not hasattr(ctypes, "windll"):
        return titles

    user32 = ctypes.windll.user32
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def enum_proc(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        window_pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if window_pid.value == pid:
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value.strip()
                if title:
                    titles.append(title)
        return True

    try:
        user32.EnumWindows(WNDENUMPROC(enum_proc), 0)
    except Exception:
        pass
    return titles


def find_running_process(exe_name: str) -> psutil.Process | None:
    target = exe_name.removesuffix(".exe").removesuffix(".EXE").casefold()
    matching: list[psutil.Process] = []

    for proc in psutil.process_iter(["pid", "name"]):
        try:
            pname = proc.info.get("name")
            if not pname:
                continue
            if pname.removesuffix(".exe").removesuffix(".EXE").casefold() == target:
                matching.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if not matching:
        return None
    if len(matching) == 1:
        return matching[0]

    for proc in matching:
        try:
            titles = get_process_window_titles(proc.pid)
            if titles:
                return proc
        except Exception:
            continue
    return matching[0]


def resolve_game_name_diagnostic(
    exe_name: str,
    *,
    process: psutil.Process | None = None,
    process_finder: Callable[[str], psutil.Process | None] | None = None,
    version_info_reader: Callable[[Path], tuple[str | None, str | None]] | None = None,
    window_title_reader: Callable[[int], list[str]] | None = None,
) -> DiagnosticResult:
    candidates: list[NameCandidate] = []
    finder = process_finder or find_running_process
    proc = process or finder(exe_name)

    pid: int | None = None
    exe_path: Path | None = None

    if proc is not None:
        pid = proc.pid
        try:
            exe_raw = proc.exe()
            if exe_raw:
                exe_path = Path(exe_raw)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            exe_path = None

    chosen_name: str | None = None
    chosen_source: str | None = None

    # a. versioninfo
    ver_reader = version_info_reader or get_version_info_strings
    if exe_path is not None:
        try:
            pname, fdesc = ver_reader(exe_path)
        except Exception:
            pname, fdesc = None, None

        if pname is not None:
            cleaned, reason = clean_and_validate_name(pname)
            if cleaned is not None:
                candidates.append(
                    NameCandidate(
                        source="versioninfo",
                        value=pname,
                        valid=True,
                    )
                )
                if chosen_name is None:
                    chosen_name = cleaned
                    chosen_source = "versioninfo"
            else:
                candidates.append(
                    NameCandidate(
                        source="versioninfo",
                        value=pname,
                        valid=False,
                        rejection_reason=f"ProductName rejeitado: {reason}",
                    )
                )
        else:
            candidates.append(
                NameCandidate(
                    source="versioninfo",
                    value=None,
                    valid=False,
                    rejection_reason="ProductName ausente",
                )
            )

        if fdesc is not None:
            cleaned, reason = clean_and_validate_name(fdesc)
            if cleaned is not None:
                candidates.append(
                    NameCandidate(
                        source="versioninfo",
                        value=fdesc,
                        valid=True,
                    )
                )
                if chosen_name is None:
                    chosen_name = cleaned
                    chosen_source = "versioninfo"
            else:
                candidates.append(
                    NameCandidate(
                        source="versioninfo",
                        value=fdesc,
                        valid=False,
                        rejection_reason=f"FileDescription rejeitado: {reason}",
                    )
                )
        else:
            candidates.append(
                NameCandidate(
                    source="versioninfo",
                    value=None,
                    valid=False,
                    rejection_reason="FileDescription ausente",
                )
            )
    else:
        candidates.append(
            NameCandidate(
                source="versioninfo",
                value=None,
                valid=False,
                rejection_reason="processo/caminho do executável não disponível",
            )
        )

    # b. pasta
    if exe_path is not None:
        folder_candidate = find_installation_folder_name(exe_path)
        if folder_candidate is not None:
            cleaned, reason = clean_and_validate_name(folder_candidate)
            if cleaned is not None:
                candidates.append(
                    NameCandidate(
                        source="pasta",
                        value=folder_candidate,
                        valid=True,
                    )
                )
                if chosen_name is None:
                    chosen_name = cleaned
                    chosen_source = "pasta"
            else:
                candidates.append(
                    NameCandidate(
                        source="pasta",
                        value=folder_candidate,
                        valid=False,
                        rejection_reason=f"pasta rejeitada: {reason}",
                    )
                )
        else:
            candidates.append(
                NameCandidate(
                    source="pasta",
                    value=None,
                    valid=False,
                    rejection_reason="nenhuma pasta não-genérica encontrada",
                )
            )
    else:
        candidates.append(
            NameCandidate(
                source="pasta",
                value=None,
                valid=False,
                rejection_reason="processo/caminho do executável não disponível",
            )
        )

    # c. janela
    win_reader = window_title_reader or get_process_window_titles
    if pid is not None:
        try:
            titles = win_reader(pid)
        except Exception:
            titles = []
        if titles:
            found_valid_title = False
            for title in titles:
                cleaned, reason = clean_and_validate_name(title)
                if cleaned is not None:
                    candidates.append(
                        NameCandidate(
                            source="janela",
                            value=title,
                            valid=True,
                        )
                    )
                    found_valid_title = True
                    if chosen_name is None:
                        chosen_name = cleaned
                        chosen_source = "janela"
                    break
                else:
                    candidates.append(
                        NameCandidate(
                            source="janela",
                            value=title,
                            valid=False,
                            rejection_reason=f"título rejeitado: {reason}",
                        )
                    )
            if not found_valid_title and not candidates:
                candidates.append(
                    NameCandidate(
                        source="janela",
                        value=None,
                        valid=False,
                        rejection_reason="nenhum título de janela válido encontrado",
                    )
                )
        else:
            candidates.append(
                NameCandidate(
                    source="janela",
                    value=None,
                    valid=False,
                    rejection_reason="nenhuma janela visível encontrada",
                )
            )
    else:
        candidates.append(
            NameCandidate(
                source="janela",
                value=None,
                valid=False,
                rejection_reason="processo não em execução",
            )
        )

    # d. exe
    exe_cleaned, _ = clean_and_validate_name(exe_name)
    final_exe_fallback = exe_cleaned if exe_cleaned is not None else exe_name.strip()
    candidates.append(
        NameCandidate(
            source="exe",
            value=final_exe_fallback,
            valid=True,
        )
    )
    if chosen_name is None:
        chosen_name = final_exe_fallback
        chosen_source = "exe"

    return DiagnosticResult(
        chosen_name=chosen_name,
        chosen_source=chosen_source,
        exe_name=exe_name,
        process_found=proc is not None,
        pid=pid,
        exe_path=exe_path,
        candidates=candidates,
    )


def resolve_game_name(
    exe_name: str,
    *,
    process: psutil.Process | None = None,
    process_finder: Callable[[str], psutil.Process | None] | None = None,
    version_info_reader: Callable[[Path], tuple[str | None, str | None]] | None = None,
    window_title_reader: Callable[[int], list[str]] | None = None,
) -> tuple[str, str]:
    diag = resolve_game_name_diagnostic(
        exe_name,
        process=process,
        process_finder=process_finder,
        version_info_reader=version_info_reader,
        window_title_reader=window_title_reader,
    )
    return diag.chosen_name, diag.chosen_source
