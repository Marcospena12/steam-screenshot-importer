from __future__ import annotations

import ctypes
import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .exe_map import (
    ExeMapError,
    ExeMapping,
    default_config_path,
    is_shortcut_applied,
    resolve_exe,
    update_exe_mapping,
)
from .games import (
    GameError,
    create_custom_game,
    custom_game_id,
    list_games,
    normalize_game_name,
)
from .importer import (
    STATUS_DRY_RUN,
    STATUS_VDF_PENDING,
    ImportResult,
    ScreenshotImportError,
    import_screenshot,
)
from .name_resolver import resolve_game_name
from .pending import (
    PendingError,
    apply_pending,
    default_pending_path,
    enqueue_import,
    update_pending_game_name,
)
from .shortcuts import ShortcutError
from .steam import SteamError, is_steam_running

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp"}
SCREENSHOT_NAME_RE = re.compile(
    r"^(?P<exe>.+)_(\d{4})_(\d{2})_(\d{2})_(\d{2})_(\d{2})_(\d{2})_(\d{3})"
    r"\.(png|jpg|jpeg|bmp)$",
    re.IGNORECASE,
)
STABILIZE_INTERVAL = 0.5
STEAM_CHECK_INTERVAL = 5.0


class WatcherError(Exception):
    pass


@dataclass(frozen=True)
class Event:
    tipo: str  # "detectado", "identificado", "importado", "pendente", "aplicado", "aviso", "erro"
    mensagem: str
    dados: dict[str, object] = field(default_factory=dict)

    def startswith(self, prefix: str, *args) -> bool:
        return self.mensagem.startswith(prefix, *args)

    def __contains__(self, item: str) -> bool:
        return item in self.mensagem

    def __str__(self) -> str:
        return self.mensagem


@dataclass(frozen=True)
class ScreenshotName:
    exe: str
    moment: datetime
    milliseconds: int

    @property
    def order_key(self) -> tuple[float, int]:
        return (self.moment.timestamp(), self.milliseconds)


@dataclass(frozen=True)
class ProcessOutcome:
    status: str
    path: Path
    lines: tuple[str, ...] = ()
    events: tuple[Event, ...] = ()
    import_result: ImportResult | None = None
    error: str | None = None


def parse_screenshot_name(filename: str | Path) -> ScreenshotName | None:
    name = Path(filename).name
    match = SCREENSHOT_NAME_RE.match(name)
    if match is None:
        return None
    exe = match.group("exe")
    if not exe:
        return None
    try:
        moment = datetime(
            int(match.group(2)),
            int(match.group(3)),
            int(match.group(4)),
            int(match.group(5)),
            int(match.group(6)),
            int(match.group(7)),
        )
    except ValueError:
        return None
    return ScreenshotName(
        exe=exe,
        moment=moment,
        milliseconds=int(match.group(8)),
    )


def default_processed_path() -> Path:
    return default_config_path().with_name("processed.json")


def _path_key(path: Path | str) -> str:
    return os.path.normcase(str(Path(path).resolve()))


_windows_kernel32 = None


