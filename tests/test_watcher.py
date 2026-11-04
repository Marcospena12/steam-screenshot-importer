from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
import vdf
from PIL import Image

from steamshotbridge import importer, pending as pending_module, shortcuts
from steamshotbridge import watcher as watcher_module
from steamshotbridge.exe_map import generate_game_id, resolve_exe
from steamshotbridge.games import custom_game_id
from steamshotbridge.pending import (
    default_pending_path,
    enqueue_import,
    load_pending,
)
from steamshotbridge.watcher import (
    FolderWatcher,
    ProcessedRegistry,
    parse_screenshot_name,
    process_file,
    wait_until_stable,
)
from tools.watch import main as watch_cli_main

USERDATA_ID = "12345"
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
    monkeypatch.setattr(pending_module, "is_steam_running", lambda: False)


def _setup_userdata(tmp_path: Path) -> Path:
    userdata = tmp_path / "Steam" / "userdata" / USERDATA_ID
    (userdata / "760").mkdir(parents=True)
    (userdata / "760" / "screenshots.vdf").write_text(
        BASE_VDF,
        encoding="utf-8",
    )
    return userdata


def _empty_shortcuts(userdata: Path) -> Path:
    path = userdata / "config" / "shortcuts.vdf"
    path.parent.mkdir(parents=True)
    path.write_bytes(vdf.binary_dumps({"shortcuts": {}}))
    return path


def _make_image(folder: Path, name: str) -> Path:
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (320, 180), (20, 40, 80)).save(path, format="JPEG")
    return path


def _paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    return (
        tmp_path / "config.json",
        tmp_path / "pending.json",
        tmp_path / "processed.json",
    )


def _screenshot_count(userdata: Path, game_id: str) -> int:
    folder = userdata / "760" / "remote" / game_id / "screenshots"
    return len(list(folder.glob("*.jpg"))) if folder.is_dir() else 0


def test_parse_name_with_underscores_uppercase_and_invalid_names() -> None:
    parsed = parse_screenshot_name(
        "ProjectZomboid64_2026_11_03_12_43_08_071.jpg"
    )
    assert parsed is not None
    assert parsed.exe == "ProjectZomboid64"
    assert parsed.moment == datetime(2026, 11, 3, 12, 43, 8)
    assert parsed.milliseconds == 71

    parsed = parse_screenshot_name("My_Game_2026_11_03_12_43_08_932.PNG")
    assert parsed is not None
    assert parsed.exe == "My_Game"
    assert parsed.milliseconds == 932

    assert parse_screenshot_name("random.jpg") is None
    assert parse_screenshot_name("aomx_2026_13_03_12_43_08_932.jpg") is None
    assert parse_screenshot_name("aomx_2026_11_03_12_43_08_932.gif") is None


def test_creation_time_comes_from_filename(tmp_path: Path) -> None:
    userdata = _setup_userdata(tmp_path)
    config, pending, processed_path = _paths(tmp_path)
    source = _make_image(tmp_path, "aomx_2026_11_03_12_42_05_932.jpg")

    outcome = process_file(
        source,
        steam_userdata_dir=userdata,
        config_path=config,
        processed=ProcessedRegistry(processed_path),
        pending_path=pending,
        stabilize_interval=0,
    )

    assert outcome.status == "imported"
    assert outcome.import_result is not None
    expected = int(datetime(2026, 11, 3, 12, 42, 5).timestamp())
    assert outcome.import_result.creation == expected


def test_new_exe_is_saved_as_provisional(tmp_path: Path) -> None:
    userdata = _setup_userdata(tmp_path)
    config, pending, processed_path = _paths(tmp_path)
    source = _make_image(tmp_path, "aomx_2026_11_03_12_42_05_932.jpg")

    outcome = process_file(
        source,
        steam_userdata_dir=userdata,
        config_path=config,
        processed=ProcessedRegistry(processed_path),
        pending_path=pending,
        stabilize_interval=0,
    )

    assert outcome.status == "imported"
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["exe_map"]["aomx"] == {
        "name": "aomx",
        "game_id": str(generate_game_id("aomx")),
        "provisional": True,
        "source": "exe",
    }
    assert outcome.lines[0] == (
        "jogo identificado: aomx -> aomx (fonte: exe) [novo]"
    )


