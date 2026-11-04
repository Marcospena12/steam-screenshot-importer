from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Callable, Mapping
import urllib.error
import urllib.parse
import urllib.request

from PIL import Image
from PySide6.QtCore import QObject, QRectF, QRunnable, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPixmap,
)

from ..games import default_config_path
from ..steam import find_steam_installation

MAX_STEAM_APPID = 0xFFFFFFFF
MAX_WIDTH = 300
COVER_ASPECT_RATIO = 2 / 3  # width / height (ex: 300x450, 120x180, 60x90)
NETWORK_TIMEOUT = 5.0
BLOCK_DURATION_SECONDS = 24 * 3600  # 24 h
USER_AGENT = "SteamShotBridge/0.1.0 (Windows)"
ALLOWED_DOMAINS = ("store.steampowered.com", "cdn.akamai.steamstatic.com")


def default_covers_dir() -> Path:
    return default_config_path().parent / "covers"


def default_covers_index_path() -> Path:
    return default_config_path().parent / "covers_index.json"


def _appid32(game_id: int | str) -> int:
    val = int(game_id)
    return (val >> 32) & 0xFFFFFFFF if val > MAX_STEAM_APPID else val


def normalize_store_name(name: str) -> str:
    """Minúsculas, sem pontuação nem espaços (apenas alfanuméricos a-z e 0-9)."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def color_from_game_id(game_id: int | str, is_dark: bool = True) -> QColor:
    val = int(game_id)
    hue = (val ^ (val >> 16)) % 360
    saturation = 140 if is_dark else 120
    lightness = 45 if is_dark else 190
    return QColor.fromHsl(hue, saturation, lightness)


def initials_from_name(name: str) -> str:
    words = [w for w in name.strip().split() if w]
    return "".join(w[0].upper() for w in words[:3]) if words else "SG"


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


def load_covers_index(index_path: Path | str | None = None) -> dict[str, dict]:
    path = Path(index_path) if index_path is not None else default_covers_index_path()
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8-sig")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_covers_index(
    index_data: Mapping[str, dict],
    index_path: Path | str | None = None,
) -> None:
    path = Path(index_path) if index_path is not None else default_covers_index_path()
    _atomic_write_json(path, index_data)


def generate_placeholder_cover(
    game_id: int | str,
    game_name: str,
    width: int = 120,
    height: int = 180,
    is_dark: bool = True,
) -> QPixmap:
    pixmap = QPixmap(width, height)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    base_color = color_from_game_id(game_id, is_dark=is_dark)
    top_color = base_color.lighter(115)
    bottom_color = base_color.darker(120)

    gradient = QLinearGradient(0, 0, width, height)
    gradient.setColorAt(0.0, top_color)
    gradient.setColorAt(1.0, bottom_color)

    path = QPainterPath()
    path.addRoundedRect(QRectF(0.5, 0.5, width - 1, height - 1), 8, 8)
    painter.fillPath(path, gradient)

    border_color = QColor("#2A2F3D" if is_dark else "#CBD0D8")
    painter.setPen(border_color)
    painter.drawPath(path)

    initials = initials_from_name(game_name)
    font_initials = QFont("Segoe UI Variable", max(14, int(width * 0.22)), QFont.Weight.Bold)
    painter.setFont(font_initials)
    painter.setPen(QColor("#FFFFFF" if is_dark else "#14161B"))
    painter.drawText(
        QRectF(8, int(height * 0.25), width - 16, int(height * 0.3)),
        Qt.AlignmentFlag.AlignCenter,
        initials,
    )

    font_sub = QFont("Segoe UI Variable", max(8, int(width * 0.08)))
    painter.setFont(font_sub)
    painter.setPen(QColor("#D1D5DB" if is_dark else "#4B5563"))
    painter.drawText(
        QRectF(6, int(height * 0.65), width - 12, int(height * 0.28)),
        Qt.AlignmentFlag.AlignHCenter | Qt.TextFlag.TextWordWrap,
        game_name[:24],
    )

    painter.end()
    return pixmap


MAX_IMAGE_PIXELS = 40_000_000
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024  # 10 MB


def save_image_to_cache(
    source_path: Path | str,
    target_cache_path: Path,
) -> bool:
    try:
        target_cache_path.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(source_path) as img:
            if (img.width * img.height) > MAX_IMAGE_PIXELS:
                return False
            img = img.convert("RGB")
            if img.width > MAX_WIDTH:
                new_height = int(img.height * (MAX_WIDTH / img.width))
                img = img.resize((MAX_WIDTH, new_height), Image.Resampling.LANCZOS)
            temp_target = target_cache_path.with_suffix(".tmp.jpg")
            img.save(temp_target, "JPEG", quality=90)
            os.replace(temp_target, target_cache_path)
            return True
    except Exception:
        return False


def save_bytes_to_cache(
    data: bytes,
    target_cache_path: Path,
) -> bool:
    try:
        target_cache_path.parent.mkdir(parents=True, exist_ok=True)
        temp_file = target_cache_path.with_suffix(".dl.tmp")
        temp_file.write_bytes(data)
        success = save_image_to_cache(temp_file, target_cache_path)
        temp_file.unlink(missing_ok=True)
        return success
    except Exception:
        return False


def save_screenshot_crop_to_cache(
    screenshot_path: Path | str,
    target_cache_path: Path,
) -> bool:
    try:
        target_cache_path.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(screenshot_path) as img:
            img = img.convert("RGB")
            # Recorta no centro em proporção 2:3
            orig_w, orig_h = img.size
            target_aspect = 2 / 3  # width / height
            current_aspect = orig_w / orig_h

            if current_aspect > target_aspect:
                # Imagem mais larga que 2:3 -> corta laterais
                crop_w = int(orig_h * target_aspect)
                left = (orig_w - crop_w) // 2
                img = img.crop((left, 0, left + crop_w, orig_h))
            else:
                # Imagem mais alta que 2:3 -> corta topo/base
                crop_h = int(orig_w / target_aspect)
                top = (orig_h - crop_h) // 2
                img = img.crop((0, top, orig_w, top + crop_h))

            if img.width > MAX_WIDTH:
                new_height = int(img.height * (MAX_WIDTH / img.width))
                img = img.resize((MAX_WIDTH, new_height), Image.Resampling.LANCZOS)

            temp_target = target_cache_path.with_suffix(".tmp.jpg")
            img.save(temp_target, "JPEG", quality=90)
            os.replace(temp_target, target_cache_path)
            return True
    except Exception:
        return False


def download_url(
    url: str,
    timeout: float = NETWORK_TIMEOUT,
    url_opener: Callable[[str], bytes | None] | None = None,
) -> bytes | None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() != "https" or parsed.netloc.lower() not in ALLOWED_DOMAINS:
        return None

    if url_opener is not None:
        try:
            res = url_opener(url)
            if res and len(res) > MAX_DOWNLOAD_BYTES:
                return None
            return res
        except Exception:
            return None

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
                return b"".join(chunks)
    except Exception:
        return None
    return None


def search_steam_store_appid(
    game_name: str,
    timeout: float = NETWORK_TIMEOUT,
    url_opener: Callable[[str], bytes | None] | None = None,
) -> int | None:
    encoded_name = urllib.parse.quote(game_name.strip())
    url = f"https://store.steampowered.com/api/storesearch/?term={encoded_name}&cc=us&l=en"

    raw_bytes = download_url(url, timeout=timeout, url_opener=url_opener)
    if not raw_bytes:
        return None

    try:
        data = json.loads(raw_bytes.decode("utf-8", errors="replace"))
        items = data.get("items", [])
        norm_expected = normalize_store_name(game_name)
        for item in items:
            item_name = item.get("name", "")
            if normalize_store_name(item_name) == norm_expected:
                appid = item.get("id")
                if appid is not None:
                    return int(appid)
    except Exception:
        return None
    return None


def find_librarycache_cover(
    appid: int,
    steam_path: Path | None = None,
) -> Path | None:
    if steam_path is None:
        return None

    cache_dir = steam_path / "appcache" / "librarycache"
    if not cache_dir.is_dir():
        return None

    aid = str(appid)

    # 1. Estrutura antiga: <appid>_library_600x900.jpg ou .png
    for ext in ("jpg", "png", "jpeg"):
        cand = cache_dir / f"{aid}_library_600x900.{ext}"
        if cand.is_file():
            return cand

    # 2. Estrutura nova: pasta <appid>/ (com ou sem subpastas de hash)
    app_dir = cache_dir / aid
    if app_dir.is_dir():
        for cand in app_dir.rglob("library_600x900.*"):
            if cand.is_file() and cand.suffix.lower() in (".jpg", ".png", ".jpeg"):
                return cand

    return None


def find_grid_cover(
    game_id: int | str,
    steam_userdata_dir: Path | str | None = None,
) -> Path | None:
    if steam_userdata_dir is None:
        return None
    grid_dir = Path(steam_userdata_dir) / "config" / "grid"
    if not grid_dir.is_dir():
        return None

    aid = str(_appid32(game_id))
    gid = str(game_id)
    candidates = [
        f"{aid}p.png",
        f"{aid}p.jpg",
        f"{aid}_p.png",
        f"{aid}_p.jpg",
        f"{gid}p.png",
        f"{gid}p.jpg",
        f"{gid}_p.png",
        f"{gid}_p.jpg",
    ]
    for c in candidates:
        target = grid_dir / c
        if target.is_file():
            return target
    return None


def find_latest_screenshot_thumbnail(
    game_id: int | str,
    steam_userdata_dir: Path | str | None = None,
) -> Path | None:
    if steam_userdata_dir is None:
        return None
    thumb_dir = Path(steam_userdata_dir) / "760" / "remote" / str(game_id) / "screenshots" / "thumbnails"
    if not thumb_dir.is_dir():
        return None

    thumbs = [p for p in thumb_dir.iterdir() if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png")]
    if not thumbs:
        return None

    thumbs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return thumbs[0]


@dataclass(frozen=True)
class CoverResolutionResult:
    game_id: int
    source: str
    cover_path: Path | None
    appid: int | None = None


def resolve_game_cover(
    game_id: int | str,
    game_name: str,
    steam_userdata_dir: Path | str | None = None,
    steam_path: Path | str | None = None,
    covers_dir: Path | str | None = None,
    index_path: Path | str | None = None,
    force: bool = False,
    url_opener: Callable[[str], bytes | None] | None = None,
) -> CoverResolutionResult:
    gid = int(game_id)
    gid_str = str(gid)
    target_dir = Path(covers_dir) if covers_dir is not None else default_covers_dir()
    idx_path = Path(index_path) if index_path is not None else default_covers_index_path()
    cached_cover = target_dir / f"{gid_str}.jpg"

    index = load_covers_index(idx_path)
    entry = index.get(gid_str, {})
    entry_source = entry.get("fonte")

    # Capa manual nunca é sobrescrita automaticamente
    if entry_source == "manual" and cached_cover.is_file() and not force:
        return CoverResolutionResult(
            game_id=gid,
            source="manual",
            cover_path=cached_cover,
            appid=entry.get("appid"),
        )

    # Cache hit válido sem requisição de rede
    if not force and cached_cover.is_file() and entry_source in ("local", "cdn", "busca", "grid", "screenshot"):
        return CoverResolutionResult(
            game_id=gid,
            source=entry_source,
            cover_path=cached_cover,
            appid=entry.get("appid"),
        )

    now = datetime.now(timezone.utc)
    fail_time_str = entry.get("data_ultima_falha")
    blocked_24h = False
    if not force and fail_time_str:
        try:
            fail_dt = datetime.fromisoformat(fail_time_str)
            if (now - fail_dt).total_seconds() < BLOCK_DURATION_SECONDS:
                blocked_24h = True
        except Exception:
            blocked_24h = False

    sp = Path(steam_path) if steam_path is not None else None
    if sp is None and steam_userdata_dir is not None:
        try:
            sp = Path(steam_userdata_dir).parents[1]
        except Exception:
            sp = None

    # ETAPA 1: Jogo da Steam (game_id <= 2**32 - 1)
    if gid <= MAX_STEAM_APPID:
        appid = gid
        # 1a. Local: librarycache
        local_path = find_librarycache_cover(appid, steam_path=sp)
        if local_path is not None and save_image_to_cache(local_path, cached_cover):
            entry = {
                "game_id": gid_str,
                "fonte": "local",
                "appid": appid,
                "data": now.isoformat(),
                "data_ultima_falha": None,
            }
            index[gid_str] = entry
            save_covers_index(index, idx_path)
            return CoverResolutionResult(gid, "local", cached_cover, appid)

        # 1b. CDN (se não bloqueado por falha em 24 h)
        if not blocked_24h:
            cdn_url = f"https://cdn.akamai.steamstatic.com/steam/apps/{appid}/library_600x900.jpg"
            img_data = download_url(cdn_url, url_opener=url_opener)
            if not img_data:
                cdn_url_fallback = f"https://cdn.akamai.steamstatic.com/steam/apps/{appid}/header.jpg"
                img_data = download_url(cdn_url_fallback, url_opener=url_opener)

            if img_data and save_bytes_to_cache(img_data, cached_cover):
                entry = {
                    "game_id": gid_str,
                    "fonte": "cdn",
                    "appid": appid,
                    "data": now.isoformat(),
                    "data_ultima_falha": None,
                }
                index[gid_str] = entry
                save_covers_index(index, idx_path)
                return CoverResolutionResult(gid, "cdn", cached_cover, appid)
            else:
                entry["data_ultima_falha"] = now.isoformat()
                index[gid_str] = entry
                save_covers_index(index, idx_path)

    # ETAPA 2: Jogo não Steam com arte no grid
    if gid > MAX_STEAM_APPID:
        grid_path = find_grid_cover(gid, steam_userdata_dir)
        if grid_path is not None and save_image_to_cache(grid_path, cached_cover):
            entry = {
                "game_id": gid_str,
                "fonte": "grid",
                "appid": None,
                "data": now.isoformat(),
                "data_ultima_falha": None,
            }
            index[gid_str] = entry
            save_covers_index(index, idx_path)
            return CoverResolutionResult(gid, "grid", cached_cover, None)

        # ETAPA 3: Jogo não Steam sem arte -> Busca Steam Store
        if not blocked_24h:
            found_appid = entry.get("appid")
            if not found_appid:
                found_appid = search_steam_store_appid(game_name, url_opener=url_opener)

            if found_appid:
                cdn_url = f"https://cdn.akamai.steamstatic.com/steam/apps/{found_appid}/library_600x900.jpg"
                img_data = download_url(cdn_url, url_opener=url_opener)
                if not img_data:
                    cdn_url_fb = f"https://cdn.akamai.steamstatic.com/steam/apps/{found_appid}/header.jpg"
                    img_data = download_url(cdn_url_fb, url_opener=url_opener)

                if img_data and save_bytes_to_cache(img_data, cached_cover):
                    entry = {
                        "game_id": gid_str,
                        "fonte": "busca",
                        "appid": found_appid,
                        "data": now.isoformat(),
                        "data_ultima_falha": None,
                    }
                    index[gid_str] = entry
                    save_covers_index(index, idx_path)
                    return CoverResolutionResult(gid, "busca", cached_cover, found_appid)

            # Falha de busca ou de download
            entry["data_ultima_falha"] = now.isoformat()
            index[gid_str] = entry
            save_covers_index(index, idx_path)

    # ETAPA 4: Fallback
    # 4a. Screenshot recente
    screenshot_thumb = find_latest_screenshot_thumbnail(gid, steam_userdata_dir)
    if screenshot_thumb is not None and save_screenshot_crop_to_cache(screenshot_thumb, cached_cover):
        entry = {
            "game_id": gid_str,
            "fonte": "screenshot",
            "appid": entry.get("appid"),
            "data": now.isoformat(),
            "data_ultima_falha": entry.get("data_ultima_falha"),
        }
        index[gid_str] = entry
        save_covers_index(index, idx_path)
        return CoverResolutionResult(gid, "screenshot", cached_cover, entry.get("appid"))

    # 4b. Placeholder
    entry = {
        "game_id": gid_str,
        "fonte": "placeholder",
        "appid": entry.get("appid"),
        "data": now.isoformat(),
        "data_ultima_falha": entry.get("data_ultima_falha"),
    }
    index[gid_str] = entry
    save_covers_index(index, idx_path)
    return CoverResolutionResult(gid, "placeholder", None, entry.get("appid"))


def set_custom_cover(
    game_id: int | str,
    source_image_path: Path | str,
    covers_dir: Path | str | None = None,
    index_path: Path | str | None = None,
) -> Path:
    gid_str = str(int(game_id))
    target_dir = Path(covers_dir) if covers_dir is not None else default_covers_dir()
    idx_path = Path(index_path) if index_path is not None else default_covers_index_path()
    target_cover = target_dir / f"{gid_str}.jpg"

    save_image_to_cache(source_image_path, target_cover)

    index = load_covers_index(idx_path)
    index[gid_str] = {
        "game_id": gid_str,
        "fonte": "manual",
        "appid": None,
        "data": datetime.now(timezone.utc).isoformat(),
        "data_ultima_falha": None,
    }
    save_covers_index(index, idx_path)
    return target_cover


class _ResolveWorkerSignals(QObject):
    resolved = Signal(object, str, object, str)  # (game_id, game_name, cover_path_or_none, source)


class CoverResolveRunnable(QRunnable):
    def __init__(
        self,
        game_id: int | str,
        game_name: str,
        steam_userdata_dir: Path | str | None = None,
        steam_path: Path | str | None = None,
        covers_dir: Path | str | None = None,
        index_path: Path | str | None = None,
        force: bool = False,
        url_opener: Callable[[str], bytes | None] | None = None,
    ) -> None:
        super().__init__()
        self.game_id = int(game_id)
        self.game_name = game_name
        self.steam_userdata_dir = steam_userdata_dir
        self.steam_path = steam_path
        self.covers_dir = covers_dir
        self.index_path = index_path
        self.force = force
        self.url_opener = url_opener
        self.signals = _ResolveWorkerSignals()

    def run(self) -> None:
        try:
            res = resolve_game_cover(
                self.game_id,
                self.game_name,
                steam_userdata_dir=self.steam_userdata_dir,
                steam_path=self.steam_path,
                covers_dir=self.covers_dir,
                index_path=self.index_path,
                force=self.force,
                url_opener=self.url_opener,
            )
            self.signals.resolved.emit(
                res.game_id,
                self.game_name,
                res.cover_path,
                res.source,
            )
        except Exception:
            self.signals.resolved.emit(
                self.game_id,
                self.game_name,
                None,
                "placeholder",
            )


class CoverManager(QObject):
    cover_loaded = Signal(object, str, object, str)  # (game_id, game_name, QPixmap, source)

    def __init__(
        self,
        covers_dir: Path | str | None = None,
        index_path: Path | str | None = None,
        steam_userdata_dir: Path | str | None = None,
        steam_path: Path | str | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.covers_dir = Path(covers_dir) if covers_dir is not None else default_covers_dir()
        self.index_path = Path(index_path) if index_path is not None else default_covers_index_path()
        self.steam_userdata_dir = Path(steam_userdata_dir) if steam_userdata_dir is not None else None
        self.steam_path = Path(steam_path) if steam_path is not None else None
        self._pool = QThreadPool.globalInstance()
        self._active_tasks: set[CoverResolveRunnable] = set()

    def get_cover_pixmap_sync(
        self,
        game_id: int | str,
        game_name: str,
        width: int = 120,
        height: int = 180,
        is_dark: bool = True,
    ) -> tuple[QPixmap, str]:
        gid_str = str(int(game_id))
        cached_file = self.covers_dir / f"{gid_str}.jpg"
        index = load_covers_index(self.index_path)
        source = index.get(gid_str, {}).get("fonte", "placeholder")

        if cached_file.is_file():
            pix = QPixmap(str(cached_file))
            if not pix.isNull():
                return (
                    pix.scaled(
                        width,
                        height,
                        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        Qt.TransformationMode.SmoothTransformation,
                    ).copy(0, 0, width, height),
                    source,
                )

        return generate_placeholder_cover(game_id, game_name, width, height, is_dark), "placeholder"

    def request_cover(
        self,
        game_id: int | str,
        game_name: str,
        force: bool = False,
        width: int = 120,
        height: int = 180,
        is_dark: bool = True,
        url_opener: Callable[[str], bytes | None] | None = None,
    ) -> tuple[QPixmap, str]:
        gid = int(game_id)
        gid_str = str(gid)
        cached_file = self.covers_dir / f"{gid_str}.jpg"
        index = load_covers_index(self.index_path)
        entry = index.get(gid_str, {})
        source = entry.get("fonte")

        # Se já está no cache e não for forçado
        if not force and cached_file.is_file() and source:
            pix = QPixmap(str(cached_file))
            if not pix.isNull():
                scaled = pix.scaled(
                    width,
                    height,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                ).copy(0, 0, width, height)
                return scaled, source

        # Retorna placeholder imediatamente e agenda resolução na threadpool
        placeholder = generate_placeholder_cover(gid, game_name, width, height, is_dark)

        runnable = CoverResolveRunnable(
            gid,
            game_name,
            steam_userdata_dir=self.steam_userdata_dir,
            steam_path=self.steam_path,
            covers_dir=self.covers_dir,
            index_path=self.index_path,
            force=force,
            url_opener=url_opener,
        )
        self._active_tasks.add(runnable)

        def on_resolved(resolved_gid: int, gname: str, cover_path: Path | None, res_source: str) -> None:
            self._active_tasks.discard(runnable)
            if cover_path is not None and cover_path.is_file():
                loaded_pix = QPixmap(str(cover_path))
                if not loaded_pix.isNull():
                    scaled_pix = loaded_pix.scaled(
                        width,
                        height,
                        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        Qt.TransformationMode.SmoothTransformation,
                    ).copy(0, 0, width, height)
                    self.cover_loaded.emit(resolved_gid, gname, scaled_pix, res_source)
                    return
            fallback_ph = generate_placeholder_cover(resolved_gid, gname, width, height, is_dark)
            self.cover_loaded.emit(resolved_gid, gname, fallback_ph, res_source)

        runnable.signals.resolved.connect(on_resolved)
        self._pool.start(runnable)

        return placeholder, "placeholder"