def _windows_can_open_exclusive(path: Path) -> bool:
    global _windows_kernel32
    if _windows_kernel32 is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        _windows_kernel32 = kernel32

    GENERIC_READ = 0x80000000
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80
    handle = _windows_kernel32.CreateFileW(
        str(path),
        GENERIC_READ,
        0,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle is None or handle == invalid:
        return False
    _windows_kernel32.CloseHandle(handle)
    return True


def can_open_exclusive(path: Path | str) -> bool:
    path = Path(path)
    if os.name == "nt":
        return _windows_can_open_exclusive(path)
    try:
        with path.open("rb"):
            return True
    except OSError:
        return False


def wait_until_stable(
    path: Path | str,
    interval: float = STABILIZE_INTERVAL,
    attempts: int = 1,
) -> bool:
    path = Path(path)
    attempts = max(1, attempts)
    for attempt in range(attempts):
        try:
            before = path.stat()
        except OSError:
            return False
        if not can_open_exclusive(path):
            if attempt + 1 < attempts:
                time.sleep(interval)
            continue
        if interval > 0:
            time.sleep(interval)
        try:
            after = path.stat()
        except OSError:
            return False
        if (
            before.st_size == after.st_size
            and before.st_mtime_ns == after.st_mtime_ns
            and can_open_exclusive(path)
        ):
            return True
        if attempt + 1 < attempts:
            time.sleep(interval)
    return False


class ProcessedRegistry:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = (
            Path(path)
            if path is not None
            else default_processed_path()
        )
        self._files: dict[str, dict[str, int]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = self.path.read_text(encoding="utf-8-sig")
            data = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WatcherError(
                f"Não foi possível ler {self.path}: {type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise WatcherError(f"Formato inválido em {self.path}")
        files = data.get("files", {})
        if not isinstance(files, dict):
            raise WatcherError(f"'files' deve ser um objeto em {self.path}")
        for key, value in files.items():
            if not isinstance(value, dict):
                continue
            try:
                size = int(value.get("size", -1))
                mtime_ns = int(value.get("mtime_ns", -1))
            except (TypeError, ValueError):
                continue
            self._files[str(key)] = {
                "size": size,
                "mtime_ns": mtime_ns,
            }

    def is_processed(
        self,
        path: Path | str,
        stat_result: os.stat_result | None = None,
    ) -> bool:
        key = _path_key(path)
        stored = self._files.get(key)
        if stored is None:
            return False
        try:
            stat_result = stat_result or Path(path).stat()
        except OSError:
            return False
        return (
            stored.get("size") == stat_result.st_size
            and stored.get("mtime_ns") == stat_result.st_mtime_ns
        )

    def mark(
        self,
        path: Path | str,
        stat_result: os.stat_result | None = None,
    ) -> None:
        stat_result = stat_result or Path(path).stat()
        self._files[_path_key(path)] = {
            "size": stat_result.st_size,
            "mtime_ns": stat_result.st_mtime_ns,
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = (
            json.dumps(
                {"files": self._files},
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def contains_path(self, path: Path | str) -> bool:
        return _path_key(path) in self._files


def resolve_fallback_game(
    steam_userdata_dir: Path | str,
    config_path: Path | str,
    game_name: str | None = None,
    game_id: str | int | None = None,
    dry_run: bool = False,
) -> tuple[str, str | None] | None:
    if game_id is not None:
        try:
            target = int(game_id)
        except (TypeError, ValueError) as exc:
            raise WatcherError(f"game_id inválido: {game_id!r}") from exc
        matches = [
            game
            for game in list_games(steam_userdata_dir, config_path)
            if game.game_id == target
        ]
        name = matches[0].name if matches else game_name
        return str(target), name

    if game_name is not None:
        normalized = normalize_game_name(game_name)
        matches = [
            game
            for game in list_games(steam_userdata_dir, config_path)
            if game.name == normalized
        ]
        if len(matches) > 1:
            raise WatcherError(
                f'Mais de um jogo encontrado com o nome "{normalized}". '
                "Use --game-id."
            )
        if matches:
            return str(matches[0].game_id), matches[0].name
        if dry_run:
            return str(custom_game_id(normalized)), normalized
        game = create_custom_game(normalized, config_path)
        return str(game.game_id), game.name

    return None


def process_file(
    path: Path | str,
    *,
    steam_userdata_dir: Path | str,
    config_path: Path | str,
    processed: ProcessedRegistry | None = None,
    pending_path: Path | str | None = None,
    fallback_game_name: str | None = None,
    fallback_game_id: str | int | None = None,
    dry_run: bool = False,
    create_shortcut: bool = True,
    hidden_shortcut: bool = False,
    stabilize_interval: float = STABILIZE_INTERVAL,
    wait_stable: bool = True,
    name_resolver_fn: Callable[[str], tuple[str, str]] | None = None,
) -> ProcessOutcome:
    source = Path(path)

    if source.suffix.lower() not in IMAGE_SUFFIXES:
        msg = f"ignorado: {source.name}: extensão não suportada"
        ev = Event(tipo="aviso", mensagem=msg, dados={"path": source})
        return ProcessOutcome(
            status="ignored_extension",
            path=source,
            lines=(msg,),
            events=(ev,),
        )

    try:
        stat_result = source.stat()
    except OSError as exc:
        error = f"{type(exc).__name__}: {exc}"
        msg = f"erro: {source.name}: {error}"
        ev = Event(tipo="erro", mensagem=msg, dados={"path": source, "error": error})
        return ProcessOutcome(
            status="error",
            path=source,
            lines=(msg,),
            events=(ev,),
            error=error,
        )

    if processed is not None and processed.is_processed(source, stat_result):
        msg = f"duplicata ignorada: {source}"
        ev = Event(tipo="aviso", mensagem=msg, dados={"path": source})
        return ProcessOutcome(
            status="duplicate",
            path=source,
            lines=(msg,),
            events=(ev,),
        )

    if wait_stable and not wait_until_stable(source, interval=stabilize_interval):
        return ProcessOutcome(status="unstable", path=source)

    parsed = parse_screenshot_name(source.name)
    events: list[Event] = []
    creation_time: datetime | None = None
    milliseconds = 0
    mapping: ExeMapping | None = None

    if parsed is not None:
        try:
            mapping = resolve_exe(
                parsed.exe,
                config_path,
                steam_userdata_dir,
                dry_run=dry_run,
                name_resolver_fn=name_resolver_fn,
            )
        except ExeMapError as exc:
            error = str(exc)
            msg = f"erro: {source.name}: {error}"
            ev = Event(tipo="erro", mensagem=msg, dados={"path": source, "error": error})
            return ProcessOutcome(
                status="error",
                path=source,
                lines=(msg,),
                events=(ev,),
                error=error,
            )
        except (OSError, ValueError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            msg = f"erro: {source.name}: {error}"
            ev = Event(tipo="erro", mensagem=msg, dados={"path": source, "error": error})
            return ProcessOutcome(
                status="error",
                path=source,
                lines=(msg,),
                events=(ev,),
                error=error,
            )

        if mapping.ignored:
            msg = f"aviso: jogo ignorado: {mapping.name}"
            ev = Event(
                tipo="aviso",
                mensagem=msg,
                dados={
                    "game_id": str(mapping.game_id),
                    "game_name": mapping.name,
                    "path": source,
                    "ignored": True,
                },
            )
            return ProcessOutcome(
                status="ignored",
                path=source,
                lines=(msg,),
                events=(ev,),
            )

        if not mapping.is_new and mapping.provisional:
            resolver = name_resolver_fn or resolve_game_name
            better_name, better_source = resolver(parsed.exe)
            if better_source != "exe" and better_name != mapping.name:
                if is_shortcut_applied(steam_userdata_dir, mapping.game_id):
                    msg = f"aviso: nome melhor disponível: {better_name}"
                    events.append(
                        Event(
                            tipo="aviso",
                            mensagem=msg,
                            dados={"better_name": better_name, "game_id": mapping.game_id},
                        )
                    )
                else:
                    if not dry_run:
                        update_exe_mapping(
                            mapping.key,
                            better_name,
                            better_source,
                            provisional=False,
                            config_path=config_path,
                        )
                        if pending_path is not None:
                            update_pending_game_name(
                                mapping.game_id,
                                better_name,
                                pending_path,
                            )
                    mapping = ExeMapping(
                        key=mapping.key,
                        exe=parsed.exe,
                        name=better_name,
                        game_id=mapping.game_id,
                        provisional=False,
                        is_new=False,
                        source=better_source,
                    )

        game_id = str(mapping.game_id)
        game_name: str | None = mapping.name
        creation_time = parsed.moment
        milliseconds = parsed.milliseconds
        origin = "novo" if mapping.is_new else "existente"
        msg = (
            f"jogo identificado: {parsed.exe} -> {mapping.name} "
            f"(fonte: {mapping.source}) [{origin}]"
        )
        events.append(
            Event(
                tipo="identificado",
                mensagem=msg,
                dados={
                    "exe": parsed.exe,
                    "game_name": mapping.name,
                    "game_id": game_id,
                    "source": mapping.source,
                    "is_new": mapping.is_new,
                    "path": source,
                },
            )
        )
    else:
        try:
            fallback = resolve_fallback_game(
                steam_userdata_dir,
                config_path,
                game_name=fallback_game_name,
                game_id=fallback_game_id,
                dry_run=dry_run,
            )
        except (GameError, WatcherError, OSError, ValueError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            msg = f"erro: {source.name}: {error}"
            ev = Event(tipo="erro", mensagem=msg, dados={"path": source, "error": error})
            return ProcessOutcome(
                status="error",
                path=source,
                lines=(msg,),
                events=(ev,),
                error=error,
            )
        if fallback is None:
            msg = f"sem jogo identificado: {source.name}"
            ev = Event(tipo="aviso", mensagem=msg, dados={"path": source})
            return ProcessOutcome(
                status="no_game",
                path=source,
                lines=(msg,),
                events=(ev,),
            )
        game_id, game_name = fallback
        try:
            milliseconds = stat_result.st_mtime_ns // 1_000_000 % 1000
        except AttributeError:
            milliseconds = int(stat_result.st_mtime * 1000) % 1000
        msg = (
            f"jogo identificado: {source.name} -> "
            f"{game_name or '(sem nome)'} ({game_id}) [fallback]"
        )
        events.append(
            Event(
                tipo="identificado",
                mensagem=msg,
                dados={
                    "game_name": game_name,
                    "game_id": game_id,
                    "fallback": True,
                    "path": source,
                },
            )
        )

    import_result: ImportResult | None = None
    try:
        import_result = import_screenshot(
            image_path=source,
            game_id=game_id,
            steam_userdata_dir=steam_userdata_dir,
            game_name=game_name,
            creation_time=creation_time,
            dry_run=dry_run,
            create_shortcut=create_shortcut,
            hidden_shortcut=hidden_shortcut,
        )
    except (
        ScreenshotImportError,
        ShortcutError,
        SteamError,
        GameError,
        OSError,
        ValueError,
    ) as exc:
        error = f"{type(exc).__name__}: {exc}"
        msg = f"erro: {source.name}: {error}"
        events.append(Event(tipo="erro", mensagem=msg, dados={"path": source, "error": error}))
        return ProcessOutcome(
            status="error",
            path=source,
            lines=tuple(e.mensagem for e in events),
            events=tuple(events),
            error=error,
        )
    except Exception as exc:  # pragma: no cover - defensivo
        error = f"{type(exc).__name__}: {exc}"
        msg = f"erro: {source.name}: {error}"
        events.append(Event(tipo="erro", mensagem=msg, dados={"path": source, "error": error}))
        return ProcessOutcome(
            status="error",
            path=source,
            lines=tuple(e.mensagem for e in events),
            events=tuple(events),
            error=error,
        )

    if import_result.status == STATUS_VDF_PENDING:
        status = "pending"
        msg = f"pendente: {source.name} -> {game_name or game_id} ({game_id})"
        events.append(
            Event(
                tipo="pendente",
                mensagem=msg,
                dados={
                    "path": source,
                    "game_name": game_name or game_id,
                    "game_id": game_id,
                },
            )
        )
        if not dry_run:
            try:
                enqueue_import(
                    import_result,
                    pending_path,
                    milliseconds=milliseconds,
                )
            except (PendingError, OSError, ValueError) as exc:
                error = f"{type(exc).__name__}: {exc}"
                msg = f"erro: fila de pendências: {error}"
                events.append(Event(tipo="erro", mensagem=msg, dados={"error": error}))
    elif import_result.status == STATUS_DRY_RUN:
        status = "dry_run"
        msg = f"dry-run: {source.name} -> {import_result.image_path}"
        events.append(Event(tipo="aviso", mensagem=msg, dados={"path": source, "image_path": import_result.image_path}))
    else:
        status = "imported"
        msg = f"importado: {source.name} -> {import_result.image_path}"
        events.append(
            Event(
                tipo="importado",
                mensagem=msg,
                dados={
                    "path": source,
                    "image_path": import_result.image_path,
                    "game_name": game_name or game_id,
                    "game_id": game_id,
                },
            )
        )

    if import_result.status != STATUS_DRY_RUN and processed is not None:
        try:
            processed.mark(source, stat_result)
            processed.save()
        except (OSError, WatcherError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            msg = f"erro: registro de processados: {error}"
            events.append(Event(tipo="erro", mensagem=msg, dados={"error": error}))

    return ProcessOutcome(
        status=status,
        path=source,
        lines=tuple(e.mensagem for e in events),
        events=tuple(events),
        import_result=import_result,
    )


def _order_key(path: Path) -> tuple[int, float, int, str]:
    parsed = parse_screenshot_name(path.name)
    if parsed is not None:
        moment, millis = parsed.order_key
        return (0, moment, millis, path.name.casefold())
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (1, mtime, 0, path.name.casefold())


class _Handler(FileSystemEventHandler):
    def __init__(self, watcher: "FolderWatcher") -> None:
        self.watcher = watcher

    def on_created(self, event) -> None:
        if not event.is_directory:
            self.watcher.enqueue_path(event.src_path)

    def on_modified(self, event) -> None:
        if not event.is_directory:
            self.watcher.enqueue_path(event.src_path)

    def on_moved(self, event) -> None:
        if not event.is_directory:
            self.watcher.enqueue_path(event.dest_path)


class FolderWatcher:
    def __init__(
        self,
        folder: Path | str,
        *,
        steam_userdata_dir: Path | str,
        config_path: Path | str | None = None,
        pending_path: Path | str | None = None,
        processed_path: Path | str | None = None,
        fallback_game_name: str | None = None,
        fallback_game_id: str | int | None = None,
        import_existing: bool = False,
        dry_run: bool = False,
        create_shortcut: bool = True,
        hidden_shortcut: bool = False,
        emit: Callable[[Event], None] | None = None,
        stabilize_interval: float = STABILIZE_INTERVAL,
        steam_check_interval: float = STEAM_CHECK_INTERVAL,
        steam_running: Callable[[], bool] | None = None,
        observer_factory: Callable[[], object] | None = None,
        name_resolver_fn: Callable[[str], tuple[str, str]] | None = None,
    ) -> None:
        self.folder = Path(folder)
        self.steam_userdata_dir = Path(steam_userdata_dir)
        self.config_path = (
            Path(config_path)
            if config_path is not None
            else default_config_path()
        )
        self.pending_path = (
            Path(pending_path)
            if pending_path is not None
            else default_pending_path()
        )
        self.processed_path = (
            Path(processed_path)
            if processed_path is not None
            else default_processed_path()
        )
        self.fallback_game_name = fallback_game_name
        self.fallback_game_id = fallback_game_id
        self.import_existing = import_existing
        self.dry_run = dry_run
        self.create_shortcut = create_shortcut
        self.hidden_shortcut = hidden_shortcut
        self.emit = emit if emit is not None else (lambda ev: print(ev.mensagem, flush=True))
        self.stabilize_interval = stabilize_interval
        self.steam_check_interval = steam_check_interval
        self._steam_running = steam_running or is_steam_running
        self._observer_factory = observer_factory or Observer
        self.name_resolver_fn = name_resolver_fn

        self.processed = ProcessedRegistry(self.processed_path)
        self._lock = threading.Lock()
        self._candidates: dict[str, tuple[Path, float]] = {}
        self._ignored_initial: set[str] = set()
        self._announced: set[str] = set()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._observer: object | None = None
        self._last_steam_running: bool | None = None
        self._apply_failed = False

    def scan_existing(self) -> list[Path]:
        if not self.folder.is_dir():
            raise WatcherError(f"Pasta inexistente: {self.folder}")
        files = sorted(
            (
                path
                for path in self.folder.rglob("*")
                if path.is_file()
                and path.suffix.lower() in IMAGE_SUFFIXES
            ),
            key=lambda path: _path_key(path),
        )
        to_import: list[Path] = []
        for path in files:
            key = _path_key(path)
            if self.import_existing:
                to_import.append(path)
                self.enqueue_path(path)
            else:
                self._ignored_initial.add(key)
        return to_import

    def enqueue_path(self, path: Path | str) -> None:
        candidate = Path(path)
        if candidate.suffix.lower() not in IMAGE_SUFFIXES:
            return
        key = _path_key(candidate)
        if key in self._ignored_initial:
            return
        with self._lock:
            if key in self._candidates:
                return
            self._candidates[key] = (candidate, time.monotonic())
            announce = key not in self._announced
            self._announced.add(key)
        if announce:
            self.emit(
                Event(
                    tipo="detectado",
                    mensagem=f"detectado: {candidate}",
                    dados={"path": candidate},
                )
            )
        self._wake.set()

    def start(self) -> None:
        self.scan_existing()
        observer = self._observer_factory()
        observer.schedule(_Handler(self), str(self.folder), recursive=True)
        observer.start()
        self._observer = observer
        self._thread = threading.Thread(
            target=self._worker,
            name="steamshotbridge-watch",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._observer is not None:
            self._observer.stop()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._observer is not None:
            self._observer.join(timeout=5)

    def run_forever(self) -> None:
        self.start()
        try:
            while not self._stop.wait(0.5):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def check_steam_pending(self) -> object | None:
        running = self._steam_running()
        previous = self._last_steam_running
        self._last_steam_running = running

        should_apply = False
        if not running:
            if previous is None or previous is True:
                should_apply = True
            elif previous is False and self._apply_failed:
                should_apply = True
        if not should_apply:
            return None

        try:
            result = apply_pending(
                self.steam_userdata_dir,
                pending_path=self.pending_path,
                steam_running=self._steam_running,
            )
        except Exception as exc:
            self._apply_failed = True
            self.emit(
                Event(
                    tipo="erro",
                    mensagem=f"erro: apply_pending: {type(exc).__name__}: {exc}",
                    dados={"error": str(exc)},
                )
            )
            return None

        self._apply_failed = False
        if result.status == "steam_aberta":
            self._last_steam_running = True
        if result.status == "aplicado":
            self.emit(
                Event(
                    tipo="aplicado",
                    mensagem=(
                        f"aplicado: {result.applied} pendência(s) "
                        f"({result.screenshots} screenshot(s), "
                        f"{result.shortcuts} atalho(s))"
                    ),
                    dados={
                        "applied": result.applied,
                        "screenshots": result.screenshots,
                        "shortcuts": result.shortcuts,
                    },
                )
            )
        return result

    def _process_candidates(self) -> bool:
        with self._lock:
            items = list(self._candidates.items())
        if not items:
            return False
        items.sort(key=lambda item: _order_key(item[1][0]))

        processed_any = False
        for key, (path, _) in items:
            if self._stop.is_set():
                break
            try:
                outcome = process_file(
                    path,
                    steam_userdata_dir=self.steam_userdata_dir,
                    config_path=self.config_path,
                    processed=self.processed,
                    pending_path=self.pending_path,
                    fallback_game_name=self.fallback_game_name,
                    fallback_game_id=self.fallback_game_id,
                    dry_run=self.dry_run,
                    create_shortcut=self.create_shortcut,
                    hidden_shortcut=self.hidden_shortcut,
                    stabilize_interval=self.stabilize_interval,
                    name_resolver_fn=self.name_resolver_fn,
                )
            except Exception as exc:
                with self._lock:
                    self._candidates.pop(key, None)
                    self._announced.discard(key)
                processed_any = True
                self.emit(
                    Event(
                        tipo="erro",
                        mensagem=f"erro: {path}: {type(exc).__name__}: {exc}",
                        dados={"path": path, "error": str(exc)},
                    )
                )
                continue
            if outcome.status == "unstable":
                continue
            with self._lock:
                self._candidates.pop(key, None)
                self._announced.discard(key)
            processed_any = True
            for ev in outcome.events:
                self.emit(ev)
        return processed_any

    def _worker(self) -> None:
        self.check_steam_pending()
        last_steam_check = time.monotonic()
        while not self._stop.is_set():
            if time.monotonic() - last_steam_check >= self.steam_check_interval:
                self.check_steam_pending()
                last_steam_check = time.monotonic()
            processed_any = self._process_candidates()
            if processed_any:
                continue
            self._wake.wait(0.1)
            self._wake.clear()
