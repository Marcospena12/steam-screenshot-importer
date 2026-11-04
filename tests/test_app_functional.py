from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import time

import pytest
from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from steamshotbridge import importer, pending as pending_module, shortcuts
from steamshotbridge.app import autostart
from steamshotbridge.app.log import setup_logging
from steamshotbridge.app.settings import Settings, load_settings, save_settings
from steamshotbridge.app.single_instance import SingleInstance
from steamshotbridge.app.tray import TrayManager
from steamshotbridge.app.window import MainWindow
from steamshotbridge.app.worker import WatcherWorker
from steamshotbridge.watcher import Event, FolderWatcher
from tools.watch import main as watch_cli_main


@pytest.fixture(autouse=True)
def _steam_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importer, "is_steam_running", lambda: False)
    monkeypatch.setattr(shortcuts, "is_steam_running", lambda: False)
    monkeypatch.setattr(pending_module, "is_steam_running", lambda: False)


def _setup_userdata(tmp_path: Path) -> Path:
    userdata = tmp_path / "Steam" / "userdata" / "12345"
    (userdata / "760").mkdir(parents=True, exist_ok=True)
    (userdata / "760" / "screenshots.vdf").write_text(
        '"screenshots"\n{\n    "shortcutnames"\n    {\n    }\n}\n',
        encoding="utf-8",
    )
    return userdata


