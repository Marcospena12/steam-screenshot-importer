from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

import vdf
from PIL import Image

from .shortcuts import ShortcutResult, ensure_shortcut
from .steam import find_mapping_key, is_steam_running, vdf_text

THUMBNAIL_WIDTH = 200
JPEG_QUALITY = 95
HScreenshot_PLACEHOLDER = "18446744073709551615"

STATUS_IMPORTED = "imported"
STATUS_VDF_PENDING = "vdf_pendente"
STATUS_DRY_RUN = "dry_run"


class ScreenshotImportError(Exception):
    pass


@dataclass(frozen=True)
class VdfDocument:
    root: vdf.VDFDict
    raw: bytes
    eol: str


@dataclass(frozen=True)
class PendingVdfUpdate:
    vdf_path: Path
    game_id: str
    entry: dict[str, str]
    game_name: str | None
    shortcutname_added: bool


@dataclass(frozen=True)
class ImportResult:
    status: str
    image_path: Path
    thumbnail_path: Path
    vdf_path: Path
    vdf_snippet: str
    entry: dict[str, str]
    creation: int
    timelineid: str | None
    backup_path: Path | None = None
    pending: PendingVdfUpdate | None = None
    shortcut: ShortcutResult | None = None
    steam_running: bool = False


def _detect_eol(raw: bytes) -> str:
    crlf_count = raw.count(b"\r\n")
    lf_count = raw.count(b"\n") - crlf_count
    return "\r\n" if crlf_count > lf_count else "\n"


