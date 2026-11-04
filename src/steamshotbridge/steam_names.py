from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Callable, Mapping
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
import vdf

from .games import default_config_path
from .steam import find_steam_installation

MAX_STEAM_APPID = 0xFFFFFFFF
NETWORK_TIMEOUT = 5.0
BLOCK_DURATION_SECONDS = 24 * 3600  # 24 h
USER_AGENT = "SteamShotBridge/0.1.0 (Windows)"
ALLOWED_STORE_DOMAIN = "store.steampowered.com"


MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024  # 5 MB


def default_steam_names_cache_path() -> Path:
    return default_config_path().parent / "steam_names.json"


def validate_steam_game_name(raw: str | None) -> str | None:
    if not raw or not isinstance(raw, str):
        return None
    cleaned = "".join(ch for ch in raw if unicodedata.category(ch) != "Cc")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned or len(cleaned) > 120:
        return None
    return cleaned


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


def load_steam_names_cache(cache_path: Path | str | None = None) -> dict[str, dict]:
    path = (
        Path(cache_path)
        if cache_path is not None
        else default_steam_names_cache_path()
    )
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8-sig")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_steam_names_cache(
    cache_data: Mapping[str, dict],
    cache_path: Path | str | None = None,
) -> None:
    path = (
        Path(cache_path)
        if cache_path is not None
        else default_steam_names_cache_path()
    )
    _atomic_write_json(path, cache_data)


def get_steam_libraries(steam_path: Path | str | None = None) -> list[Path]:
    if steam_path is None:
        return []

    sp = Path(steam_path)
    libraries: list[Path] = [sp]
    libraryfolders_file = sp / "steamapps" / "libraryfolders.vdf"
    if not libraryfolders_file.is_file():
        return libraries

    try:
        text = libraryfolders_file.read_text(encoding="utf-8", errors="replace")
        root = vdf.loads(text, merge_duplicate_keys=False)
        folders = root.get("libraryfolders", root)
        if isinstance(folders, Mapping):
            for entry in folders.values():
                if isinstance(entry, Mapping):
                    p_str = entry.get("path")
                    if p_str and isinstance(p_str, str):
                        p = Path(p_str)
                        if p.is_dir() and p not in libraries:
                            libraries.append(p)
    except Exception:
        pass

    return libraries


def find_manifest_game_name(
    appid: int | str,
    steam_path: Path | str | None = None,
) -> str | None:
    aid = str(appid)
    libraries = get_steam_libraries(steam_path)
    for lib in libraries:
        manifest = lib / "steamapps" / f"appmanifest_{aid}.acf"
        if not manifest.is_file():
            continue
        try:
            text = manifest.read_text(encoding="utf-8", errors="replace")
            data = vdf.loads(text, merge_duplicate_keys=False)
            app_state = data.get("AppState", data)
            if isinstance(app_state, Mapping):
                raw_name = app_state.get("name")
                validated = validate_steam_game_name(raw_name)
                if validated is not None:
                    return validated
        except Exception:
            continue
    return None


def fetch_store_app_name(
    appid: int | str,
    timeout: float = NETWORK_TIMEOUT,
    url_opener: Callable[[str], bytes | None] | None = None,
) -> str | None:
    aid = str(appid)
    url = f"https://store.steampowered.com/api/appdetails?appids={aid}&filters=basic&l=english"
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() != "https" or parsed.netloc.lower() != ALLOWED_STORE_DOMAIN:
        return None

    raw_bytes: bytes | None = None
    if url_opener is not None:
        try:
            res = url_opener(url)
            if res and len(res) > MAX_DOWNLOAD_BYTES:
                return None
            raw_bytes = res
        except Exception:
            return None
    else:
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT},
            )
            with urllib.request.urlopen(req, timeout=timeout) as response:
                if response.status == 200:
                    chunks: list[bytes] = []
                    total = 0
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            return None
                        chunks.append(chunk)
                    raw_bytes = b"".join(chunks)
        except Exception:
            return None

    if not raw_bytes:
        return None

    try:
        data = json.loads(raw_bytes.decode("utf-8", errors="replace"))
        app_res = data.get(aid, {})
        if app_res.get("success") is True:
            app_data = app_res.get("data", {})
            if isinstance(app_data, Mapping):
                raw_name = app_data.get("name")
                return validate_steam_game_name(raw_name)
    except Exception:
        return None

    return None


@dataclass(frozen=True)
class SteamNameResult:
    appid: int
    name: str
    source: str  # "manifest", "cache", "store", "fallback"