def test_existing_exe_uses_stored_game_id_and_name(tmp_path: Path) -> None:
    userdata = _setup_userdata(tmp_path)
    config, pending, processed_path = _paths(tmp_path)
    game_id = generate_game_id("projectzomboid64")
    config.write_text(
        json.dumps(
            {
                "exe_map": {
                    "projectzomboid64": {
                        "name": "Project Zomboid",
                        "game_id": str(game_id),
                        "provisional": False,
                    }
                }
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    source = _make_image(
        tmp_path,
        "ProjectZomboid64_2026_11_03_12_43_08_071.jpg",
    )

    outcome = process_file(
        source,
        steam_userdata_dir=userdata,
        config_path=config,
        processed=ProcessedRegistry(processed_path),
        pending_path=pending,
        stabilize_interval=0,
    )

    assert outcome.status == "imported"
    assert outcome.import_result is not None
    assert outcome.import_result.entry["gameid"] == str(game_id)
    assert "[existente]" in outcome.lines[0]
    assert "Project Zomboid" in outcome.lines[0]
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["exe_map"]["projectzomboid64"]["name"] == "Project Zomboid"


def test_file_still_being_written_is_not_imported(tmp_path: Path) -> None:
    userdata = _setup_userdata(tmp_path)
    config, pending, processed_path = _paths(tmp_path)
    source = _make_image(tmp_path, "aomx_2026_11_03_12_42_05_932.jpg")

    with source.open("ab"):
        assert not wait_until_stable(source, interval=0.01)
        outcome = process_file(
            source,
            steam_userdata_dir=userdata,
            config_path=config,
            processed=ProcessedRegistry(processed_path),
            pending_path=pending,
            stabilize_interval=0.01,
        )
        assert outcome.status == "unstable"

    outcome = process_file(
        source,
        steam_userdata_dir=userdata,
        config_path=config,
        processed=ProcessedRegistry(processed_path),
        pending_path=pending,
        stabilize_interval=0,
    )
    assert outcome.status == "imported"


def test_duplicate_is_ignored_after_restart(tmp_path: Path) -> None:
    userdata = _setup_userdata(tmp_path)
    config, pending, processed_path = _paths(tmp_path)
    source = _make_image(tmp_path, "aomx_2026_11_03_12_42_05_932.jpg")

    first = process_file(
        source,
        steam_userdata_dir=userdata,
        config_path=config,
        processed=ProcessedRegistry(processed_path),
        pending_path=pending,
        stabilize_interval=0,
    )
    assert first.status == "imported"
    game_id = str(generate_game_id("aomx"))
    assert _screenshot_count(userdata, game_id) == 1

    restarted = ProcessedRegistry(processed_path)
    second = process_file(
        source,
        steam_userdata_dir=userdata,
        config_path=config,
        processed=restarted,
        pending_path=pending,
        stabilize_interval=0,
    )

    assert second.status == "duplicate"
    assert _screenshot_count(userdata, game_id) == 1


def test_preexisting_files_are_ignored_unless_requested(tmp_path: Path) -> None:
    userdata = _setup_userdata(tmp_path)
    config, pending, processed_path = _paths(tmp_path)
    existing = _make_image(tmp_path, "aomx_2026_11_03_12_42_05_932.jpg")

    watcher = FolderWatcher(
        tmp_path,
        steam_userdata_dir=userdata,
        config_path=config,
        pending_path=pending,
        processed_path=processed_path,
        emit=lambda line: None,
    )
    ready = watcher.scan_existing()

    assert ready == []
    watcher.enqueue_path(existing)
    assert not watcher._candidates

    watcher_with_existing = FolderWatcher(
        tmp_path,
        steam_userdata_dir=userdata,
        config_path=config,
        pending_path=pending,
        processed_path=processed_path,
        import_existing=True,
        emit=lambda line: None,
    )
    ready = watcher_with_existing.scan_existing()

    assert ready == [existing]
    assert watcher_with_existing._candidates


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    userdata = _setup_userdata(tmp_path)
    config, pending, processed_path = _paths(tmp_path)
    source = _make_image(tmp_path, "aomx_2026_11_03_12_42_05_932.jpg")
    vdf_path = userdata / "760" / "screenshots.vdf"
    original_vdf = vdf_path.read_bytes()

    outcome = process_file(
        source,
        steam_userdata_dir=userdata,
        config_path=config,
        processed=ProcessedRegistry(processed_path),
        pending_path=pending,
        dry_run=True,
        stabilize_interval=0,
    )

    assert outcome.status == "dry_run"
    assert not config.exists()
    assert not pending.exists()
    assert not processed_path.exists()
    assert not (userdata / "760" / "remote").exists()
    assert not (userdata / "config" / "shortcuts.vdf").exists()
    assert vdf_path.read_bytes() == original_vdf


def test_steam_open_queues_screenshot_and_shortcut(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importer, "is_steam_running", lambda: True)
    monkeypatch.setattr(shortcuts, "is_steam_running", lambda: True)
    userdata = _setup_userdata(tmp_path)
    config, pending, processed_path = _paths(tmp_path)
    source = _make_image(tmp_path, "aomx_2026_11_03_12_42_05_932.jpg")

    outcome = process_file(
        source,
        steam_userdata_dir=userdata,
        config_path=config,
        processed=ProcessedRegistry(processed_path),
        pending_path=pending,
        stabilize_interval=0,
    )

    assert outcome.status == "pending"
    entries = load_pending(pending)
    assert len(entries) == 1
    assert entries[0]["shortcut"]["name"] == "aomx"
    assert entries[0]["milliseconds"] == 932
    assert any(line.startswith("pendente:") for line in outcome.lines)
    assert ProcessedRegistry(processed_path).contains_path(source)


def test_watcher_applies_pending_when_steam_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importer, "is_steam_running", lambda: True)
    monkeypatch.setattr(shortcuts, "is_steam_running", lambda: True)
    userdata = _setup_userdata(tmp_path)
    shortcut_file = _empty_shortcuts(userdata)
    config, pending, processed_path = _paths(tmp_path)
    source = _make_image(tmp_path, "aomx_2026_11_03_12_42_05_932.jpg")
    queued = process_file(
        source,
        steam_userdata_dir=userdata,
        config_path=config,
        processed=ProcessedRegistry(processed_path),
        pending_path=pending,
        stabilize_interval=0,
    )
    assert queued.status == "pending"

    state = {"running": True}
    lines: list[str] = []
    watcher = FolderWatcher(
        tmp_path,
        steam_userdata_dir=userdata,
        config_path=config,
        pending_path=pending,
        processed_path=processed_path,
        steam_running=lambda: state["running"],
        emit=lines.append,
    )

    assert watcher.check_steam_pending() is None
    state["running"] = False
    result = watcher.check_steam_pending()

    assert result is not None
    assert result.status == "aplicado"
    assert any(line.startswith("aplicado:") for line in lines)
    assert load_pending(pending) == []
    assert len(list(shortcut_file.parent.glob("shortcuts.vdf.bak_*"))) == 1
    vdf_path = userdata / "760" / "screenshots.vdf"
    assert len(list(vdf_path.parent.glob("screenshots.vdf.bak_*"))) == 1


def test_extension_outside_the_allowed_set_is_ignored(tmp_path: Path) -> None:
    userdata = _setup_userdata(tmp_path)
    config, pending, processed_path = _paths(tmp_path)
    source = tmp_path / "notes.txt"
    source.write_text("nada", encoding="utf-8")

    outcome = process_file(
        source,
        steam_userdata_dir=userdata,
        config_path=config,
        processed=ProcessedRegistry(processed_path),
        pending_path=pending,
        stabilize_interval=0,
    )

    assert outcome.status == "ignored_extension"
    assert not config.exists()
    assert not ProcessedRegistry(processed_path).contains_path(source)


def test_error_in_one_file_does_not_prevent_the_next(tmp_path: Path) -> None:
    userdata = _setup_userdata(tmp_path)
    config, pending, processed_path = _paths(tmp_path)
    processed = ProcessedRegistry(processed_path)
    corrupt = tmp_path / "aomx_2026_11_03_12_42_05_932.jpg"
    corrupt.write_bytes(b"isto nao e uma imagem")
    valid = _make_image(tmp_path, "aomx_2026_11_03_12_42_06_100.jpg")

    failed = process_file(
        corrupt,
        steam_userdata_dir=userdata,
        config_path=config,
        processed=processed,
        pending_path=pending,
        stabilize_interval=0,
    )
    succeeded = process_file(
        valid,
        steam_userdata_dir=userdata,
        config_path=config,
        processed=processed,
        pending_path=pending,
        stabilize_interval=0,
    )

    assert failed.status == "error"
    assert failed.error is not None
    assert not processed.contains_path(corrupt)
    assert succeeded.status == "imported"


def test_out_of_pattern_file_without_fallback_is_ignored(
    tmp_path: Path,
) -> None:
    userdata = _setup_userdata(tmp_path)
    config, pending, processed_path = _paths(tmp_path)
    source = _make_image(tmp_path, "captura qualquer.jpg")

    outcome = process_file(
        source,
        steam_userdata_dir=userdata,
        config_path=config,
        processed=ProcessedRegistry(processed_path),
        pending_path=pending,
        stabilize_interval=0,
    )

    assert outcome.status == "no_game"
    assert outcome.lines == (
        "sem jogo identificado: captura qualquer.jpg",
    )
    assert not (userdata / "760" / "remote").exists()


def test_out_of_pattern_file_uses_fallback_game(tmp_path: Path) -> None:
    userdata = _setup_userdata(tmp_path)
    config, pending, processed_path = _paths(tmp_path)
    source = _make_image(tmp_path, "captura qualquer.jpg")

    outcome = process_file(
        source,
        steam_userdata_dir=userdata,
        config_path=config,
        processed=ProcessedRegistry(processed_path),
        pending_path=pending,
        fallback_game_name="Jogo Fallback",
        stabilize_interval=0,
    )

    assert outcome.status == "imported"
    assert outcome.import_result is not None
    expected = str(custom_game_id("Jogo Fallback"))
    assert outcome.import_result.entry["gameid"] == expected
    assert f"[fallback]" in outcome.lines[0]


def test_collision_skips_the_file_and_keeps_config(tmp_path: Path) -> None:
    userdata = _setup_userdata(tmp_path)
    config, pending, processed_path = _paths(tmp_path)
    conflicting = generate_game_id("aomx")
    config.write_text(
        json.dumps(
            {
                "exe_map": {
                    "outro": {
                        "name": "Outro",
                        "game_id": str(conflicting),
                        "provisional": True,
                    }
                }
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    before = config.read_bytes()
    source = _make_image(tmp_path, "aomx_2026_11_03_12_42_05_932.jpg")

    outcome = process_file(
        source,
        steam_userdata_dir=userdata,
        config_path=config,
        processed=ProcessedRegistry(processed_path),
        pending_path=pending,
        stabilize_interval=0,
    )

    assert outcome.status == "error"
    assert "colide" in (outcome.error or "")
    assert config.read_bytes() == before
    assert not (userdata / "760" / "remote").exists()


def test_cli_exes_lists_the_map(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "config.json"
    resolve_exe("aomx", config)

    exit_code = watch_cli_main(["exes", "--config", str(config)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "EXE" in captured.out
    assert "aomx" in captured.out
    assert str(generate_game_id("aomx")) in captured.out
    assert "sim" in captured.out


def test_cli_pending_lists_and_applies_the_queue(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setattr(importer, "is_steam_running", lambda: True)
    monkeypatch.setattr(shortcuts, "is_steam_running", lambda: True)
    userdata = _setup_userdata(tmp_path)
    config, _, processed_path = _paths(tmp_path)
    source = _make_image(tmp_path, "aomx_2026_11_03_12_42_05_932.jpg")
    pending_path = default_pending_path()

    outcome = process_file(
        source,
        steam_userdata_dir=userdata,
        config_path=config,
        processed=ProcessedRegistry(processed_path),
        pending_path=pending_path,
        stabilize_interval=0,
    )
    assert outcome.status == "pending"

    exit_code = watch_cli_main(["pending"])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert str(generate_game_id("aomx")) in captured.out
    assert "atalho: aomx" in captured.out

    monkeypatch.setattr(importer, "is_steam_running", lambda: False)
    monkeypatch.setattr(shortcuts, "is_steam_running", lambda: False)
    exit_code = watch_cli_main(
        [
            "pending",
            "--apply",
            "--steam-userdata",
            str(userdata),
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "aplicado:" in captured.out
    assert load_pending(pending_path) == []
