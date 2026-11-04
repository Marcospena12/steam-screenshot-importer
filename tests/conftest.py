from __future__ import annotations

import hashlib
import os
from pathlib import Path

# Garante display virtual headless para Qt em CI / background
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

_ORIGINAL_APPDATA = os.environ.get("APPDATA")
_REAL_BASE = (
    Path(_ORIGINAL_APPDATA) / "SteamShotBridge"
    if _ORIGINAL_APPDATA
    else None
)
_REAL_STEAM_USERDATA = Path(
    r"C:\Program Files (x86)\Steam\userdata\259740280"
)

REAL_GUARD_PATHS = [
    p
    for p in (
        _REAL_BASE / "config.json" if _REAL_BASE else None,
        _REAL_BASE / "pending.json" if _REAL_BASE else None,
        _REAL_BASE / "processed.json" if _REAL_BASE else None,
        _REAL_STEAM_USERDATA / "config" / "shortcuts.vdf",
        _REAL_STEAM_USERDATA / "760" / "screenshots.vdf",
    )
    if p is not None
]


def _file_state(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@pytest.fixture(autouse=True)
def _isolate_appdata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_appdata = tmp_path / "fake_appdata" / "Roaming"
    fake_localappdata = tmp_path / "fake_appdata" / "Local"
    fake_profile = tmp_path / "fake_profile"

    fake_appdata.mkdir(parents=True, exist_ok=True)
    fake_localappdata.mkdir(parents=True, exist_ok=True)
    fake_profile.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("APPDATA", str(fake_appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(fake_localappdata))
    monkeypatch.setenv("USERPROFILE", str(fake_profile))
    monkeypatch.setenv("HOME", str(fake_profile))


@pytest.fixture(scope="session", autouse=True)
def _real_files_unchanged() -> None:
    # Monitora apenas os arquivos que existiam ou caminhos que de fato pertencem à máquina local
    active_guards = [p for p in REAL_GUARD_PATHS if p.exists() or (p.parent.exists() and "259740280" in str(p))]
    if not active_guards:
        # Ambiente de CI limpo sem Steam instalada
        yield
        return

    before = {str(path): _file_state(path) for path in active_guards}
    yield
    after = {str(path): _file_state(path) for path in active_guards}
    assert before == after, (
        "Um arquivo real da Steam/SteamShotBridge foi alterado ou criado durante os testes.\n"
        f"Antes:\n{before}\nDepois:\n{after}"
    )