def resolve_steam_game_name(
    appid: int | str,
    steam_path: Path | str | None = None,
    cache_path: Path | str | None = None,
    allow_network: bool = True,
    url_opener: Callable[[str], bytes | None] | None = None,
    force: bool = False,
) -> SteamNameResult:
    aid = int(appid)
    aid_str = str(aid)
    fallback_name = f"AppID {aid}"

    cache = load_steam_names_cache(cache_path)
    entry = cache.get(aid_str, {})
    now = datetime.now(timezone.utc)

    # 1. Local: appmanifest nas bibliotecas Steam
    manifest_name = find_manifest_game_name(aid, steam_path=steam_path)
    if manifest_name is not None:
        entry = {
            "appid": aid,
            "nome": manifest_name,
            "fonte": "manifest",
            "data": now.isoformat(),
            "data_ultima_falha": None,
        }
        cache[aid_str] = entry
        save_steam_names_cache(cache, cache_path)
        return SteamNameResult(aid, manifest_name, "manifest")

    # 2. Cache: se já estiver em cache com nome válido e não for forçado
    cached_name = entry.get("nome")
    if not force and cached_name:
        validated = validate_steam_game_name(cached_name)
        if validated is not None:
            return SteamNameResult(aid, validated, "cache")

    # 3. Loja: Steam Store API
    blocked_24h = False
    fail_time_str = entry.get("data_ultima_falha")
    if not force and fail_time_str:
        try:
            fail_dt = datetime.fromisoformat(fail_time_str)
            if (now - fail_dt).total_seconds() < BLOCK_DURATION_SECONDS:
                blocked_24h = True
        except Exception:
            blocked_24h = False

    if allow_network and not blocked_24h:
        store_name = fetch_store_app_name(aid, url_opener=url_opener)
        if store_name is not None:
            entry = {
                "appid": aid,
                "nome": store_name,
                "fonte": "store",
                "data": now.isoformat(),
                "data_ultima_falha": None,
            }
            cache[aid_str] = entry
            save_steam_names_cache(cache, cache_path)
            return SteamNameResult(aid, store_name, "store")
        else:
            entry["data_ultima_falha"] = now.isoformat()
            cache[aid_str] = entry
            save_steam_names_cache(cache, cache_path)

    # 4. Fallback
    return SteamNameResult(aid, fallback_name, "fallback")


class _NameResolveSignals(QObject):
    resolved = Signal(int, str, str)  # (appid, name, source)


class SteamNameResolveRunnable(QRunnable):
    def __init__(
        self,
        appid: int,
        steam_path: Path | str | None = None,
        cache_path: Path | str | None = None,
        force: bool = False,
        url_opener: Callable[[str], bytes | None] | None = None,
    ) -> None:
        super().__init__()
        self.appid = int(appid)
        self.steam_path = steam_path
        self.cache_path = cache_path
        self.force = force
        self.url_opener = url_opener
        self.signals = _NameResolveSignals()

    def run(self) -> None:
        try:
            res = resolve_steam_game_name(
                self.appid,
                steam_path=self.steam_path,
                cache_path=self.cache_path,
                allow_network=True,
                url_opener=self.url_opener,
                force=self.force,
            )
            self.signals.resolved.emit(res.appid, res.name, res.source)
        except Exception:
            self.signals.resolved.emit(self.appid, f"AppID {self.appid}", "fallback")


class SteamNameManager(QObject):
    name_resolved = Signal(int, str, str)  # (appid, name, source)

    def __init__(
        self,
        steam_path: Path | str | None = None,
        cache_path: Path | str | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.steam_path = Path(steam_path) if steam_path is not None else None
        self.cache_path = Path(cache_path) if cache_path is not None else default_steam_names_cache_path()
        self._pool = QThreadPool.globalInstance()
        self._active_tasks: set[SteamNameResolveRunnable] = set()

    def get_or_request_name(
        self,
        appid: int,
        force: bool = False,
        allow_network: bool = True,
        url_opener: Callable[[str], bytes | None] | None = None,
    ) -> tuple[str, str]:
        # Tenta resolução síncrona sem rede
        sync_res = resolve_steam_game_name(
            appid,
            steam_path=self.steam_path,
            cache_path=self.cache_path,
            allow_network=False,
            force=force,
        )
        if sync_res.source in ("manifest", "cache") and not force:
            return sync_res.name, sync_res.source

        if not allow_network:
            return sync_res.name, sync_res.source

        # Se precisa de rede, agenda resolução assíncrona na threadpool
        task = SteamNameResolveRunnable(
            appid,
            steam_path=self.steam_path,
            cache_path=self.cache_path,
            force=force,
            url_opener=url_opener,
        )
        self._active_tasks.add(task)

        def on_resolved(res_aid: int, res_name: str, res_source: str) -> None:
            self._active_tasks.discard(task)
            self.name_resolved.emit(res_aid, res_name, res_source)

        task.signals.resolved.connect(on_resolved)
        self._pool.start(task)

        return sync_res.name, sync_res.source
