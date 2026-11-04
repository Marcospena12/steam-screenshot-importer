from __future__ import annotations

import os
from pathlib import Path
import sys

REG_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
REG_VALUE_NAME = "SteamShotBridge"


class WindowsRegistryBackend:
    def get_value(self, subkey: str, name: str) -> str | None:
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_READ) as key:
                val, _ = winreg.QueryValueEx(key, name)
                return str(val)
        except (OSError, ImportError):
            return None

    def set_value(self, subkey: str, name: str, value: str) -> None:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, subkey) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    def delete_value(self, subkey: str, name: str) -> None:
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
        except (OSError, ImportError):
            pass


class MockRegistryBackend:
    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self.values: dict[str, str] = dict(initial or {})

    def get_value(self, subkey: str, name: str) -> str | None:
        return self.values.get(f"{subkey}\\{name}")

    def set_value(self, subkey: str, name: str, value: str) -> None:
        self.values[f"{subkey}\\{name}"] = value

    def delete_value(self, subkey: str, name: str) -> None:
        self.values.pop(f"{subkey}\\{name}", None)


_default_backend = WindowsRegistryBackend()


def get_autostart_command(executable: str | None = None) -> str:
    exe = executable or sys.executable
    exe_path = Path(exe)

    if getattr(sys, "frozen", False):
        return f'"{exe}" --minimized'

    if exe_path.name.lower() == "python.exe":
        pythonw = exe_path.with_name("pythonw.exe")
        if pythonw.is_file():
            exe = str(pythonw)

    return f'"{exe}" -m steamshotbridge.app --minimized'


def is_enabled(backend: WindowsRegistryBackend | MockRegistryBackend | None = None) -> bool:
    be = backend or _default_backend
    val = be.get_value(REG_SUBKEY, REG_VALUE_NAME)
    return val is not None and len(val.strip()) > 0


def enable(
    backend: WindowsRegistryBackend | MockRegistryBackend | None = None,
    command: str | None = None,
) -> None:
    be = backend or _default_backend
    cmd = command or get_autostart_command()
    be.set_value(REG_SUBKEY, REG_VALUE_NAME, cmd)


def disable(backend: WindowsRegistryBackend | MockRegistryBackend | None = None) -> None:
    be = backend or _default_backend
    be.delete_value(REG_SUBKEY, REG_VALUE_NAME)
