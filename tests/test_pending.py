from __future__ import annotations

from pathlib import Path

import pytest
import vdf
from PIL import Image

from steamshotbridge import importer, pending as pending_module, shortcuts
from steamshotbridge.games import custom_game_id
from steamshotbridge.importer import import_screenshot
from steamshotbridge.pending import (
    PendingError,
    apply_pending,
    enqueue_import,
    load_pending,
)
from steamshotbridge.steam import load_shortcuts_vdf

USERDATA_ID = "12345"
GAME_ID = str(custom_game_id("Jogo Pendente"))
BASE_VDF = '''"screenshots"
{
    "shortcutnames"
    {
    }
}
'''


@pytest.fixture(autouse=True)
def _steam_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importer, "is_steam_running", lambda: False)
    monkeypatch.setattr(shortcuts, "is_steam_running", lambda: False)


def _setup_userdata(tmp_path: Path) -> tuple[Path, Path]:
    userdata = tmp_path / "Steam" / "userdata" / USERDATA_ID
    (userdata / "760").mkdir(parents=True)
    vdf_path = userdata / "760" / "screenshots.vdf"
    vdf_path.write_text(BASE_VDF, encoding="utf-8")
    return userdata, vdf_path


def _make_source(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    Image.new("RGB", (320, 180), (10, 30, 60)).save(path, format="JPEG")
    return path


def _write_empty_shortcuts(userdata: Path) -> Path:
    path = userdata / "config" / "shortcuts.vdf"
    path.parent.mkdir(parents=True)
    path.write_bytes(vdf.binary_dumps({"shortcuts": {}}))
    return path


def _queue_result(
    tmp_path: Path,
    userdata: Path,
    pending_path: Path,
    name: str,
    creation: int,
    milliseconds: int,
) -> None:
    source = _make_source(tmp_path, name)
    result = import_screenshot(
        source,
        GAME_ID,
        userdata,
        game_name="Jogo Pendente",
        creation_time=creation,
    )
    assert result.status == "vdf_pendente"
    enqueue_import(result, pending_path, milliseconds=milliseconds)


def test_steam_running_queues_screenshot_and_shortcut(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importer, "is_steam_running", lambda: True)
    monkeypatch.setattr(shortcuts, "is_steam_running", lambda: True)
    userdata, vdf_path = _setup_userdata(tmp_path)
    original_vdf = vdf_path.read_bytes()
    pending_path = tmp_path / "pending.json"
    _queue_result(
        tmp_path,
        userdata,
        pending_path,
        "aomx_2026_11_03_12_42_05_932.jpg",
        932,
        932,
    )

    entries = load_pending(pending_path)

    assert len(entries) == 1
    record = entries[0]
    assert record["game_id"] == GAME_ID
    assert record["game_name"] == "Jogo Pendente"
    assert record["milliseconds"] == 932
    assert record["entry"]["filename"].endswith(".jpg")
    assert record["shortcut"]["name"] == "Jogo Pendente"
    assert record["shortcut"]["game_id"] == GAME_ID
    assert vdf_path.read_bytes() == original_vdf
    assert not (userdata / "config" / "shortcuts.vdf").exists()


def test_apply_pending_writes_each_file_once_with_one_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importer, "is_steam_running", lambda: True)
    monkeypatch.setattr(shortcuts, "is_steam_running", lambda: True)
    userdata, vdf_path = _setup_userdata(tmp_path)
    shortcut_file = _write_empty_shortcuts(userdata)
    pending_path = tmp_path / "pending.json"
    _queue_result(
        tmp_path,
        userdata,
        pending_path,
        "aomx_2026_11_03_12_42_05_932.jpg",
        100,
        932,
    )
    _queue_result(
        tmp_path,
        userdata,
        pending_path,
        "aomx_2026_11_03_12_42_06_100.jpg",
        101,
        100,
    )
    assert len(load_pending(pending_path)) == 2

    writes: list[Path] = []
    backups: list[Path] = []
    original_write = pending_module._atomic_write
    original_backup = pending_module._create_backup

    def counting_write(path: Path, content: bytes) -> None:
        writes.append(path)
        original_write(path, content)

    def counting_backup(path: Path) -> Path:
        result = original_backup(path)
        backups.append(result)
        return result

    monkeypatch.setattr(pending_module, "_atomic_write", counting_write)
    monkeypatch.setattr(pending_module, "_create_backup", counting_backup)

    result = apply_pending(
        userdata,
        pending_path,
        steam_running=lambda: False,
    )

    assert result.status == "aplicado"
    assert result.applied == 2
    assert result.screenshots == 2
    assert len(writes) == 2
    assert writes == [shortcut_file, vdf_path]
    assert [path.name.split(".bak_")[0] for path in backups] == [
        shortcut_file.name,
        vdf_path.name,
    ]
    assert len(list(shortcut_file.parent.glob("shortcuts.vdf.bak_*"))) == 1
    assert len(list(vdf_path.parent.glob("screenshots.vdf.bak_*"))) == 1
    assert load_pending(pending_path) == []

    shortcut_root = load_shortcuts_vdf(shortcut_file)
    assert len(shortcut_root["shortcuts"]) == 1
    assert shortcut_root["shortcuts"]["0"]["AppName"] == "Jogo Pendente"

    root = vdf.loads(vdf_path.read_text(encoding="utf-8"))
    entries = root["screenshots"][GAME_ID]
    assert len(entries) == 2
    assert entries["0"]["creation"] == "101"
    assert entries["1"]["creation"] == "100"
    assert entries["0"]["timelineid"] == entries["1"]["timelineid"]