def load_vdf_document(path: Path) -> VdfDocument:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ScreenshotImportError(
            f"O screenshots.vdf não está em UTF-8 válido: {path}"
        ) from exc
    try:
        root = vdf.loads(text, mapper=vdf.VDFDict, merge_duplicate_keys=False)
    except Exception as exc:
        raise ScreenshotImportError(
            f"Falha ao interpretar {path}: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(root, vdf.VDFDict):
        raise ScreenshotImportError(f"Estrutura VDF inesperada em {path}")
    return VdfDocument(root=root, raw=raw, eol=_detect_eol(raw))


def _escape_vdf(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _render_mapping(mapping: Mapping, depth: int, lines: list[str]) -> None:
    lines.append("\t" * depth + "{")
    for key, value in mapping.items():
        prefix = "\t" * (depth + 1)
        label = f'"{_escape_vdf(key)}"'
        if isinstance(value, Mapping):
            lines.append(f"{prefix}{label}")
            _render_mapping(value, depth + 1, lines)
        else:
            lines.append(f'{prefix}{label}\t\t"{_escape_vdf(value)}"')
    lines.append("\t" * depth + "}")


def serialize_screenshots_vdf(root: Mapping, eol: str = "\n") -> str:
    lines: list[str] = []
    for key, value in root.items():
        lines.append(f'"{_escape_vdf(key)}"')
        if isinstance(value, Mapping):
            _render_mapping(value, 0, lines)
        else:
            lines.append(f'\t"{_escape_vdf(value)}"')
    return eol.join(lines) + eol


def _render_entry_snippet(index: str, entry: Mapping, eol: str) -> str:
    lines = [f'\t\t"{_escape_vdf(index)}"']
    _render_mapping(entry, 2, lines)
    return eol.join(lines) + eol


def _normalize_game_id(game_id: object) -> int:
    try:
        value = int(game_id)
    except (TypeError, ValueError) as exc:
        raise ScreenshotImportError(f"game_id inválido: {game_id!r}") from exc
    if value < 1 or value > 0xFFFFFFFFFFFFFFFF:
        raise ScreenshotImportError(
            "game_id deve estar entre 1 e 18446744073709551615"
        )
    return value


def _resolve_creation(
    creation_time: datetime | int | float | None,
    source_path: Path,
) -> tuple[int, datetime]:
    if creation_time is None:
        timestamp = int(source_path.stat().st_mtime)
        return timestamp, datetime.fromtimestamp(timestamp)
    if isinstance(creation_time, datetime):
        timestamp = int(creation_time.timestamp())
        if creation_time.tzinfo is None:
            return timestamp, creation_time
        return timestamp, creation_time
    if isinstance(creation_time, (int, float)) and not isinstance(creation_time, bool):
        timestamp = int(creation_time)
        return timestamp, datetime.fromtimestamp(timestamp)
    raise ScreenshotImportError(
        "creation_time deve ser datetime, timestamp numérico ou None"
    )


def _entry_creation(entry: object) -> int | None:
    if not isinstance(entry, Mapping):
        return None
    try:
        return int(vdf_text(find_mapping_key(entry, "creation")))
    except (TypeError, ValueError):
        return None


def _latest_screenshot_for_game(app_block: Mapping) -> tuple[int, Mapping] | None:
    latest: tuple[int, Mapping] | None = None
    for _, entry in app_block.items():
        if not isinstance(entry, Mapping):
            continue
        creation = _entry_creation(entry)
        if creation is None:
            continue
        if latest is None or creation > latest[0]:
            latest = (creation, entry)
    return latest


def _session_start_from_timelineid(timelineid: object) -> datetime | None:
    value = vdf_text(timelineid)
    if not value.startswith("timeline_") or "_" not in value:
        return None
    prefix, time_part = value.rsplit("_", 1)
    date_part = prefix[-8:]
    if len(time_part) != 6 or len(date_part) != 8:
        return None
    if not time_part.isdigit() or not date_part.isdigit():
        return None
    try:
        return datetime.strptime(
            date_part + time_part,
            "%Y%m%d%H%M%S",
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _new_timelineid(game_id: str, creation: int) -> str:
    moment = datetime.fromtimestamp(creation, tz=timezone.utc)
    return f"timeline_{game_id}{moment:%Y%m%d}_{moment:%H%M%S}"


def _build_timeline(
    game_id: str,
    creation: int,
    app_block: Mapping | None,
    session_gap_minutes: float,
    include_timeline: bool,
) -> tuple[str | None, str | None]:
    if not include_timeline:
        return None, None
    if session_gap_minutes < 0:
        raise ScreenshotImportError("session_gap_minutes não pode ser negativo")

    if app_block is not None:
        latest = _latest_screenshot_for_game(app_block)
        if latest is not None:
            latest_creation, latest_entry = latest
            diff = creation - latest_creation
            if 0 <= diff < session_gap_minutes * 60:
                latest_timelineid = find_mapping_key(latest_entry, "timelineid")
                session_start = _session_start_from_timelineid(latest_timelineid)
                if session_start is not None:
                    moment = int(session_start.timestamp())
                    if moment <= creation:
                        return (
                            vdf_text(latest_timelineid),
                            str((creation - moment) * 1000),
                        )

    return _new_timelineid(game_id, creation), "0"


def _build_entry(
    game_id: str,
    filename: str,
    width: int,
    height: int,
    creation: int,
    timelineid: str | None,
    timelinetime: str | None,
) -> vdf.VDFDict:
    values = [
        ("type", "1"),
        ("filename", f"{game_id}/screenshots/{filename}"),
        ("thumbnail", f"{game_id}/screenshots/thumbnails/{filename}"),
        ("imported", "1"),
    ]
    if timelineid is not None and timelinetime is not None:
        values.extend(
            [
                ("timelineid", timelineid),
                ("timelinetime", timelinetime),
            ]
        )
    values.extend(
        [
            ("width", str(width)),
            ("height", str(height)),
            ("gameid", game_id),
            ("creation", str(creation)),
            ("Permissions", "2"),
            ("hscreenshot", HScreenshot_PLACEHOLDER),
        ]
    )
    return vdf.VDFDict(values)


def _choose_filename(screenshots_dir: Path, timestamp_part: str) -> str:
    thumbnails_dir = screenshots_dir / "thumbnails"
    number = 1
    while True:
        filename = f"{timestamp_part}_{number}.jpg"
        if not (screenshots_dir / filename).exists() and not (
            thumbnails_dir / filename
        ).exists():
            return filename
        number += 1


def _merge_entry(app_block: Mapping, entry: Mapping) -> str:
    pairs = list(app_block.items())
    pairs.append((None, entry))

    def sort_key(item: tuple[object, object]) -> int:
        value = _entry_creation(item[1])
        return value if value is not None else -1

    pairs.sort(key=sort_key, reverse=True)
    final_index = "0"
    renumbered = vdf.VDFDict()
    for index, (_, value) in enumerate(pairs):
        renumbered[str(index)] = value
        if value is entry:
            final_index = str(index)

    app_block.clear()
    for key, value in renumbered.items():
        app_block[key] = value
    return final_index


def _insert_game_block_before_shortcutnames(
    screenshots: Mapping,
    game_id: str,
    app_block: vdf.VDFDict,
) -> None:
    rebuilt = vdf.VDFDict()
    inserted = False
    for key, value in screenshots.items():
        if not inserted and str(key).casefold() == "shortcutnames":
            rebuilt[game_id] = app_block
            inserted = True
        rebuilt[key] = value
    if not inserted:
        rebuilt[game_id] = app_block

    screenshots.clear()
    for key, value in rebuilt.items():
        screenshots[key] = value


def _add_shortcutname_if_needed(
    screenshots: Mapping,
    game_id: str,
    game_id_value: int,
    game_name: str | None,
) -> bool:
    if game_id_value <= 0xFFFFFFFF or not game_name:
        return False
    shortcutnames = find_mapping_key(screenshots, "shortcutnames")
    if shortcutnames is None:
        shortcutnames = vdf.VDFDict()
        screenshots["shortcutnames"] = shortcutnames
    if not isinstance(shortcutnames, Mapping):
        raise ScreenshotImportError(
            "O bloco shortcutnames do screenshots.vdf não é um dicionário"
        )
    if any(str(key) == game_id for key in shortcutnames.keys()):
        return False
    shortcutnames[game_id] = str(game_name)
    return True


def _apply_vdf_entry(
    root: Mapping,
    game_id: str,
    game_id_value: int,
    entry: Mapping,
    creation: int,
    game_name: str | None,
) -> tuple[Mapping, str, bool]:
    screenshots = find_mapping_key(root, "screenshots")
    if not isinstance(screenshots, Mapping):
        raise ScreenshotImportError(
            "O screenshots.vdf não contém um bloco 'screenshots'"
        )

    existing_key = next(
        (key for key in screenshots.keys() if str(key) == game_id),
        None,
    )
    if existing_key is not None:
        app_block = screenshots[existing_key]
        if not isinstance(app_block, Mapping):
            raise ScreenshotImportError(
                f"O bloco do game_id {game_id} não é um dicionário"
            )
        final_index = _merge_entry(app_block, entry)
    else:
        app_block = vdf.VDFDict()
        app_block["0"] = entry
        _insert_game_block_before_shortcutnames(screenshots, game_id, app_block)
        final_index = "0"

    shortcutname_added = _add_shortcutname_if_needed(
        screenshots,
        game_id,
        game_id_value,
        game_name,
    )
    return screenshots, final_index, shortcutname_added


def _source_image_format(path: Path) -> tuple[str | None, tuple[int, int]]:
    try:
        with Image.open(path) as image:
            image.load()
            return image.format, image.size
    except Exception as exc:
        raise ScreenshotImportError(
            f"Não foi possível ler a imagem {path}: {type(exc).__name__}: {exc}"
        ) from exc


def _write_images(
    source_path: Path,
    image_path: Path,
    thumbnail_path: Path,
    source_format: str | None,
    source_size: tuple[int, int],
) -> None:
    image_path.parent.mkdir(parents=True, exist_ok=True)
    thumbnail_path.parent.mkdir(parents=True, exist_ok=True)

    if source_format == "JPEG":
        shutil.copy2(source_path, image_path)
    else:
        try:
            with Image.open(source_path) as image:
                image.convert("RGB").save(
                    image_path,
                    format="JPEG",
                    quality=JPEG_QUALITY,
                )
        except Exception as exc:
            raise ScreenshotImportError(
                f"Falha ao converter a imagem para JPG: {type(exc).__name__}: {exc}"
            ) from exc

    width, height = source_size
    thumbnail_height = int(height * THUMBNAIL_WIDTH / width)
    if thumbnail_height < 1:
        raise ScreenshotImportError("A imagem é larga demais para gerar a thumbnail")
    try:
        with Image.open(image_path) as image:
            thumbnail = image.resize(
                (THUMBNAIL_WIDTH, thumbnail_height),
                Image.Resampling.LANCZOS,
            )
            thumbnail.save(thumbnail_path, format="JPEG", quality=JPEG_QUALITY)
    except Exception as exc:
        raise ScreenshotImportError(
            f"Falha ao gerar a thumbnail: {type(exc).__name__}: {exc}"
        ) from exc


def _create_backup(vdf_path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = vdf_path.with_name(f"{vdf_path.name}.bak_{timestamp}")
    counter = 1
    while candidate.exists():
        candidate = vdf_path.with_name(
            f"{vdf_path.name}.bak_{timestamp}_{counter}"
        )
        counter += 1
    shutil.copy2(vdf_path, candidate)
    return candidate


def _atomic_write(path: Path, content: bytes) -> None:
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


def import_screenshot(
    image_path: Path | str,
    game_id: int | str,
    steam_userdata_dir: Path | str,
    game_name: str | None = None,
    creation_time: datetime | int | float | None = None,
    session_gap_minutes: float = 30,
    include_timeline: bool = True,
    force: bool = False,
    dry_run: bool = False,
    create_shortcut: bool = True,
    hidden_shortcut: bool = False,
    shortcut_exe: str | None = None,
) -> ImportResult:
    source_path = Path(image_path)
    userdata_dir = Path(steam_userdata_dir)
    game_id_value = _normalize_game_id(game_id)
    game_id_text = str(game_id_value)

    if not source_path.is_file():
        raise ScreenshotImportError(f"Imagem de origem não encontrada: {source_path}")
    if not (userdata_dir / "760").is_dir():
        raise ScreenshotImportError(
            f"Userdata da Steam inválido: {userdata_dir}"
        )

    vdf_path = userdata_dir / "760" / "screenshots.vdf"
    if not vdf_path.is_file():
        raise ScreenshotImportError(f"screenshots.vdf não encontrado: {vdf_path}")

    document = load_vdf_document(vdf_path)
    screenshots = find_mapping_key(document.root, "screenshots")
    if not isinstance(screenshots, Mapping):
        raise ScreenshotImportError(
            "O screenshots.vdf não contém um bloco 'screenshots'"
        )
    existing_key = next(
        (key for key in screenshots.keys() if str(key) == game_id_text),
        None,
    )
    existing_app = screenshots[existing_key] if existing_key is not None else None
    if existing_app is not None and not isinstance(existing_app, Mapping):
        raise ScreenshotImportError(
            f"O bloco do game_id {game_id_text} não é um dicionário"
        )

    source_format, source_size = _source_image_format(source_path)
    width, height = source_size
    if width < 1 or height < 1:
        raise ScreenshotImportError(f"Dimensões inválidas na imagem: {width}x{height}")

    creation, local_moment = _resolve_creation(creation_time, source_path)
    timestamp_part = local_moment.strftime("%Y%m%d%H%M%S")
    screenshots_dir = userdata_dir / "760" / "remote" / game_id_text / "screenshots"
    filename = _choose_filename(screenshots_dir, timestamp_part)
    image_path_out = screenshots_dir / filename
    thumbnail_path_out = screenshots_dir / "thumbnails" / filename

    timelineid, timelinetime = _build_timeline(
        game_id_text,
        creation,
        existing_app,
        session_gap_minutes,
        include_timeline,
    )
    entry = _build_entry(
        game_id_text,
        filename,
        width,
        height,
        creation,
        timelineid,
        timelinetime,
    )
    _, final_index, shortcutname_added = _apply_vdf_entry(
        document.root,
        game_id_text,
        game_id_value,
        entry,
        creation,
        game_name,
    )
    vdf_snippet = _render_entry_snippet(final_index, entry, document.eol)

    shortcut_result = None
    if (
        create_shortcut
        and game_id_value > 0xFFFFFFFF
        and game_name
        and game_name.strip()
    ):
        shortcut_result = ensure_shortcut(
            userdata_dir,
            game_id_value,
            game_name,
            exe=shortcut_exe,
            hidden=hidden_shortcut,
            force=force,
            dry_run=dry_run,
        )

    if dry_run:
        return ImportResult(
            status=STATUS_DRY_RUN,
            image_path=image_path_out,
            thumbnail_path=thumbnail_path_out,
            vdf_path=vdf_path,
            vdf_snippet=vdf_snippet,
            entry=dict(entry),
            creation=creation,
            timelineid=timelineid,
            shortcut=shortcut_result,
        )

    _write_images(
        source_path,
        image_path_out,
        thumbnail_path_out,
        source_format,
        source_size,
    )

    steam_running = is_steam_running()
    if shortcut_result is not None and shortcut_result.status == "pendente":
        steam_running = True
    if steam_running and not force:
        return ImportResult(
            status=STATUS_VDF_PENDING,
            image_path=image_path_out,
            thumbnail_path=thumbnail_path_out,
            vdf_path=vdf_path,
            vdf_snippet=vdf_snippet,
            entry=dict(entry),
            creation=creation,
            timelineid=timelineid,
            pending=PendingVdfUpdate(
                vdf_path=vdf_path,
                game_id=game_id_text,
                entry=dict(entry),
                game_name=game_name,
                shortcutname_added=shortcutname_added,
            ),
            shortcut=shortcut_result,
            steam_running=True,
        )

    updated_content = serialize_screenshots_vdf(
        document.root,
        document.eol,
    ).encode("utf-8")
    backup_path = _create_backup(vdf_path)
    _atomic_write(vdf_path, updated_content)

    return ImportResult(
        status=STATUS_IMPORTED,
        image_path=image_path_out,
        thumbnail_path=thumbnail_path_out,
        vdf_path=vdf_path,
        vdf_snippet=vdf_snippet,
        entry=dict(entry),
        creation=creation,
        timelineid=timelineid,
        backup_path=backup_path,
        shortcut=shortcut_result,
        steam_running=steam_running,
    )
