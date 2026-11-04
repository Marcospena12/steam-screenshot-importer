from __future__ import annotations

from pathlib import Path
import threading
import time
from typing import Callable

from PySide6.QtCore import QObject, Signal

from ..pending import default_pending_path
from ..steam import is_steam_running
from ..watcher import Event, FolderWatcher, default_processed_path


class WatcherWorker(QObject):
    evento_log = Signal(object)  # Event
    importado = Signal(str, str)  # (jogo, arquivo)
    pendente = Signal(str)  # (jogo)
    aplicado = Signal(int)  # (qtd)
    erro = Signal(str)  # (msg)
    estado = Signal(str)  # "rodando" | "pausado" | "parado"
    steam = Signal(bool)  # aberta (True se aberta, False se fechada)

    def __init__(
        self,
        watch_folder: Path | str | None = None,
        steam_userdata_dir: Path | str | None = None,
        config_path: Path | str | None = None,
        pending_path: Path | str | None = None,
        processed_path: Path | str | None = None,
        fallback_game: str | None = None,
        create_shortcut: bool = True,
        hidden_shortcut: bool = False,
        steam_running_fn: Callable[[], bool] | None = None,
        watcher_factory: Callable[..., FolderWatcher] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.watch_folder = Path(watch_folder) if watch_folder else None
        self.steam_userdata_dir = (
            Path(steam_userdata_dir) if steam_userdata_dir else None
        )
        self.config_path = Path(config_path) if config_path else None
        self.pending_path = (
            Path(pending_path) if pending_path else default_pending_path()
        )
        self.processed_path = (
            Path(processed_path) if processed_path else default_processed_path()
        )
        self.fallback_game = fallback_game
        self.create_shortcut = create_shortcut
        self.hidden_shortcut = hidden_shortcut
        self.steam_running_fn = steam_running_fn or is_steam_running
        self._watcher_factory = watcher_factory

        self._state = "parado"
        self._paused = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._watcher: FolderWatcher | None = None
        self._last_steam: bool | None = None

    @property
    def is_running(self) -> bool:
        return self._state == "rodando"

    @property
    def is_paused(self) -> bool:
        return self._state == "pausado"

    @property
    def current_state(self) -> str:
        return self._state

    def validate_configuration(self) -> tuple[bool, str]:
        if not self.watch_folder or not self.watch_folder.is_dir():
            return False, "Pasta de capturas não configurada ou inexistente."

        if not self.steam_userdata_dir or not (self.steam_userdata_dir / "760").is_dir():
            return False, "Pasta userdata da Steam inválida (pasta 760 ausente)."

        return True, ""

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            self.stop()

        ok, err_msg = self.validate_configuration()
        if not ok:
            self._state = "parado"
            self.estado.emit("parado")
            self.erro.emit(err_msg)
            return False

        self._stop_event.clear()
        self._paused = False
        self._state = "rodando"
        self.estado.emit("rodando")

        self._thread = threading.Thread(
            target=self._run_loop,
            name="steamshotbridge-worker",
            daemon=True,
        )
        self._thread.start()
        return True

    def pause(self) -> None:
        if self._state == "rodando":
            self._paused = True
            self._state = "pausado"
            self.estado.emit("pausado")

    def resume(self) -> None:
        if self._state == "pausado":
            self._paused = False
            self._state = "rodando"
            self.estado.emit("rodando")

    def stop(self) -> None:
        self._stop_event.set()
        if self._watcher is not None:
            self._watcher.stop()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._watcher = None
        self._thread = None
        self._state = "parado"
        self.estado.emit("parado")

    def restart(
        self,
        watch_folder: Path | str | None = None,
        steam_userdata_dir: Path | str | None = None,
    ) -> bool:
        self.stop()
        if watch_folder is not None:
            self.watch_folder = Path(watch_folder) if watch_folder else None
        if steam_userdata_dir is not None:
            self.steam_userdata_dir = (
                Path(steam_userdata_dir) if steam_userdata_dir else None
            )
        return self.start()

    def _on_watcher_event(self, event: Event) -> None:
        if self._paused:
            return

        self.evento_log.emit(event)

        if event.tipo == "importado":
            game_name = str(event.dados.get("game_name", "Jogo"))
            image_path = str(event.dados.get("image_path", ""))
            self.importado.emit(game_name, image_path)

        elif event.tipo == "pendente":
            game_name = str(event.dados.get("game_name", "Jogo"))
            self.pendente.emit(game_name)

        elif event.tipo == "aplicado":
            qtd = int(event.dados.get("applied", 0))
            self.aplicado.emit(qtd)

        elif event.tipo == "erro":
            self.erro.emit(event.mensagem)

    def _run_loop(self) -> None:
        assert self.watch_folder is not None
        assert self.steam_userdata_dir is not None

        factory = self._watcher_factory or FolderWatcher
        self._watcher = factory(
            folder=self.watch_folder,
            steam_userdata_dir=self.steam_userdata_dir,
            config_path=self.config_path,
            pending_path=self.pending_path,
            processed_path=self.processed_path,
            fallback_game_name=self.fallback_game,
            create_shortcut=self.create_shortcut,
            hidden_shortcut=self.hidden_shortcut,
            emit=self._on_watcher_event,
            steam_running=self.steam_running_fn,
        )

        try:
            self._watcher.start()
        except Exception as exc:
            self.erro.emit(f"Falha ao iniciar o monitoramento: {exc}")
            self._state = "parado"
            self.estado.emit("parado")
            return

        while not self._stop_event.is_set():
            running = self.steam_running_fn()
            if running != self._last_steam:
                self._last_steam = running
                self.steam.emit(running)

            self._stop_event.wait(0.5)

        if self._watcher is not None:
            self._watcher.stop()