# 1. SETTINGS
def test_settings_defaults_and_atomic_write(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    defaults = load_settings(config)
    assert defaults.watch_folder == ""
    assert defaults.notifications is True
    assert defaults.create_shortcut is True
    assert defaults.hidden_shortcut is False
    assert defaults.start_with_windows is False
    assert defaults.start_minimized is False
    assert defaults.theme == "auto"

    defaults.watch_folder = str(tmp_path / "shots")
    defaults.theme = "dark"
    defaults.notifications = False
    save_settings(defaults, config)

    loaded = load_settings(config)
    assert loaded.watch_folder == str(tmp_path / "shots")
    assert loaded.theme == "dark"
    assert loaded.notifications is False

    data = json.loads(config.read_text(encoding="utf-8"))
    assert "settings" in data
    assert data["settings"]["theme"] == "dark"


def test_settings_migration_preserves_exe_map_and_custom_games(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.json"
    legacy_data = {
        "watch_folder": "C:/Old/Shots",
        "theme": "light",
        "import_existing": True,
        "steam_userdata_dir": "C:/Steam/userdata/999888",
        "exe_map": {"aomx": {"name": "aomx", "game_id": "123", "provisional": True}},
        "custom_games": [{"name": "Custom", "game_id": "456"}],
    }
    config.write_text(json.dumps(legacy_data, indent=2), encoding="utf-8")

    migrated = load_settings(config)
    assert migrated.watch_folder == "C:/Old/Shots"
    assert migrated.theme == "light"
    assert migrated.steam_userdata_id == "999888"

    # Confirma que o arquivo foi atualizado com "settings" e preservou exe_map e custom_games
    raw_data = json.loads(config.read_text(encoding="utf-8"))
    assert "settings" in raw_data
    assert raw_data["settings"]["watch_folder"] == "C:/Old/Shots"
    assert raw_data["exe_map"] == legacy_data["exe_map"]
    assert raw_data["custom_games"] == legacy_data["custom_games"]


# 2. AUTOSTART
def test_autostart_with_mock_registry() -> None:
    backend = autostart.MockRegistryBackend()
    assert autostart.is_enabled(backend) is False

    autostart.enable(backend, command='"C:\\app.exe" -m steamshotbridge.app --minimized')
    assert autostart.is_enabled(backend) is True
    val = backend.get_value(autostart.REG_SUBKEY, autostart.REG_VALUE_NAME)
    assert "--minimized" in val

    autostart.disable(backend)
    assert autostart.is_enabled(backend) is False


# 3. WORKER
class MockFolderWatcher:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.emit = kwargs.get("emit")
        self.is_started = False
        self.is_stopped = False

    def start(self) -> None:
        self.is_started = True

    def stop(self) -> None:
        self.is_stopped = True


def test_worker_validation_errors(qapp: QApplication, tmp_path: Path) -> None:
    worker = WatcherWorker(
        watch_folder=tmp_path / "missing_folder",
        steam_userdata_dir=tmp_path / "missing_userdata",
    )

    errors: list[str] = []
    worker.erro.connect(errors.append)

    ok = worker.start()
    assert ok is False
    assert len(errors) == 1
    assert "inexistente" in errors[0]
    assert worker.current_state == "parado"


def test_worker_events_become_qt_signals_and_pause_resume(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    shots = tmp_path / "shots"
    shots.mkdir()
    userdata = _setup_userdata(tmp_path)

    worker = WatcherWorker(
        watch_folder=shots,
        steam_userdata_dir=userdata,
        watcher_factory=MockFolderWatcher,
    )

    logs: list[Event] = []
    imported_signals: list[tuple[str, str]] = []
    state_signals: list[str] = []

    worker.evento_log.connect(logs.append)
    worker.importado.connect(lambda g, a: imported_signals.append((g, a)))
    worker.estado.connect(state_signals.append)

    assert worker.start() is True
    assert worker.is_running is True
    assert "rodando" in state_signals

    # Emite evento através do callback do watcher
    test_ev = Event(
        tipo="importado",
        mensagem="importado: aomx.jpg -> C:/remote/1.jpg",
        dados={"game_name": "aomx", "image_path": "C:/remote/1.jpg"},
    )
    worker._on_watcher_event(test_ev)
    qapp.processEvents()

    assert len(logs) == 1
    assert len(imported_signals) == 1
    assert imported_signals[0] == ("aomx", "C:/remote/1.jpg")

    # Pausa
    worker.pause()
    assert worker.is_paused is True
    assert "pausado" in state_signals

    # Evento durante pausa é ignorado
    worker._on_watcher_event(test_ev)
    qapp.processEvents()
    assert len(logs) == 1

    # Retoma
    worker.resume()
    assert worker.is_running is True

    worker.stop()
    assert worker.current_state == "parado"


def test_worker_steam_closure_emits_signals(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    shots = tmp_path / "shots"
    shots.mkdir()
    userdata = _setup_userdata(tmp_path)

    steam_state = {"running": True}
    worker = WatcherWorker(
        watch_folder=shots,
        steam_userdata_dir=userdata,
        steam_running_fn=lambda: steam_state["running"],
        watcher_factory=MockFolderWatcher,
    )

    steam_signals: list[bool] = []
    worker.steam.connect(steam_signals.append)

    worker.start()
    time.sleep(0.1)
    qapp.processEvents()

    steam_state["running"] = False
    time.sleep(0.6)
    qapp.processEvents()

    assert False in steam_signals
    worker.stop()


# 4. SINGLE INSTANCE
def test_single_instance_detection(qapp: QApplication) -> None:
    app_id = "SteamShotBridge_Test_Unique_Socket_1234"
    first = SingleInstance(app_id=app_id)
    assert first.is_already_running() is False

    signals: list[bool] = []
    first.show_requested.connect(lambda: signals.append(True))

    second = SingleInstance(app_id=app_id)
    assert second.is_already_running() is True

    # Aguarda mensagem no socket
    for _ in range(20):
        qapp.processEvents()
        if signals:
            break
        time.sleep(0.02)

    assert len(signals) == 1
    first.close()


# 5. TRAY & CLOSE EVENT
def test_tray_menu_and_apply_enabled_when_steam_closed(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.json"
    userdata = _setup_userdata(tmp_path)
    shots = tmp_path / "shots"
    shots.mkdir()
    save_settings(Settings(watch_folder=str(shots)), config)

    window = MainWindow(config_path=config, steam_userdata_dir=userdata)
    tray = window.tray

    assert tray.action_open is not None
    assert tray.action_toggle_pause is not None
    assert tray.action_apply_pending is not None
    assert tray.action_quit is not None

    tray.set_steam_running(True)
    assert tray.action_apply_pending.isEnabled() is False

    tray.set_steam_running(False)
    assert tray.action_apply_pending.isEnabled() is True

    window.force_quit()


def test_close_event_hides_window_instead_of_quitting(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.json"
    userdata = _setup_userdata(tmp_path)
    shots = tmp_path / "shots"
    shots.mkdir()
    save_settings(Settings(watch_folder=str(shots)), config)

    window = MainWindow(config_path=config, steam_userdata_dir=userdata)
    window.show()
    assert window.isVisible() is True

    window.close()
    assert window.isVisible() is False
    assert window._is_quitting is False

    window.force_quit()


# 6. LOGGING ROTATIVO
def test_rotating_logger_creates_file(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    errors: list[tuple[str, str]] = []

    logger = setup_logging(
        log_dir=log_dir,
        ui_error_callback=lambda t, m: errors.append((t, m)),
    )
    logger.info("Teste de log informativo")

    log_file = log_dir / "app.log"
    assert log_file.is_file()
    assert "Teste de log informativo" in log_file.read_text(encoding="utf-8")


# 7. CLI WATCH IDÊNTICO
def test_cli_watch_output_identical_format(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.json"
    userdata = _setup_userdata(tmp_path)

    # Testa que o comando 'exes' continua gerando cabeçalho e linhas
    exit_code = watch_cli_main(["exes", "--config", str(config)])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "EXE\tNOME\tGAME_ID\tPROVISORIO" in captured.out


# 8. PONTOS DE ENTRADA INICIAM SEM ERRO
def test_entry_points_launch_cleanly() -> None:
    import subprocess
    import sys

    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")

    # tools/app.py
    p1 = subprocess.Popen([sys.executable, "tools/app.py"], env=env)
    try:
        p1.wait(timeout=1.5)
    except subprocess.TimeoutExpired:
        p1.kill()
        p1.wait(timeout=2.0)

    # python -m steamshotbridge.app --minimized
    p2 = subprocess.Popen(
        [sys.executable, "-m", "steamshotbridge.app", "--minimized"],
        env=env,
    )
    try:
        p2.wait(timeout=1.5)
    except subprocess.TimeoutExpired:
        p2.kill()
        p2.wait(timeout=2.0)