def test_apply_pending_keeps_queue_when_steam_is_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importer, "is_steam_running", lambda: True)
    monkeypatch.setattr(shortcuts, "is_steam_running", lambda: True)
    userdata, _ = _setup_userdata(tmp_path)
    pending_path = tmp_path / "pending.json"
    _queue_result(
        tmp_path,
        userdata,
        pending_path,
        "aomx_2026_11_03_12_42_05_932.jpg",
        100,
        0,
    )
    before = pending_path.read_bytes()

    result = apply_pending(
        userdata,
        pending_path,
        steam_running=lambda: True,
    )

    assert result.status == "steam_aberta"
    assert pending_path.read_bytes() == before


def test_failure_in_the_middle_keeps_queue_and_all_files_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importer, "is_steam_running", lambda: True)
    monkeypatch.setattr(shortcuts, "is_steam_running", lambda: True)
    userdata, vdf_path = _setup_userdata(tmp_path)
    shortcut_file = _write_empty_shortcuts(userdata)
    pending_path = tmp_path / "pending.json"
    _queue_result(
        tmp_path,
        userdata,
        pending_path,
        "aomx_2026_11_03_12_42_05_932.jpg",
        100,
        0,
    )
    _queue_result(
        tmp_path,
        userdata,
        pending_path,
        "aomx_2026_11_03_12_42_06_100.jpg",
        101,
        0,
    )
    pending_before = pending_path.read_bytes()
    vdf_before = vdf_path.read_bytes()
    shortcut_before = shortcut_file.read_bytes()

    original_build = pending_module._build_timeline
    calls = {"count": 0}

    def failing_build(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("falha simulada")
        return original_build(*args, **kwargs)

    monkeypatch.setattr(pending_module, "_build_timeline", failing_build)

    with pytest.raises(PendingError, match="falha simulada"):
        apply_pending(
            userdata,
            pending_path,
            steam_running=lambda: False,
        )

    assert pending_path.read_bytes() == pending_before
    assert vdf_path.read_bytes() == vdf_before
    assert shortcut_file.read_bytes() == shortcut_before
    assert not list(vdf_path.parent.glob("screenshots.vdf.bak_*"))
    assert not list(shortcut_file.parent.glob("shortcuts.vdf.bak_*"))


def test_empty_queue_is_a_no_op(tmp_path: Path) -> None:
    userdata, _ = _setup_userdata(tmp_path)
    pending_path = tmp_path / "pending.json"

    result = apply_pending(
        userdata,
        pending_path,
        steam_running=lambda: False,
    )

    assert result.status == "vazio"
    assert not pending_path.exists()


def test_entries_for_another_userdata_are_kept(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importer, "is_steam_running", lambda: True)
    monkeypatch.setattr(shortcuts, "is_steam_running", lambda: True)
    userdata, _ = _setup_userdata(tmp_path)
    other_userdata = tmp_path / "Steam" / "userdata" / "99999"
    (other_userdata / "760").mkdir(parents=True)
    (other_userdata / "760" / "screenshots.vdf").write_text(
        BASE_VDF,
        encoding="utf-8",
    )
    pending_path = tmp_path / "pending.json"
    _queue_result(
        tmp_path,
        other_userdata,
        pending_path,
        "aomx_2026_11_03_12_42_05_932.jpg",
        100,
        0,
    )
    before = pending_path.read_bytes()

    result = apply_pending(
        userdata,
        pending_path,
        steam_running=lambda: False,
    )

    assert result.status == "vazio"
    assert pending_path.read_bytes() == before
